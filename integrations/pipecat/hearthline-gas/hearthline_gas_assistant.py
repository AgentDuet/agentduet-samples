"""
Hearthline Gas & Energy — AgentDuet + Pipecat + Gemini Live (speech-to-speech).

Inbound residential utility phone assistant. Answers general questions from the
inline FACTS only. No account lookups, payments, live status, or human handoff.
Hang up only when the caller asks to end the call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from agentduet import (
    BufferFullError,
    Call,
    CallAudioConfig,
    CallClosedError,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
BRAND = "Hearthline Gas & Energy"
WEBSITE = "hearthlineenergy.example.com"
AGENT_NAME = "Riley"

FACTS = f"""
Company: {BRAND} (fictional residential natural gas utility for this demo).
Website: https://{WEBSITE}

Customer service hours (local time):
- Monday–Friday: 8:00 AM – 6:00 PM
- Saturday: 9:00 AM – 1:00 PM
- Sunday: closed
- Holidays: closed on major US public holidays (New Year's Day, Independence Day,
  Thanksgiving Day, Christmas Day)

Main office:
420 Ember Street, Suite 200
Mapleford, OR 97035

Service areas (residential natural gas only):
Mapleford, Cedar Ridge, Pine Hollow, and Riverview — all in Oregon.
We do not serve commercial accounts on this line and we do not serve outside those towns.

How to open new residential service:
- Online: start an application at https://{WEBSITE} (preferred, available anytime).
- In person: visit the main office / customer store at the address above during
  customer service hours. Bring a photo ID and your service address. Staff can
  help you start the same new-service application; this phone line cannot open
  an account for you.

General info you may share:
- Bill pay and paperless billing are available on the website.
- Meter access tip (what it means): keep the outdoor gas meter clear of brush,
  weeds, trash cans, and stored items so our technicians can safely reach it to
  read or service the meter. Leave a clear path to the meter; do not lock or
  block it. This tip is general guidance only — not a work order.
- Appointment windows for routine work are booked online; this phone agent cannot schedule.
"""

SYSTEM_PROMPT = f"""You are {AGENT_NAME}, the inbound phone assistant for {BRAND}.

Voice style:
- Short, warm, natural spoken English. Talk like a helpful phone receptionist —
  not a menu, not a brochure.
- One or two sentences per turn. Ask one clarifying question at a time when needed.
- Opening greeting: just welcome them as Riley from the company and ask how you
  can help. Do NOT list topics unprompted.
- Only if they ask what you can help with / what info you have: answer briefly in
  plain speech, for example that you can cover hours, where the office is, which
  towns we serve, starting new service online or stopping by the store, and a
  quick meter-access tip — then ask what they need. Do not sound like reading a checklist.

Grounding (hard rule):
- Answer ONLY from FACTS below. Never invent hours, addresses, towns, fees, outage
  details, account balances, or policies that are not written there.
- If the answer is not in FACTS, say you can only help with general information,
  point them to https://{WEBSITE}, and stay on the line for other general questions.

FACTS:
{FACTS}

Safety (absolute priority — overrides every other rule):
- If the caller describes a gas leak, gas odor, hissing pipe, explosion risk, carbon
  monoxide alarm, CO symptoms (headache, dizziness, nausea, confusion with possible
  CO exposure), fire involving gas, or any immediate safety emergency: tell them to
  leave the area immediately and call emergency services in their area. Do NOT give
  a phone number. Do NOT send them to the company website first. Do NOT invent a
  company emergency line. After the safety instruction, you may briefly say they can
  also report non-emergency follow-up later on the website.

Out of scope (no tools, no lookups, no transfers):
- Account lookup, meter reads, balances, payments, payment arrangements
- Live outage status for a specific address, dispatch, technician ETA
- Scheduling, cancellations, transfers, callbacks, or speaking to a human
- If asked for those: explain this line is general information only, give
  https://{WEBSITE}, and continue listening. Do NOT hang up yet.

