"""
Meridian Clinic AI receptionist — AgentDuet + Qwen Omni + CRM lookup.

Known callers get a personalized greeting and open tickets; unknown callers
become leads. Tools: get_open_tickets, record_intent, update_lead_name,
transfer_to_human (connect + close).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
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

from crm import CRMBackend, create_crm_backend
from prompts import COMPANY_NAME, build_system_prompt, build_tool_specs

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
MODEL = "qwen3.5-omni-flash-realtime"
HANDOFF_GRACE_SECONDS = float(os.getenv("HANDOFF_AUDIO_GRACE_SECONDS", "3"))
RECEPTION_CALLS_PATH = Path(
    os.getenv("RECEPTION_CALLS_PATH", str(HERE / "data" / "reception_calls.json"))
).resolve()
CRM_BACKEND_NAME = os.getenv("CRM_BACKEND", "json").strip().lower()


class ReceptionCallLog:
    def __init__(self, path: Path = RECEPTION_CALLS_PATH):
        self.path = path

    def append(self, entry: dict[str, Any]) -> None:
        data: dict[str, Any] = {"calls": []}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("calls", []).append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class QwenClient:
    def __init__(
        self,
        *,
        api_key: str,
        instructions: str,
        tools: list[dict[str, Any]],
        on_audio: Callable[[bytes], None],
        on_transcript: Callable[[str, str], Any],
        on_interrupt: Callable[[], Any],
        on_tool_call: Callable[[str, str, dict[str, Any]], Any],
    ):
        self.url = f"{QWEN_WS_URL}?model={MODEL}"
        self.api_key = api_key
        self.instructions = instructions
        self.tools = tools
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_interrupt = on_interrupt
        self.on_tool_call = on_tool_call
        self.ws = None
        self._responding = False

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.api_key}"}
        )
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "voice": os.getenv("QWEN_VOICE", "Jennifer"),
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
        }
        if self.tools:
            session["tools"] = self.tools
        await self._send({"type": "session.update", "session": session})

    async def _send(self, event: dict[str, Any]) -> None:
        assert self.ws is not None
        if "event_id" not in event:
            event["event_id"] = f"evt_{int(time.time() * 1000)}"
        await self.ws.send(json.dumps(event))

    async def append_audio(self, pcm16_16k: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16_16k).decode("ascii"),
            }
        )

    async def cancel(self) -> None:
        if self._responding:
            await self._send({"type": "response.cancel"})
            self._responding = False

    async def send_tool_output(self, call_id: str, output: dict[str, Any]) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output),
                },
            }
        )
        await self._send({"type": "response.create"})

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
            elif etype == "response.function_call_arguments.done":
                name = event.get("name") or ""
                call_id = event.get("call_id") or ""
                args = json.loads(event.get("arguments") or "{}")
                await self.on_tool_call(name, call_id, args)
            elif etype == "error":
                logger.error("Qwen error: %s", event)

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()


class Receptionist:
    def __init__(
        self,
        call: Call,
        qwen: QwenClient,
        *,
        crm: CRMBackend,
        customer: dict[str, Any] | None,
        caller_phone: str,
    ):
        self._call = call
        self._qwen = qwen
        self._crm = crm
        self._customer = customer
        self._caller_phone = caller_phone
        self._terminated = False
        self._transfer = asyncio.Event()
        self._transfer_reason = ""
        self._classified_intent: str | None = None
        self._tool_calls: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task] = []

    @property
    def transfer_requested(self) -> bool:
        return self._transfer.is_set()

    @property
    def transfer_reason(self) -> str:
        return self._transfer_reason

    @property
    def classified_intent(self) -> str | None:
        return self._classified_intent

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self._tool_calls

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

    def on_audio(self, audio: bytes) -> None:
        if self._terminated:
            return
        asyncio.create_task(self._send(audio))

    async def _send(self, audio: bytes) -> None:
        try:
            await self._call.send_audio(audio)
        except (BufferFullError, CallClosedError):
            pass

    async def on_tool_call(
        self, name: str, call_id: str, args: dict[str, Any]
    ) -> None:
        logger.info("Tool %s args=%s", name, args)
        self._tool_calls.append({"name": name, "arguments": args})
        result: dict[str, Any]

        if name == "get_open_tickets":
            customer_id = str(args.get("customer_id") or "").strip()
            if not customer_id and self._customer:
                customer_id = str(self._customer.get("id", ""))
            tickets = await self._crm.get_open_tickets(customer_id)
            result = {"customer_id": customer_id, "open_tickets": tickets}
        elif name == "record_intent":
            self._classified_intent = str(args.get("intent", "general"))
            result = {
                "recorded": True,
                "intent": self._classified_intent,
                "notes": args.get("notes", ""),
            }
        elif name == "update_lead_name":
            caller_name = str(args.get("name", "")).strip()
            updated = await self._crm.update_lead_name(self._caller_phone, caller_name)
            result = {"updated": updated, "name": caller_name}
        elif name == "transfer_to_human":
            reason = str(args.get("reason", "Caller requested human agent")).strip()
            department = str(args.get("department", "general"))
            self._transfer_reason = f"{department}: {reason}"
            result = {
                "transfer": True,
                "reason": reason,
                "department": department,
                "message": "Connecting caller to human agent now.",
            }
            await self._qwen.send_tool_output(call_id, result)
            self._transfer.set()
            return
        else:
            result = {"ok": False, "error": f"Unknown tool: {name}"}

        await self._qwen.send_tool_output(call_id, result)

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
        logger.info("Transfer requested — waiting %.1fs for goodbye audio", HANDOFF_GRACE_SECONDS)
        await asyncio.sleep(HANDOFF_GRACE_SECONDS)
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


async def handle_call(call: Call, crm: CRMBackend, call_log: ReceptionCallLog) -> None:
    phone = call.participant.value if call.participant else "unknown"
    customer = await crm.get_customer_by_phone(phone)
    lead_created = False
    lead_record: dict[str, Any] | None = None
    open_tickets: list[dict[str, Any]] = []

    if customer:
        logger.info("CRM hit: %s (%s)", customer.get("name"), customer.get("id"))
        open_tickets = await crm.get_open_tickets(str(customer.get("id", "")))
    else:
        lead_record = await crm.create_lead(phone, call.id)
        lead_created = bool(lead_record.get("created", True))
        logger.info("Created lead id=%s for %s", lead_record.get("id"), phone)

    instructions = build_system_prompt(
        customer=customer,
        open_tickets=open_tickets,
        lead_created=lead_created or (customer is None),
        caller_number=phone,
    )

    receptionist: Optional[Receptionist] = None

    async def on_interrupt() -> None:
        if receptionist:
            await receptionist.on_interrupt()

    async def on_transcript(role: str, text: str) -> None:
        if receptionist:
            await receptionist.on_transcript(role, text)

    async def on_tool_call(name: str, call_id: str, args: dict[str, Any]) -> None:
        if receptionist:
            await receptionist.on_tool_call(name, call_id, args)

    qwen = QwenClient(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        instructions=instructions,
        tools=build_tool_specs(),
        on_audio=lambda b: receptionist.on_audio(b) if receptionist else None,
        on_transcript=on_transcript,
        on_interrupt=on_interrupt,
        on_tool_call=on_tool_call,
    )
    receptionist = Receptionist(
        call,
        qwen,
        crm=crm,
        customer=customer,
        caller_phone=phone,
    )

    answered = await call.answer()
    if not answered:
        logger.error(
            "answer failed: %s (%s)", answered.error_message, answered.error_code
        )
        return

    transfer = await receptionist.run()
    outcome = "handoff_to_human" if transfer else "ai_handled"

    call_log.append(
        {
            "id": str(uuid.uuid4()),
            "call_id": call.id,
            "caller_number": phone,
            "customer_id": customer.get("id") if customer else None,
            "customer_name": customer.get("name") if customer else None,
            "lead_created": lead_created,
            "crm_backend": CRM_BACKEND_NAME,
            "lead_record": lead_record,
            "intent": receptionist.classified_intent,
            "outcome": outcome,
            "handoff_reason": receptionist.transfer_reason or None,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "tool_calls": receptionist.tool_calls,
        }
    )

    if not transfer:
        try:
            await call.close()
        except CallClosedError:
            pass
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
    crm = create_crm_backend()
    call_log = ReceptionCallLog()
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(
            sample_rate=SAMPLE_RATE, buffer_size=8 * 1024 * 1024
        ),
    )
    async with SessionManager(config) as sm:
        logger.info(
            "%s AI receptionist online (CRM=%s model=%s)",
            COMPANY_NAME,
            CRM_BACKEND_NAME,
            MODEL,
        )

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            try:
                await handle_call(call, crm, call_log)
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await call.close()
                except Exception:
                    pass

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
