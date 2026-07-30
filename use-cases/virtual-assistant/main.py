"""
Sarah — Apex Retail virtual support assistant.

AgentDuet + OpenAI Realtime. Answers store policy questions from knowledge.md.
Optional local follow-up email capture when she cannot answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.session import RealtimeSession
from dotenv import load_dotenv

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

from prompts import GREETING_KICKOFF, SYSTEM_PROMPT
from tools import SARAH_TOOLS, reset_ctx

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000


def build_runner() -> RealtimeRunner:
    agent = RealtimeAgent(
        name="Sarah",
        instructions=SYSTEM_PROMPT,
        tools=SARAH_TOOLS,
    )
    return RealtimeRunner(
        starting_agent=agent,
        config={
            "model_settings": {
                "model_name": "gpt-realtime-1.5",
                "audio": {
                    "input": {
                        "format": "pcm16",
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "interrupt_response": True,
                        },
                    },
                    "output": {"format": "pcm16", "voice": "coral"},
                },
            }
        },
    )


class OpenAIBridge:
    def __init__(self, call: Call, session: RealtimeSession):
        self._call = call
        self._session = session
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._terminated = False

    async def _shutdown(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        try:
            await self._session.close()
        except Exception:
            logger.exception("Error closing OpenAI session")
        for t in (self._send_task, self._recv_task):
            if t and not t.done():
                t.cancel()
        await asyncio.gather(
            *[t for t in (self._send_task, self._recv_task) if t],
            return_exceptions=True,
        )

    async def _on_hangup(self, _evt: Any) -> None:
        await self._shutdown()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self._session.send_message(GREETING_KICKOFF)
        self._send_task = asyncio.create_task(self._to_model())
        self._recv_task = asyncio.create_task(self._from_model())
        await asyncio.gather(
            self._send_task,
            self._recv_task,
            return_exceptions=True,
        )

    async def _to_model(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._session.send_audio(chunk)
        except (CallClosedError, asyncio.CancelledError):
            pass

    async def _from_model(self) -> None:
        try:
            async for event in self._session:
                if self._terminated:
                    break
                if event.type == "audio":
                    data = event.audio.data
                    if not data:
                        continue
                    try:
                        await self._call.send_audio(data)
                    except BufferFullError:
                        logger.warning("Outgoing buffer full - dropping chunk")
                    except CallClosedError:
                        break
                elif event.type == "audio_interrupted":
                    await self._call.clear_send_audio_buffer()
                elif event.type == "error":
                    logger.error("OpenAI Realtime error: %s", event.error)
        except (CallClosedError, asyncio.CancelledError):
            pass


async def handle_call(call: Call) -> None:
    reset_ctx(call=call)
    logger.info("Sarah call %s", call.id)

    runner = build_runner()
    async with await runner.run() as oai_session:
        answered = await call.answer()
        if not answered:
            logger.error(
                "answer failed: %s (%s)",
                answered.error_message,
                answered.error_code,
            )
            return
        await OpenAIBridge(call, oai_session).run()


async def main() -> None:
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("Sarah (Apex Retail) online")

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
