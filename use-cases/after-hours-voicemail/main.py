"""
After-hours voicemail + Slack notification — AgentDuet + Gemini Live.

During business hours: connect the caller to the subscriber line and leave
(pre-answer connect). After hours: AI answers, takes a message, saves it
locally, and posts a Slack Incoming Webhook notification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
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

Speak briefly and warmly. On connect, greet the caller, say the office is closed,
and invite them to leave a message with:
1. Their name
2. A callback number (if different from the calling line)
3. What they need help with

When you have a clear message (name + reason at minimum), call leave_voicemail
with a concise transcript summary. Do not invent details. After leave_voicemail
returns ok=true, thank them, say the team will follow up on the next business
day, then call end_call.

If the caller only wants to know hours, tell them weekday business hours from
the environment (typically 9–17 local) and offer to take a message anyway.
Never claim you will transfer them to a person on this call.
Never place an outbound call.
"""


LEAVE_TOOL = types.FunctionDeclaration(
    name="leave_voicemail",
    description=(
        "Save the caller's after-hours message and notify the team on Slack. "
        "Call once you have a clear message (name + reason at minimum)."
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
    description="End the call after the goodbye. Call only after leave_voicemail succeeds, or if the caller declines to leave a message.",
    parameters={"type": "object", "properties": {}, "required": []},
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def is_business_hours(now: Optional[datetime] = None) -> bool:
    """True when wall-clock time falls in configured business hours."""
    if os.getenv("FORCE_AFTER_HOURS", "").strip() in ("1", "true", "True", "yes"):
        return False

    tz_name = os.getenv("BUSINESS_TZ", "UTC")
    tz = ZoneInfo(tz_name)
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


async def notify_slack(payload: dict[str, Any]) -> dict[str, Any]:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.warning("SLACK_WEBHOOK_URL unset — skipping Slack notify")
        return {"ok": False, "error": "SLACK_WEBHOOK_URL not configured"}

    caller_name = payload.get("caller_name") or "Unknown"
    callback = payload.get("callback_number") or payload.get("from_number") or "—"
    message = payload.get("message") or ""
    urgency = payload.get("urgency") or "normal"
    voicemail_id = payload.get("voicemail_id") or "—"
    when = payload.get("received_at") or ""

    text = (
        f"*After-hours voicemail* (`{voicemail_id}`)\n"
        f"*From:* {caller_name}\n"
        f"*Callback:* `{callback}`\n"
        f"*Urgency:* {urgency}\n"
        f"*When:* {when}\n"
        f"*Message:*\n>{message}"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(webhook, json={"text": text})
            if resp.status_code >= 400:
                logger.error("Slack webhook failed %s: %s", resp.status_code, resp.text)
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.exception("Slack notify failed")
        return {"ok": False, "error": str(exc)}

    logger.info("Slack notified for voicemail %s", voicemail_id)
    return {"ok": True}


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
        received_at = datetime.now(ZoneInfo(os.getenv("BUSINESS_TZ", "UTC"))).isoformat()
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
        slack = await notify_slack(record)
        return {
            "ok": True,
            "voicemail_id": voicemail_id,
            "stored_at": str(path),
            "slack": slack,
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
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

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
                            result = await _dispatch_tool(
                                fc.name,
                                args,
                                from_number=self._from_number,
                            )
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
                            await asyncio.sleep(HANDOFF_GRACE_SECONDS)
                            self._terminated = True
                            try:
                                await self._call.disconnect()
                            except Exception:
                                logger.exception("disconnect after end_call failed")
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
    # Agent leaves; caller and callee stay connected.
    await call.close()
    logger.info("Connected and left call %s", call.id)


async def handle_after_hours(call: Call) -> None:
    from_number = call.participant.value if call.participant else "unknown"
    logger.info("After hours — AI voicemail for %s", from_number)

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

    mode = "business-hours pass-through" if is_business_hours() else "after-hours voicemail"
    logger.info("Northline after-hours desk online (%s)", mode)

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
