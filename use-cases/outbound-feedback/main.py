"""
Feedback Collection with Outbound Calls — AgentDuet + Gemini Live.

An automated, ultra-low-latency voice feedback agent calls passengers after
a bus trip from New York City to Washington, D.C.:
1. Comfort rating (1-5)
2. Punctuality (Did the bus depart and arrive on time?)
3. Driver & Cleanliness satisfaction
4. Closing: "Thank you for your feedback. Have a safe day!"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession

from agentduet import (
    BufferFullError,
    Call,
    CallAudioConfig,
    CallClosedError,
    SessionManager,
    SessionManagerConfig,
)

try:
    from agentduet import Address, new_session_id
except ImportError:
    def new_session_id() -> str:
        return uuid.uuid4().hex

    class Address:  # type: ignore
        @staticmethod
        def telco(number: str) -> str:
            return number

from prompts import (
    DESTINATION_CITY,
    END_CALL_TOOL,
    ORIGIN_CITY,
    RECORD_FEEDBACK_TOOL,
    SYSTEM_INSTRUCTION,
)
from tools import (
    TripFeedbackRecord,
    handle_record_feedback_tool,
    log_outbound_disposition,
    save_feedback_record,
)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("outbound_feedback")

SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "24000"))
MODEL = os.getenv("GEMINI_MODEL", "models/gemini-3.1-flash-live-preview")
VOICE_NAME = os.getenv("GEMINI_VOICE", "Zephyr")
HANDOFF_GRACE_SECONDS = float(os.getenv("HANDOFF_AUDIO_GRACE_SECONDS", "3.5"))

genai_client = genai.Client(
    vertexai=False,
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
)


class GeminiLiveFeedbackBridge:
    """Ultra-responsive full-duplex conversational audio bridge for Gemini Live."""

    def __init__(
        self,
        call: Call,
        live: AsyncSession,
        trip_id: str,
        passenger_phone: str,
    ) -> None:
        self._call = call
        self._live = live
        self._trip_id = trip_id
        self._passenger_phone = passenger_phone
        self._terminated = False
        self._end_call = False
        self._tasks: list[asyncio.Task] = []
        self._start_time = asyncio.get_running_loop().time()
        self._feedback_saved = False

    async def _on_hangup(self, _evt: Any) -> None:
        logger.info("Passenger hung up call %s", self._call.id)
        self._terminated = True
        duration = asyncio.get_running_loop().time() - self._start_time
        if not self._feedback_saved:
            logger.info("Call ended before survey completion; logging partial disposition.")
            partial_rec = TripFeedbackRecord(
                trip_id=self._trip_id,
                passenger_phone=self._passenger_phone,
                call_status="INCOMPLETE",
                disposition="PASSENGER_HANGUP",
                origin=ORIGIN_CITY,
                destination=DESTINATION_CITY,
                call_duration_seconds=round(duration, 2),
            )
            save_feedback_record(partial_rec)
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)

        passenger_party = self._call.callee or self._call.caller

        # Send initial realtime greeting trigger
        await self._live.send_realtime_input(
            text=(
                f"The phone call is connected. Greet the passenger naturally: "
                f"'Hi there! I'm calling from TransitExpress's virtual support team about your bus ride from {ORIGIN_CITY} to {DESTINATION_CITY}. "
                f"Do you mind if I ask you a couple of quick questions?'"
            )
        )

        to_model = asyncio.create_task(self._to_model(passenger_party))
        from_model = asyncio.create_task(self._from_model())
        self._tasks = [to_model, from_model]

        await asyncio.gather(to_model, from_model, return_exceptions=True)

    async def _to_model(self, party: Any) -> None:
        """Stream passenger microphone audio to Gemini Live."""
        try:
            async for chunk in party.audio_stream():
                if self._terminated:
                    break
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("Error streaming passenger audio to Gemini Live")

    async def _from_model(self) -> None:
        """Receive synthesized audio and tool calls from Gemini Live."""
        try:
            while not self._terminated:
                async for response in self._live.receive():
                    sc = response.server_content
                    if sc and sc.interrupted:
                        logger.debug("Passenger spoke; clearing audio buffer immediately")
                        await self._call.clear_send_audio_buffer()
                        break

                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Buffer full - dropping chunk")
                                except CallClosedError:
                                    return

                    # Handle Tool Calls
                    if response.tool_call:
                        responses = []
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})
                            if fc.name == "record_feedback":
                                logger.info("⚡ [TOOL CALL] record_feedback(%s)", args)
                                duration = (
                                    asyncio.get_running_loop().time() - self._start_time
                                )
                                tool_result, _ = handle_record_feedback_tool(
                                    trip_id=self._trip_id,
                                    passenger_phone=self._passenger_phone,
                                    args=args,
                                    duration_seconds=duration,
                                )
                                self._feedback_saved = True
                                responses.append(
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response=tool_result,
                                    )
                                )

                            elif fc.name == "end_call":
                                logger.info("⚡ [TOOL CALL] end_call triggered by agent.")
                                self._end_call = True
                                responses.append(
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={"ok": True},
                                    )
                                )

                        if responses:
                            await self._live.send_tool_response(
                                function_responses=responses
                            )

                        if self._end_call:
                            logger.info(
                                "Closing speech active — waiting %.1fs for speech playback to finish completely...",
                                HANDOFF_GRACE_SECONDS,
                            )
                            await asyncio.sleep(HANDOFF_GRACE_SECONDS)
                            logger.info("👋 Closing speech complete. Disconnecting call cleanly...")
                            self._terminated = True
                            try:
                                await self._call.disconnect()
                            except Exception:
                                pass
                            return
        except CallClosedError:
            pass
        except genai_errors.APIError as e:
            logger.debug("Gemini Live API error: %s - %s", e.code, e.message)
        except Exception:
            if not self._terminated:
                logger.exception("Error receiving from Gemini Live")


async def conduct_outbound_feedback(
    call: Call,
    trip_id: str,
    passenger_phone: str,
) -> None:
    """Connect to Gemini Live and conduct interactive passenger feedback survey."""
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[
            types.Tool(
                function_declarations=[RECORD_FEEDBACK_TOOL, END_CALL_TOOL]
            )
        ],
    )

    async with genai_client.aio.live.connect(model=MODEL, config=config) as live:
        logger.info(
            "Connected to Gemini Live session. Running feedback survey for Trip %s (%s -> %s)...",
            trip_id,
            ORIGIN_CITY,
            DESTINATION_CITY,
        )
        bridge = GeminiLiveFeedbackBridge(
            call=call,
            live=live,
            trip_id=trip_id,
            passenger_phone=passenger_phone,
        )
        await bridge.run()


async def dial_passenger_for_feedback(
    sm: SessionManager,
    subscriber: str,
    passenger_phone: str,
    trip_id: str = "NYC-DC-EXP",
) -> TripFeedbackRecord:
    """Place an outbound feedback call and handle both answered and unanswered outcomes cleanly."""
    logger.info(
        "📞 Dialing outbound feedback call for Trip %s (%s -> %s) to passenger %s (originating: %s)...",
        trip_id,
        ORIGIN_CITY,
        DESTINATION_CITY,
        passenger_phone,
        subscriber,
    )

    session = await sm.open_session(new_session_id(), subscriber)
    call = await session.make_call(Address.telco(passenger_phone))
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[
            types.Tool(
                function_declarations=[RECORD_FEEDBACK_TOOL, END_CALL_TOOL]
            )
        ],
    )

    async with genai_client.aio.live.connect(model=MODEL, config=config) as live:
        logger.info("⚡ Pre-connected Gemini Live session. Initiating call dial...")
        dial_result = await call.dial(ring_time_seconds=25)

        if dial_result:
            logger.info("✅ Passenger answered call %s. Instantly starting voice bridge...", call.id)
            bridge = GeminiLiveFeedbackBridge(
                call=call,
                live=live,
                trip_id=trip_id,
                passenger_phone=passenger_phone,
            )
            try:
                await bridge.run()
            finally:
                try:
                    await call.disconnect()
                except Exception:
                    pass
            return TripFeedbackRecord(
                trip_id=trip_id,
                passenger_phone=passenger_phone,
                call_status="COMPLETED",
                disposition="CONNECTED",
                origin=ORIGIN_CITY,
                destination=DESTINATION_CITY,
            )
        else:
            disposition = dial_result.error_code or "CALL_UNANSWERED"
            logger.info(
                "ℹ️ Outbound call for Trip %s was not answered (Outcome: %s). "
                "Normal disposition logged; proceeding without error.",
                trip_id,
                disposition,
            )
            record = log_outbound_disposition(
                trip_id=trip_id,
                passenger_phone=passenger_phone,
                disposition_code=disposition,
            )
            try:
                await call.close()
            except Exception:
                pass
            return record


async def main() -> None:
    api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")
    subscriber = os.getenv("AGENTDUET_SUBSCRIBER")
    destination = os.getenv("DESTINATION_NUMBER")

    if not api_key or not connector_uuid:
        logger.error("AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID must be set.")
        return

    if not subscriber or not destination:
        logger.error("Set AGENTDUET_SUBSCRIBER (originating line) and DESTINATION_NUMBER (passenger).")
        return

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        logger.error("GEMINI_API_KEY must be set to run Gemini Live.")
        return

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=SAMPLE_RATE,
            buffer_size=1024 * 1024,
        ),
    )

    async with SessionManager(config) as sm:
        logger.info("TransitExpress outbound feedback agent online.")
        await dial_passenger_for_feedback(
            sm=sm,
            subscriber=subscriber,
            passenger_phone=destination,
            trip_id="NYC-DC-EXP",
        )
        logger.info("Outbound feedback job completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Outbound feedback agent shutting down.")
