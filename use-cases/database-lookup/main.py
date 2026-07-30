"""
Database Lookup receptionist — AgentDuet + Qwen Omni + SQLite CRM.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import soxr
import websockets
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

from crm import CRM, Customer

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
REGION = os.getenv("DASHSCOPE_REGION", "intl")
BASE_DOMAIN = (
    "dashscope-intl.aliyuncs.com" if REGION == "intl" else "dashscope.aliyuncs.com"
)
QWEN_WS_URL = f"wss://{BASE_DOMAIN}/api-ws/v1/realtime"
MODEL = "qwen3-omni-flash-realtime"
CRM_DB = Path(__file__).resolve().parent / "data" / "crm.sqlite"

TRANSFER_PHRASES = (
    "transfer me",
    "speak to a human",
    "talk to a person",
    "real person",
    "customer service",
    "agent please",
    "representative",
)


def build_instructions(customer: Optional[Customer], phone: str) -> str:
    if customer:
        context = (
            f"Known customer: {customer.name}, tier={customer.tier}, "
            f"open_tickets={customer.open_tickets}. Notes: {customer.notes}. "
            "Greet them by name. Only state facts from this context — never invent "
            "account balances, order statuses, or ticket IDs."
        )
    else:
        context = (
            f"Unknown caller phone {phone}. A new lead was just created. "
            "Welcome them, ask how you can help, and collect their name if natural."
        )
    return (
        "You are a phone receptionist. Keep answers short and spoken. "
        f"{context} "
        "If they need a human, billing disputes, or anything outside simple FAQ, "
        "say you will connect them now and stop talking. "
        "Do not claim you transferred until the system does."
    )


class QwenClient:
    def __init__(
        self,
        *,
        api_key: str,
        instructions: str,
        on_audio: Callable[[bytes], None],
        on_transcript: Callable[[str, str], Any],
        on_interrupt: Callable[[], Any],
    ):
        self.url = f"{QWEN_WS_URL}?model={MODEL}"
        self.api_key = api_key
        self.instructions = instructions
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_interrupt = on_interrupt
        self.ws = None
        self._responding = False

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.api_key}"}
        )
        await self.ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "voice": "Jennifer",
                        "instructions": self.instructions,
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm24",
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                        "input_audio_transcription": {"model": "gummy-realtime-v1"},
                    },
                }
            )
        )

    async def append_audio(self, pcm16_16k: bytes) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16_16k).decode("ascii"),
                }
            )
        )

    async def cancel(self) -> None:
        if self._responding:
            await self.ws.send(json.dumps({"type": "response.cancel"}))
            self._responding = False

    async def receive_loop(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            event = json.loads(raw)
            etype = event.get("type")
            if etype == "response.audio.delta":
                self.on_audio(base64.b64decode(event["delta"]))
            elif etype == "input_audio_buffer.speech_started":
                await self.on_interrupt()
            elif etype == "response.created":
                self._responding = True
            elif etype == "response.done":
                self._responding = False
            elif etype == "conversation.item.input_audio_transcription.completed":
                text = event.get("transcript") or ""
                await self.on_transcript("caller", text)
            elif etype == "response.audio_transcript.done":
                text = event.get("transcript") or ""
                await self.on_transcript("agent", text)
            elif etype == "error":
                logger.error("Qwen error: %s", event)

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()


class Receptionist:
    def __init__(self, call: Call, qwen: QwenClient):
        self._call = call
        self._qwen = qwen
        self._terminated = False
        self._transfer = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        await self._qwen.close()
        for t in self._tasks:
            t.cancel()

    async def on_interrupt(self) -> None:
        await self._qwen.cancel()
        await self._call.clear_send_audio_buffer()

    async def on_transcript(self, role: str, text: str) -> None:
        logger.info("[%s] %s", role, text)
        lower = text.lower()
        if role == "caller" and any(p in lower for p in TRANSFER_PHRASES):
            self._transfer.set()
        if role == "agent" and "connect you" in lower:
            self._transfer.set()

    def on_audio(self, audio: bytes) -> None:
        if self._terminated:
            return
        asyncio.create_task(self._send(audio))

    async def _send(self, audio: bytes) -> None:
        try:
            await self._call.send_audio(audio)
        except (BufferFullError, CallClosedError):
            pass

    @staticmethod
    def downsample_24_to_16(pcm: bytes) -> bytes:
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype=np.int16)
        return soxr.resample(samples, 24000, 16000).astype(np.int16).tobytes()

    async def run(self) -> bool:
        self._call.on_hangup(self._on_hangup)
        await self._qwen.connect()
        self._tasks = [
            asyncio.create_task(self._stream_up()),
            asyncio.create_task(self._qwen.receive_loop()),
            asyncio.create_task(self._watch_transfer()),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return self._transfer.is_set()

    async def _watch_transfer(self) -> None:
        await self._transfer.wait()
        logger.info("Transfer requested — stopping AI bridge")
        self._terminated = True
        await self._qwen.close()
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()

    async def _stream_up(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._qwen.append_audio(self.downsample_24_to_16(chunk))
        except CallClosedError:
            pass


async def handle_call(call: Call, crm: CRM) -> None:
    phone = call.participant.value if call.participant else "unknown"
    customer = crm.get_by_phone(phone)
    if customer:
        logger.info("CRM hit: %s (%s)", customer.name, customer.tier)
    else:
        lead_id = crm.create_lead(phone)
        logger.info("Created lead id=%s for %s", lead_id, phone)

    receptionist: Optional[Receptionist] = None

    async def on_interrupt() -> None:
        if receptionist:
            await receptionist.on_interrupt()

    async def on_transcript(role: str, text: str) -> None:
        if receptionist:
            await receptionist.on_transcript(role, text)

    qwen = QwenClient(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        instructions=build_instructions(customer, phone),
        on_audio=lambda b: receptionist.on_audio(b) if receptionist else None,
        on_transcript=on_transcript,
        on_interrupt=on_interrupt,
    )
    receptionist = Receptionist(call, qwen)

    answered = await call.answer()
    if not answered:
        logger.error(
            "answer failed: %s (%s)", answered.error_message, answered.error_code
        )
        return

    transfer = await receptionist.run()
    if not transfer:
        await call.close()
        return

    try:
        await call.clear_send_audio_buffer()
    except CallClosedError:
        return
    connected = await call.connect(ring_time_seconds=40)
    if not connected:
        logger.error(
            "connect failed: %s (%s)", connected.error_message, connected.error_code
        )
        await call.disconnect()
        return
    await call.close()


async def main() -> None:
    crm = CRM(CRM_DB)
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(
            sample_rate=SAMPLE_RATE, buffer_size=8 * 1024 * 1024
        ),
    )
    async with SessionManager(config) as sm:
        logger.info("Database lookup receptionist online")

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            try:
                await handle_call(call, crm)
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await call.close()
                except Exception:
                    pass

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
