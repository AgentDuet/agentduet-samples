"""
Human Escalation — AgentDuet + Grok Voice.

AI talks first; escalate_to_human saves context and bridges to staff.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import websockets
from dotenv import load_dotenv
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus

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
GROK_MODEL = "grok-voice-latest"
GROK_URL = f"wss://api.x.ai/v1/realtime?model={GROK_MODEL}"
GROK_VOICE = os.environ.get("GROK_VOICE", "leo").lower()
CONTEXT_DIR = Path(__file__).resolve().parent / "data"
CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "You are a helpful phone support agent. Keep replies short and conversational. "
    "Handle common FAQ yourself. If the caller is frustrated, asks for a human, "
    "or needs something outside your abilities, call save_context_and_escalate with "
    "a concise summary. When the caller is done and says goodbye, call hang_up."
)

TOOLS = [
    {
        "type": "function",
        "name": "save_context_and_escalate",
        "description": "Save a handoff summary and transfer to a human specialist.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short situation summary for the human agent.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why escalation is needed.",
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["calm", "confused", "frustrated", "urgent"],
                },
            },
            "required": ["summary", "reason", "sentiment"],
        },
    },
    {
        "type": "function",
        "name": "hang_up",
        "description": "End the call after a brief goodbye.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


class GrokEscalationBridge:
    def __init__(self, call: Call, ws: ClientConnection):
        self._call = call
        self._ws = ws
        self._terminated = False
        self._escalate: Optional[dict[str, Any]] = None
        self._playback_gen = 0
        self._audio_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        self._active_response_id: Optional[str] = None
        self._cancelled: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._ws.close()
        except Exception:
            pass
        for t in self._tasks:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def run(self) -> Optional[dict[str, Any]]:
        self._call.on_hangup(self._on_hangup)
        await self._configure()
        await self._ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": "Greet the caller briefly and ask how you can help."
                    },
                }
            )
        )
        playback = asyncio.create_task(self._playback())
        send = asyncio.create_task(self._stream_up())
        recv = asyncio.create_task(self._receive())
        self._tasks = [playback, send, recv]
        await asyncio.gather(send, recv, return_exceptions=True)
        self._audio_queue.put_nowait(None)
        try:
            await playback
        except asyncio.CancelledError:
            pass
        return self._escalate

    async def _configure(self) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "voice": GROK_VOICE,
                        "instructions": SYSTEM_PROMPT,
                        "reasoning": {"effort": "none"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "silence_duration_ms": 300,
                            "prefix_padding_ms": 200,
                        },
                        "tools": TOOLS,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE}
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE}
                            },
                        },
                    },
                }
            )
        )

    async def _stream_up(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
                )
        except (CallClosedError, ConnectionClosed):
            pass

    async def _playback(self) -> None:
        while True:
            item = await self._audio_queue.get()
            if item is None:
                break
            gen, audio = item
            if gen != self._playback_gen:
                continue
            try:
                await self._call.send_audio(audio)
            except (BufferFullError, CallClosedError):
                break

    async def _flush(self) -> None:
        self._playback_gen += 1
        self._active_response_id = None
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            await self._call.clear_send_audio_buffer()
        except CallClosedError:
            pass

    async def _tool_output(self, call_id: str, output: dict[str, Any]) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(output),
                    },
                }
            )
        )

    async def _save_context(self, args: dict[str, Any]) -> Path:
        path = CONTEXT_DIR / f"{self._call.id}.json"
        payload = {
            "call_id": self._call.id,
            "caller": self._call.participant.value if self._call.participant else None,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            **args,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Escalation context saved %s", path)
        return path

    async def _receive(self) -> None:
        try:
            async for raw in self._ws:
                if self._terminated:
                    break
                event = json.loads(raw)
                etype = event.get("type")

                if etype == "response.created":
                    self._active_response_id = event.get("response", {}).get("id")
                elif etype == "input_audio_buffer.speech_started":
                    try:
                        buf = await self._call.get_send_audio_buffer_size()
                    except CallClosedError:
                        buf = 0
                    if self._active_response_id or buf or self._audio_queue.qsize():
                        if self._active_response_id:
                            self._cancelled.add(self._active_response_id)
                        await self._flush()
                elif etype == "response.done":
                    rid = event.get("response", {}).get("id")
                    if rid:
                        self._cancelled.discard(rid)
                    if rid == self._active_response_id:
                        self._active_response_id = None
                elif etype in ("response.output_audio.delta", "response.audio.delta"):
                    rid = event.get("response_id")
                    if rid and rid in self._cancelled:
                        continue
                    audio = base64.b64decode(event["delta"])
                    self._audio_queue.put_nowait((self._playback_gen, audio))
                elif etype == "response.function_call_arguments.done":
                    name = event.get("name")
                    call_id = event["call_id"]
                    args = json.loads(event.get("arguments") or "{}")
                    if name == "save_context_and_escalate":
                        path = await self._save_context(args)
                        self._escalate = {**args, "path": str(path)}
                        await self._tool_output(
                            call_id, {"status": "saved", "path": str(path)}
                        )
                        # Let Grok say a short transfer line, then we bridge.
                        await self._ws.send(
                            json.dumps(
                                {
                                    "type": "response.create",
                                    "response": {
                                        "instructions": (
                                            "Tell the caller you are connecting them "
                                            "to a specialist now. Keep it to one sentence."
                                        )
                                    },
                                }
                            )
                        )
                        await asyncio.sleep(3.5)
                        return
                    if name == "hang_up":
                        await self._tool_output(call_id, {"status": "hanging_up"})
                        for _ in range(40):
                            try:
                                if await self._call.get_send_audio_buffer_size() == 0:
                                    break
                            except CallClosedError:
                                return
                            await asyncio.sleep(0.1)
                        await self._call.close()
                        return
                elif etype == "error":
                    logger.error("Grok error: %s", event)
        except (CallClosedError, ConnectionClosed):
            pass


async def handle_call(call: Call) -> None:
    try:
        async with websockets.connect(
            GROK_URL,
            additional_headers={
                "Authorization": f"Bearer {os.environ['XAI_API_KEY']}"
            },
            open_timeout=15,
        ) as ws:
            answered = await call.answer()
            if not answered:
                logger.error(
                    "answer failed: %s (%s)",
                    answered.error_message,
                    answered.error_code,
                )
                return
            escalate = await GrokEscalationBridge(call, ws).run()
    except InvalidStatus as e:
        body = getattr(e.response, "body", b"") or b""
        logger.error("Grok rejected: %s", body.decode("utf-8", errors="replace"))
        return

    if not escalate:
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
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("Human escalation agent online")

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