Ending the call:
- Only when the caller clearly says goodbye, bye, byy, byee, hang up, end the
  call, or that they are done: say one short goodbye, then call hang_up.
- Treat obvious misspellings of bye/goodbye the same as a clear goodbye.
- Never transfer or offer a human agent.
"""

CALL_START_KICKOFF = (
    f"The phone call is connected. Greet the caller once like a real receptionist: "
    f'warm and brief — "Welcome to {BRAND}, I\'m {AGENT_NAME}. '
    'How can I help you today?" Then wait. Do not list services, hours, address, '
    "or tips unless they ask."
)


class AgentDuetInputTransport(BaseInputTransport):
    """Push AgentDuet caller PCM into the Pipecat pipeline."""

    def __init__(self, transport: BaseTransport, call: Call, params: TransportParams):
        super().__init__(params)
        self._transport = transport
        self._call = call
        self._capture_task: asyncio.Task | None = None
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        self._capture_task = self.create_task(self._capture_audio())
        await self.set_transport_ready(frame)

    async def _capture_audio(self):
        rate = self.sample_rate or SAMPLE_RATE
        try:
            async for chunk in self._call.caller.audio_stream():
                await self.push_audio_frame(
                    InputAudioRawFrame(
                        audio=chunk,
                        sample_rate=rate,
                        num_channels=1,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AgentDuet audio capture failed")

    async def stop(self, frame: EndFrame):
        await self._cancel_capture()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await self._cancel_capture()
        await super().cancel(frame)

    async def cleanup(self):
        await self._cancel_capture()
        await super().cleanup()
        await self._transport.cleanup()

    async def _cancel_capture(self):
        if self._capture_task:
            await self.cancel_task(self._capture_task)
            self._capture_task = None


class AgentDuetOutputTransport(BaseOutputTransport):
    """Play Pipecat output PCM on the AgentDuet call; clear buffer on barge-in."""

    def __init__(self, transport: BaseTransport, call: Call, params: TransportParams):
        super().__init__(params)
        self._transport = transport
        self._call = call
        self._initialized = False
        self._audio_chunks_sent = 0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        try:
            await self._call.send_audio(frame.audio)
            self._audio_chunks_sent += 1
            if self._audio_chunks_sent == 1:
                logger.info(
                    "First outbound audio chunk to call %s (%d bytes @ %d Hz)",
                    self._call.id,
                    len(frame.audio),
                    frame.sample_rate or SAMPLE_RATE,
                )
            return True
        except BufferFullError:
            logger.warning("AgentDuet send buffer full — dropping chunk")
            return False
        except CallClosedError:
            return False

    async def process_frame(self, frame, direction: FrameDirection):
        if isinstance(frame, InterruptionFrame):
            try:
                await self._call.clear_send_audio_buffer()
            except CallClosedError:
                pass
        await super().process_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        await self._transport.cleanup()


class AgentDuetTransport(BaseTransport):
    """Pipecat transport backed by an AgentDuet Call."""

    def __init__(self, call: Call, params: TransportParams | None = None):
        super().__init__()
        self._call = call
        self._params = params or TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_channels=1,
        )
        self._input: AgentDuetInputTransport | None = None
        self._output: AgentDuetOutputTransport | None = None

    def input(self) -> AgentDuetInputTransport:
        if not self._input:
            self._input = AgentDuetInputTransport(self, self._call, self._params)
        return self._input

    def output(self) -> AgentDuetOutputTransport:
        if not self._output:
            self._output = AgentDuetOutputTransport(self, self._call, self._params)
        return self._output


async def handle_call(call: Call) -> None:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY")

    # Capture on the call's event-loop thread. Used if a hangup handler ever runs
    # off-loop (sync → to_thread); async hangup handlers are awaited on this loop
    # directly (agentduet Call._emit_event in 1.0.0b10).
    loop = asyncio.get_running_loop()

    transport = AgentDuetTransport(call)
    hang_up_tool = FunctionSchema(
        name="hang_up",
        description=(
            "End the phone call after the caller says goodbye, bye, byy, byee, "
            "hang up, or that they are finished."
        ),
        properties={},
        required=[],
    )
    llm = GeminiLiveLLMService(
        api_key=api_key,
        settings=GeminiLiveLLMService.Settings(
            model=os.getenv(
                "GEMINI_LIVE_MODEL",
                "models/gemini-3.1-flash-live-preview",
            ),
            voice=os.getenv("GEMINI_LIVE_VOICE", "Aoede"),
            system_instruction=SYSTEM_PROMPT,
        ),
        tools=ToolsSchema(standard_tools=[hang_up_tool]),
    )

    task_holder: dict[str, PipelineTask | None] = {"task": None}

    def _schedule_pipeline_cancel() -> None:
        """Cancel the pipeline without awaiting it on the current stack.

        Pipecat runs tool handlers in a separate task (LLMService.create_task →
        _run_function_call), but that task is still owned by the pipeline's task
        manager. Awaiting PipelineTask.cancel() from inside hang_up / on_hangup
        (which call.close() can nest) can cancel the active tool task mid-flight.
        Scheduling keeps goodbye-audio drain + close on a clean stack.
        """
        task = task_holder["task"]
        if not task:
            return

        async def _cancel() -> None:
            try:
                await task.cancel()
            except Exception:
                logger.exception("Pipeline cancel failed for call %s", call.id)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_cancel())
        else:
            asyncio.run_coroutine_threadsafe(_cancel(), loop)

    async def on_hang_up(params: FunctionCallParams) -> None:
        # Runs in Pipecat's function-call task (not the main frame pump).
        await params.result_callback({"ok": True, "status": "hanging_up"})
        logger.info("hang_up requested for call %s", call.id)
        try:
            for _ in range(50):
                if await call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
        except CallClosedError:
            pass
        try:
            await call.close()
        except CallClosedError:
            pass
        # close() may already have fired on_hangup; scheduling again is harmless.
        _schedule_pipeline_cancel()

    llm.register_function("hang_up", on_hang_up)

    # Seed only — Pipecat does not push this to Gemini until LLMRunFrame.
    context = LLMContext(
        messages=[{"role": "user", "content": CALL_START_KICKOFF}]
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        realtime_service_mode=True,
    )

    pipeline = Pipeline(
        [
            transport.input(),
            user_agg,
            llm,
            transport.output(),
            assistant_agg,
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
        ),
    )
    task_holder["task"] = task

    # agentduet 1.0.0b10 Call._emit_event: async handlers are awaited on the loop;
    # sync handlers go through asyncio.to_thread. Prefer async so we stay on-loop.
    @call.on_hangup
    async def _on_remote_hangup(_evt) -> None:
        logger.info("Remote hangup on call %s", call.id)
        _schedule_pipeline_cancel()

    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(_task, _frame):
        # Answer after the pipeline is live, then kick Gemini so the greeting
        # is generated (context messages alone do not start inference).
        answered = await call.answer()
        if not answered:
            logger.error(
                "answer failed: %s (%s)",
                answered.error_message,
                answered.error_code,
            )
            await task.cancel()
            return
        logger.info("Call %s answered — queueing LLMRunFrame for greeting", call.id)
        await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        try:
            await call.close()
        except CallClosedError:
            pass


async def main() -> None:
    api_key = os.environ.get("AGENTDUET_API_KEY")
    connector = os.environ.get("AGENTDUET_CONNECTOR_UUID")
    if not api_key or not connector:
        raise SystemExit(
            "Set AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID in .env"
        )
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise SystemExit("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector,
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("%s assistant (%s) online", AGENT_NAME, BRAND)

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            try:
                await handle_call(call)
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await call.close()
                except Exception:
                    pass

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
