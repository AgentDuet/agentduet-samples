"""
ParcelPilot order tracking — AgentDuet + Gemini Live + LangGraph.

Inbound voice agent for order tracking and gated modifications against a
local mock order database. Phone only — no WhatsApp/SMS setup required.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.live import AsyncSession

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

from graph import OrderSession
from prompts import AGENT_NAME, BRAND_NAME, CALL_START_KICKOFF, SYSTEM_PROMPT
import orders

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
MODEL = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview")

genai_client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
)


class GeminiOrderBridge:
    def __init__(
        self,
        call: Call,
        live: AsyncSession,
        order_session: OrderSession,
    ):
        self._call = call
        self._live = live
        self._orders = order_session
        self._terminated = False
        self._hang_up_requested = False
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def _agent_hang_up(self) -> None:
        """Let goodbye audio drain, then close the call."""
        logger.info("Agent hanging up call %s", self._call.id)
        try:
            for _ in range(50):  # up to ~5s for goodbye audio
                if await self._call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
        except CallClosedError:
            self._terminated = True
            return
        try:
            result = await self._call.close()
            if not result:
                logger.error(
                    "Hang up failed for %s: %s (%s)",
                    self._call.id,
                    result.error_message,
                    result.error_code,
                )
        except CallClosedError:
            pass
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self._live.send_realtime_input(text=CALL_START_KICKOFF)
        to_model = asyncio.create_task(self._to_model())
        from_model = asyncio.create_task(self._from_model())
        self._tasks = [to_model, from_model]
        await asyncio.gather(to_model, from_model, return_exceptions=True)

    async def _to_model(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except Exception:
            if not self._terminated:
                logger.exception("stream to Gemini failed")

    async def _from_model(self) -> None:
        try:
            while not self._terminated:
                async for response in self._live.receive():
                    if self._terminated:
                        return
                    sc = response.server_content
                    if sc and sc.interrupted:
                        await self._call.clear_send_audio_buffer()
                        break
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Buffer full - drop audio")
                                except CallClosedError:
                                    return
                    if response.tool_call:
                        responses = []
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})
                            result = await self._orders.ainvoke_tool(fc.name, args)
                            if result.get("hang_up"):
                                self._hang_up_requested = True
                            responses.append(
                                types.FunctionResponse(
                                    id=fc.id,
                                    name=fc.name,
                                    response=result,
                                )
                            )
                        await self._live.send_tool_response(
                            function_responses=responses
                        )
                        if self._hang_up_requested:
                            # Brief pause so a same-turn goodbye can start playing.
                            await asyncio.sleep(0.4)
                            await self._agent_hang_up()
                            return
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("receive from Gemini failed")


async def handle_call(call: Call) -> None:
    orders.reload_orders()
    order_session = OrderSession(thread_id=call.id)

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
            )
        ),
        tools=[types.Tool(function_declarations=order_session.gemini_declarations())],
        system_instruction=SYSTEM_PROMPT,
    )

    async with genai_client.aio.live.connect(model=MODEL, config=config) as live:
        answered = await call.answer()
        if not answered:
            logger.error(
                "answer failed: %s (%s)",
                answered.error_message,
                answered.error_code,
            )
            return
        await GeminiOrderBridge(call, live, order_session).run()

    logger.info("Call %s graph snapshot=%s", call.id, order_session.snapshot())
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
        logger.info("%s order-tracking agent (%s) online", AGENT_NAME, BRAND_NAME)

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
