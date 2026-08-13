"""
After-hours voicemail + OpenClaw → Slack — AgentDuet + Gemini Live.

AgentDuet owns the call. Gemini Live owns the conversation. OpenClaw delivers
Slack via `openclaw message send`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

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

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
MODEL = "models/gemini-3.1-flash-live-preview"
VOICE = "Zephyr"
OFFICE_NAME = "Northline Partners"
AGENT_NAME = "Riley"
VOICEMAIL_STORE = HERE / "voicemails" / "messages.jsonl"
HANDOFF_GRACE_SECONDS = float(os.getenv("HANDOFF_AUDIO_GRACE_SECONDS", "2.5"))

genai_client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
)

SYSTEM_INSTRUCTION = f"""Your name is {AGENT_NAME}. You are the after-hours phone
attendant for {OFFICE_NAME}. The office is closed.

Speak slowly and warmly — do not rush. On connect, greet the caller, say the
office is closed, and invite them to leave a message with:
1. Their name
2. A callback number (if different from the calling line)
3. What they need help with

Stay on the line until you have a name and a reason, or they clearly refuse.
If they only greet you, ask you to slow down, or chat, keep asking for a
voicemail. Do not hang up.

When you have a name and a reason, call leave_voicemail with a concise
transcript summary. Do not invent details. If they refuse to leave a message,
still call leave_voicemail with whatever they said (or "caller declined").

After leave_voicemail returns ok=true, thank them, say the team will follow up
on the next business day, finish that goodbye, then call end_call.
Never call end_call before leave_voicemail succeeds.

If they only want hours, tell them weekday hours (typically 9–17 local) and
still take a message.
Never claim you will transfer them to a person on this call.
Never place an outbound call.
"""


LEAVE_TOOL = types.FunctionDeclaration(
    name="leave_voicemail",
    description=(
        "Save the caller's after-hours message and notify the team on Slack "
        "via OpenClaw. Call once you have a clear message (name + reason at minimum)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "caller_name": {
                "type": "string",
                "description": "Caller's name as stated",
            },
            "callback_number": {
                "type": "string",
                "description": "Preferred callback number if they gave one",
            },
            "message": {
                "type": "string",
                "description": "Concise transcript summary of what they need",
            },
            "urgency": {
                "type": "string",
                "description": "low | normal | high (caller-stated urgency)",
            },
        },
        "required": ["message"],
    },
)

END_TOOL = types.FunctionDeclaration(
    name="end_call",
    description=(
        "End the call after the goodbye. Forbidden until leave_voicemail has "
        "returned ok=true on this call. If the caller declines, call "
        "leave_voicemail first with a declined/no-message summary."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def is_business_hours(now: Optional[datetime] = None) -> bool:
    if os.getenv("FORCE_AFTER_HOURS", "").strip() in ("1", "true", "True", "yes"):
        return False

    tz = ZoneInfo(os.getenv("BUSINESS_TZ", "UTC"))
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    start = _env_int("BUSINESS_HOURS_START", 9)
    end = _env_int("BUSINESS_HOURS_END", 17)
    days_raw = os.getenv("BUSINESS_DAYS", "0,1,2,3,4")
    days = {int(p.strip()) for p in days_raw.split(",") if p.strip()}
    if now.weekday() not in days:
        return False
    return start <= now.hour < end


def _format_slack_text(record: dict[str, Any]) -> str:
    return (
        f"*After-hours voicemail* (`{record.get('voicemail_id')}`)\n"
        f"*From:* {record.get('caller_name') or 'Unknown'}\n"
        f"*Callback:* `{record.get('callback_number') or record.get('from_number') or '—'}`\n"
        f"*Urgency:* {record.get('urgency') or 'normal'}\n"
        f"*When:* {record.get('received_at') or ''}\n"
        f"*Message:*\n>{record.get('message') or ''}"
    )


def notify_via_openclaw(record: dict[str, Any]) -> dict[str, Any]:
    """Post to Slack through OpenClaw's message CLI."""
    target = os.getenv("OPENCLAW_SLACK_TARGET", "").strip()
    if not target:
        return {
            "ok": False,
            "error": "OPENCLAW_SLACK_TARGET unset (e.g. channel:C01234567 or #incoming-calls)",
        }

    openclaw = os.getenv("OPENCLAW_BIN", "openclaw").strip() or "openclaw"
    if shutil.which(openclaw) is None and openclaw == "openclaw":
        return {
            "ok": False,
            "error": (
                "openclaw CLI not found on PATH. Install OpenClaw, connect Slack, "
                "then set OPENCLAW_BIN if needed."
            ),
        }

    cmd = [
        openclaw,
        "message",
        "send",
        "--channel",
        "slack",
        "--target",
        target,
        "--message",
        _format_slack_text(record),
    ]
    if os.getenv("OPENCLAW_DRY_RUN", "").strip() in ("1", "true", "True", "yes"):
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except Exception as exc:
        logger.exception("openclaw message send failed")
        return {"ok": False, "error": str(exc)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        logger.error("openclaw failed: %s", err)
        return {"ok": False, "error": err}

    logger.info("OpenClaw Slack notify ok for %s", record.get("voicemail_id"))
    return {
        "ok": True,
        "via": "openclaw",
        "target": target,
        "stdout": (proc.stdout or "").strip()[:500],
    }


def save_voicemail(record: dict[str, Any]) -> Path:
    VOICEMAIL_STORE.parent.mkdir(parents=True, exist_ok=True)
    with VOICEMAIL_STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return VOICEMAIL_STORE


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    from_number: str,
) -> dict[str, Any]:
    if name == "leave_voicemail":
        voicemail_id = f"vm_{uuid.uuid4().hex[:10]}"
        received_at = datetime.now(
            ZoneInfo(os.getenv("BUSINESS_TZ", "UTC"))
        ).isoformat()
        record = {
            "voicemail_id": voicemail_id,
            "received_at": received_at,
            "from_number": from_number,
            "caller_name": args.get("caller_name"),
            "callback_number": args.get("callback_number") or from_number,
            "message": args.get("message", ""),
            "urgency": args.get("urgency") or "normal",
        }
        path = save_voicemail(record)
        # Run OpenClaw CLI off the event loop.
        openclaw = await asyncio.to_thread(notify_via_openclaw, record)
        return {
            "ok": True,
            "voicemail_id": voicemail_id,
            "stored_at": str(path),
            "openclaw": openclaw,
            "slack_delivered": bool(openclaw.get("ok")),
        }

    if name == "end_call":
        return {"ok": True, "end": True}

    return {"ok": False, "error": f"unknown tool {name}"}


