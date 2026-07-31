"""
Human Escalation - AgentDuet + Grok Voice.

VibeRider support talks first; escalate_to_human saves context and bridges to staff.
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
GROK_MODEL = "grok-voice-think-fast-1.0"
GROK_REALTIME_URL = f"wss://api.x.ai/v1/realtime?model={GROK_MODEL}"
GROK_VOICE = os.environ.get("GROK_VOICE", "leo").lower()
AGENT_NAME = "Alex"
BRAND = "VibeRider"
CONTEXT_DIR = HERE / "data"
CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    f"Your name is {AGENT_NAME}. You are a {BRAND} phone support agent. "
    "Keep replies short and conversational. Handle common FAQ yourself "
    f"(account access, trip receipts, app troubleshooting, {BRAND} wallet basics). "
    "If the caller is frustrated, asks for a human, or needs something outside "
    "your abilities, call save_context_and_escalate with a concise summary. "
    "When the caller is done and says goodbye, call hang_up."
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
        "description": (
            "End the phone call. Use when the caller asks to hang up, "
            "end the call, or says goodbye and wants to leave."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


class GrokEscalationBridge:
    """Bidirectional audio bridge: AgentDuet Call ↔ xAI Grok Realtime."""

    def __init__(self, call: Call, ws: ClientConnection):
        self._call = call
        self._ws = ws
        self._terminated = False
        self._escalate: Optional[dict[str, Any]] = None
        self._playback_gen = 0
        self._audio_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        self._active_response_id: Optional[str] = None
        self._cancelled: set[str] = set()
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._playback_task: Optional[asyncio.Task] = None

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._ws.close()
        except Exception:
            pass
        for t in (self._send_task, self._recv_task, self._playback_task):
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
                        "instructions": (
                            f"Greet the caller warmly. Introduce yourself as {AGENT_NAME} "
                            f"from {BRAND} support and ask how you can help today."
                        )
                    },
                }
            )
        )
        self._playback_task = asyncio.create_task(self._playback())
        self._send_task = asyncio.create_task(self._stream_up())
        self._recv_task = asyncio.create_task(self._receive())
        await asyncio.gather(self._send_task, self._recv_task, return_exceptions=True)
        if self._playback_task and not self._playback_task.done():
            self._audio_queue.put_nowait(None)
            try:
                await self._playback_task
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error streaming caller audio to Grok")
            raise

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
            except BufferFullError:
                logger.warning("Outgoing buffer full - dropping chunk")
            except CallClosedError:
                break
            except asyncio.CancelledError:
                raise

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
            return

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
            "brand": BRAND,
            "caller": self._call.participant.value if self._call.participant else None,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            **args,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Escalation context saved %s", path)
        return path

    async def _agent_hang_up(self) -> None:
        logger.info("Agent hanging up call %s", self._call.id)
        try:
            for _ in range(40):  # up to ~4s for goodbye audio to drain
                if await self._call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
        except CallClosedError:
            return
        result = await self._call.close()
        if not result:
            logger.error(
                "Hang up failed for %s: %s (%s)",
                self._call.id,
                result.error_message,
                result.error_code,
            )

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
                    should_flush = (
                        self._active_response_id is not None
                        or buf > 0
                        or self._audio_queue.qsize() > 0
                    )
                    if should_flush:
                        if self._active_response_id is not None:
                            self._cancelled.add(self._active_response_id)
                        logger.info("Caller interrupted - stopping playback")
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
                    if (
                        rid
                        and self._active_response_id
                        and rid != self._active_response_id
                    ):
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
                                            f"to a {BRAND} specialist now. "
                                            "Keep it to one sentence."
                                        )
                                    },
                                }
                            )
                        )
                        await asyncio.sleep(3.5)
                        return
                    if name == "hang_up":
                        await self._tool_output(call_id, {"status": "hanging_up"})
                        await self._agent_hang_up()
                        return
                    logger.warning("Unknown tool: %s", name)

                elif etype == "error":
                    logger.error("Grok error: %s", event)

        except (CallClosedError, ConnectionClosed):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error receiving from Grok")
            raise
        finally:
            self._audio_queue.put_nowait(None)


async def handle_call(call: Call) -> None:
    try:
        # Warm the WebSocket before answer() so the greeting is not delayed.
        async with websockets.connect(
            GROK_REALTIME_URL,
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
        detail = body.decode("utf-8", errors="replace") if body else str(e)
        logger.error(
            "Grok WebSocket rejected (HTTP %s). Check XAI_API_KEY - %s",
            e.response.status_code,
            detail,
        )
        return

    if not escalate:
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
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("%s support escalation agent online", BRAND)

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            logger.info("Incoming call %s from %s", noti.call_id, noti.participant)
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
