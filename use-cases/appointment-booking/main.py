"""
Appointment Booking — AgentDuet + Gemini Live + Google Calendar.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

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

import httpx

from calendar_client import Booking, build_calendar

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
CALENDAR = build_calendar()
_LAST_BOOKING: dict[str, Optional[Booking]] = {"value": None}

genai_client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
)


def _upcoming_days(n: int = 5) -> list[date]:
    today = date.today()
    days: list[date] = []
    d = today
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def availability_blurb() -> str:
    lines = []
    for d in _upcoming_days():
        slots = CALENDAR.list_open_slots(d)
        if slots:
            lines.append(f"{d.isoformat()}: {', '.join(slots)}")
    return "\n".join(lines) or "No open slots in the next few weekdays."


LIST_TOOL = types.FunctionDeclaration(
    name="list_slots",
    description="List open appointment times for an ISO date (YYYY-MM-DD).",
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["date"],
    },
)

BOOK_TOOL = types.FunctionDeclaration(
    name="book_appointment",
    description=(
        "Book only after the caller explicitly confirms date, time, and service. "
        "Never invent a confirmation — wait for this tool's success response."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "time": {"type": "string", "description": "HH:MM 24h"},
            "service": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["date", "time", "service", "phone"],
    },
)

ESCALATE_TOOL = types.FunctionDeclaration(
    name="escalate_to_human",
    description="Transfer to a human scheduler when no slot works or the caller asks.",
    parameters={"type": "object", "properties": {}, "required": []},
)


async def _confirm_telegram(booking: Booking) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CUSTOMER_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    text = (
        f"*Appointment confirmed*\n"
        f"Booking ID: `{booking.booking_id}`\n"
        f"Calendar event ID: `{booking.event_id}`\n"
        f"Service: {booking.service}\n"
        f"When: {booking.start.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"Phone: `{booking.attendee_phone}`"
    )
    if booking.html_link:
        text += f"\nLink: {booking.html_link}"
    if not token or not chat:
        logger.warning(
            "Telegram skipped (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). %s", text
        )
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        )
        if resp.status_code >= 400:
            logger.error("Telegram send failed %s: %s", resp.status_code, resp.text)


async def _dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "list_slots":
        day = date.fromisoformat(args["date"])
        slots = CALENDAR.list_open_slots(day)
        return {"date": day.isoformat(), "slots": slots}

    if name == "book_appointment":
        try:
            booking = CALENDAR.book(
                day=date.fromisoformat(args["date"]),
                time_hhmm=args["time"],
                service=args["service"],
                phone=args["phone"],
            )
        except Exception as exc:
            logger.warning("Book failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        _LAST_BOOKING["value"] = booking
        await _confirm_telegram(booking)
        return {
            "ok": True,
            "booking_id": booking.booking_id,
            "event_id": booking.event_id,
            "when": booking.start.isoformat(),
            "service": booking.service,
        }

    if name == "escalate_to_human":
        return {"ok": True, "escalate": True}

    return {"ok": False, "error": f"unknown tool {name}"}


class GeminiBookingBridge:
    def __init__(self, call: Call, live: AsyncSession):
        self._call = call
        self._live = live
        self._escalate = False
        self._terminated = False
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def run(self) -> bool:
        """Returns True if the agent requested human escalation."""
        self._call.on_hangup(self._on_hangup)
        await self._live.send_realtime_input(
            text=(
                "Greet the caller briefly and help them book. "
                f"Known open slots:\n{availability_blurb()}"
            )
        )
        to_model = asyncio.create_task(self._to_model())
        from_model = asyncio.create_task(self._from_model())
        self._tasks = [to_model, from_model]
        await asyncio.gather(to_model, from_model, return_exceptions=True)
        return self._escalate

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
                            result = await _dispatch_tool(fc.name, args)
                            if result.get("escalate"):
                                self._escalate = True
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
                        if self._escalate:
                            return
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("receive from Gemini failed")


async def handle_call(call: Call) -> None:
    _LAST_BOOKING["value"] = None
    phone = call.participant.value if call.participant else "unknown"
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
            )
        ),
        tools=[types.Tool(function_declarations=[LIST_TOOL, BOOK_TOOL, ESCALATE_TOOL])],
        system_instruction=(
            "You book appointments by phone. Be concise. "
            "Always call list_slots before offering times. "
            "Only say a booking is confirmed after book_appointment returns ok=true; "
            "then read back the booking_id to the caller. "
            f"Caller's phone is {phone}. "
            "If nothing works or they ask for a person, call escalate_to_human."
        ),
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
        escalate = await GeminiBookingBridge(call, live).run()

    if _LAST_BOOKING["value"] and not escalate:
        await call.close()
        return

    # Human fallback
    try:
        await call.clear_send_audio_buffer()
    except CallClosedError:
        return
    connected = await call.connect(ring_time_seconds=40)
    if not connected:
        logger.error(
            "staff connect failed: %s (%s)",
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
        logger.info("Appointment booking agent online")

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
