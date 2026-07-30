"""
Customer Support — AgentDuet + OpenAI Realtime.

Answers FAQs, files tickets, escalates to a human when needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agents import function_tool
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

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
TICKET_DIR = Path(__file__).resolve().parent / "data"
TICKET_DIR.mkdir(parents=True, exist_ok=True)

_CTX: dict[str, Any] = {}


@function_tool
async def create_ticket(subject: str, details: str, priority: str = "normal") -> str:
    """File a support ticket after the caller describes their issue."""
    ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "ticket_id": ticket_id,
        "call_id": _CTX.get("call_id"),
        "caller": _CTX.get("caller"),
        "subject": subject,
        "details": details,
        "priority": priority,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = TICKET_DIR / f"{ticket_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    logger.info("Ticket saved %s", path)
    _CTX["last_ticket_id"] = ticket_id
    return (
        f"Ticket {ticket_id} created. Read the ticket ID back to the caller. "
        "Ask if anything else is needed."
    )


@function_tool
async def escalate_to_human(reason: str) -> str:
    """Transfer to a human agent when the bot cannot help."""
    _CTX["escalate"] = {"reason": reason}
    return (
        "Tell the caller you are connecting them to a specialist now. "
        "Keep it to one short sentence."
    )


@function_tool
async def hang_up() -> str:
    """End the call after a brief goodbye."""
    call: Call | None = _CTX.get("call")
    if call is not None:
        try:
            for _ in range(40):
                if await call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
            await call.close()
        except CallClosedError:
            pass
    return "Call ended."


INSTRUCTIONS = (
    "You are a phone customer support agent for a general consumer product company. "
    "Greet briefly, ask how you can help, and keep replies short and spoken. "
    "For account issues that need follow-up, call create_ticket and read the ticket ID. "
    "If the caller is angry, asks for a person, or the request is outside basic FAQ, "
    "call escalate_to_human. When they are done, say goodbye and call hang_up. "
    "Never invent refund amounts, order statuses, or policy exceptions."
)


def build_runner() -> RealtimeRunner:
    agent = RealtimeAgent(
        name="CustomerSupport",
        instructions=INSTRUCTIONS,
        tools=[create_ticket, escalate_to_human, hang_up],
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
                    "output": {"format": "pcm16", "voice": "ash"},
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

    async def _on_hangup(self, _evt: Any) -> None:
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

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        self._send_task = asyncio.create_task(self._to_model())
        self._recv_task = asyncio.create_task(self._from_model())
        await asyncio.gather(self._send_task, self._recv_task, return_exceptions=True)

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
                        logger.warning("Outgoing buffer full — dropping chunk")
                    except CallClosedError:
                        break
                elif event.type == "audio_interrupted":
                    await self._call.clear_send_audio_buffer()
                elif event.type == "error":
                    logger.error("OpenAI Realtime error: %s", event.error)
        except (CallClosedError, asyncio.CancelledError):
            pass


async def handle_call(call: Call) -> None:
    _CTX.clear()
    _CTX.update(
        {
            "call": call,
            "call_id": call.id,
            "caller": call.participant.value if call.participant else "unknown",
        }
    )
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

    if not _CTX.get("escalate"):
        return

    try:
        await call.clear_send_audio_buffer()
    except CallClosedError:
        return
    connected = await call.connect(ring_time_seconds=40)
    if not connected:
        logger.error(
            "connect failed: %s (%s)",
            connected.error_message,
            connected.error_code,
        )
        await call.disconnect()
        return
    await call.close()


async def main() -> None:
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("Customer support agent online")

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