class VoicemailBridge:
    def __init__(self, call: Call, live: AsyncSession, from_number: str):
        self._call = call
        self._live = live
        self._from_number = from_number
        self._terminated = False
        self._end = False
        self._voicemail_ok = False
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def _hang_up(self) -> None:
        """Drop the PSTN call after goodbye audio has left the send buffer."""
        try:
            for _ in range(50):
                if await self._call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
        except CallClosedError:
            return

        self._terminated = True
        # Inbound agent-only calls are ANSWERED: call.disconnect is INVALID_COMMAND.
        result = await self._call.close()
        if result:
            logger.info("Call %s closed after end_call", self._call.id)
            return
        logger.error(
            "close after end_call failed: %s (%s)",
            result.error_message,
            result.error_code,
        )

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self._live.send_realtime_input(
            text=(
                f"The call is connected. Greet as {AGENT_NAME} from {OFFICE_NAME}. "
                f"Office is closed. Invite a short voicemail. "
                f"Calling line appears as {self._from_number}."
            )
        )
        self._tasks = [
            asyncio.create_task(self._to_model()),
            asyncio.create_task(self._from_model()),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

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
                                    logger.warning("Buffer full — drop audio")
                                except CallClosedError:
                                    return
                    if response.tool_call:
                        responses = []
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})
                            logger.info("tool %s(%s)", fc.name, args)
                            if fc.name == "end_call" and not self._voicemail_ok:
                                result = {
                                    "ok": False,
                                    "end": False,
                                    "error": (
                                        "leave_voicemail has not succeeded yet. "
                                        "Take or record a declined message first."
                                    ),
                                }
                            else:
                                result = await _dispatch_tool(
                                    fc.name,
                                    args,
                                    from_number=self._from_number,
                                )
                            if fc.name == "leave_voicemail" and result.get("ok"):
                                self._voicemail_ok = True
                            if result.get("end"):
                                self._end = True
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
                        if self._end:
                            await self._hang_up()
                            return
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("receive from Gemini failed")


async def handle_business_hours(call: Call) -> None:
    ring = _env_int("CONNECT_RING_SECONDS", 30)
    logger.info("Business hours — connecting caller to subscriber (ring=%ss)", ring)
    result = await call.connect(ring_time_seconds=ring)
    if not result:
        logger.error(
            "connect failed: %s (%s)",
            result.error_message,
            result.error_code,
        )
        if result.error_code == "CALL_UNANSWERED":
            await call.disconnect()
        return
    await call.close()
    logger.info("Connected and left call %s", call.id)


async def handle_after_hours(call: Call) -> None:
    from_number = call.participant.value if call.participant else "unknown"
    logger.info("After hours — OpenClaw voicemail for %s", from_number)

    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise SystemExit("Set GEMINI_API_KEY (or GOOGLE_API_KEY) for after-hours mode")

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[types.Tool(function_declarations=[LEAVE_TOOL, END_TOOL])],
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
        await VoicemailBridge(call, live, from_number).run()


async def handle_call(call: Call) -> None:
    if is_business_hours():
        await handle_business_hours(call)
    else:
        await handle_after_hours(call)


async def main() -> None:
    missing = [
        k
        for k in ("AGENTDUET_API_KEY", "AGENTDUET_CONNECTOR_UUID")
        if not os.getenv(k)
    ]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")
    if not os.getenv("OPENCLAW_SLACK_TARGET"):
        logger.warning(
            "OPENCLAW_SLACK_TARGET unset — leave_voicemail will fail Slack notify"
        )

    mode = (
        "business-hours pass-through"
        if is_business_hours()
        else "after-hours via OpenClaw → Slack"
    )
    logger.info("Northline OpenClaw desk online (%s)", mode)

    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:

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
