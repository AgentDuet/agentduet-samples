"""Google Calendar adapter with an in-memory fallback for local demos."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo(os.getenv("BUSINESS_TZ", "America/New_York"))
SLOT_HOURS = [9, 10, 11, 14, 15, 16]  # local hour starts offered
SLOT_MINUTES = 0
DURATION_MIN = 30


@dataclass
class Booking:
    booking_id: str
    event_id: str
    service: str
    start: datetime
    end: datetime
    attendee_phone: str
    html_link: Optional[str] = None


class CalendarBackend:
    """Interface used by the voice agent tools."""

    def list_open_slots(self, day: date, *, limit: int = 6) -> list[str]:
        raise NotImplementedError

    def book(
        self,
        *,
        day: date,
        time_hhmm: str,
        service: str,
        phone: str,
        summary: Optional[str] = None,
    ) -> Booking:
        raise NotImplementedError


class MemoryCalendar(CalendarBackend):
    def __init__(self) -> None:
        self._taken: set[str] = set()  # "YYYY-MM-DD|HH:MM"

    def list_open_slots(self, day: date, *, limit: int = 6) -> list[str]:
        out: list[str] = []
        for h in SLOT_HOURS:
            hhmm = f"{h:02d}:{SLOT_MINUTES:02d}"
            key = f"{day.isoformat()}|{hhmm}"
            if key not in self._taken:
                out.append(hhmm)
            if len(out) >= limit:
                break
        return out

    def book(
        self,
        *,
        day: date,
        time_hhmm: str,
        service: str,
        phone: str,
        summary: Optional[str] = None,
    ) -> Booking:
        key = f"{day.isoformat()}|{time_hhmm}"
        if key in self._taken or time_hhmm not in self.list_open_slots(day, limit=20):
            raise ValueError(f"Slot unavailable: {day} {time_hhmm}")
        self._taken.add(key)
        hour, minute = map(int, time_hhmm.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
        end = start + timedelta(minutes=DURATION_MIN)
        booking_id = f"BK-{uuid.uuid4().hex[:8].upper()}"
        event_id = f"mem_{uuid.uuid4().hex}"
        return Booking(
            booking_id=booking_id,
            event_id=event_id,
            service=service,
            start=start,
            end=end,
            attendee_phone=phone,
        )


class GoogleCalendar(CalendarBackend):
    def __init__(self, credentials_path: Path, calendar_id: str) -> None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/calendar"]
        creds = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=scopes
        )
        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._calendar_id = calendar_id

    def _busy_ranges(self, day: date) -> list[tuple[datetime, datetime]]:
        start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ)
        end = start + timedelta(days=1)
        body = {
            "timeMin": start.astimezone(timezone.utc).isoformat(),
            "timeMax": end.astimezone(timezone.utc).isoformat(),
            "timeZone": str(TZ),
            "items": [{"id": self._calendar_id}],
        }
        result = self._svc.freebusy().query(body=body).execute()
        busy = result["calendars"][self._calendar_id].get("busy", [])
        ranges: list[tuple[datetime, datetime]] = []
        for b in busy:
            ranges.append(
                (
                    datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ),
                    datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ),
                )
            )
        return ranges

    def list_open_slots(self, day: date, *, limit: int = 6) -> list[str]:
        busy = self._busy_ranges(day)
        out: list[str] = []
        for h in SLOT_HOURS:
            start = datetime(day.year, day.month, day.day, h, SLOT_MINUTES, tzinfo=TZ)
            end = start + timedelta(minutes=DURATION_MIN)
            if any(start < b_end and end > b_start for b_start, b_end in busy):
                continue
            if start < datetime.now(TZ):
                continue
            out.append(f"{h:02d}:{SLOT_MINUTES:02d}")
            if len(out) >= limit:
                break
        return out

    def book(
        self,
        *,
        day: date,
        time_hhmm: str,
        service: str,
        phone: str,
        summary: Optional[str] = None,
    ) -> Booking:
        if time_hhmm not in self.list_open_slots(day, limit=20):
            raise ValueError(f"Slot unavailable: {day} {time_hhmm}")
        hour, minute = map(int, time_hhmm.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
        end = start + timedelta(minutes=DURATION_MIN)
        booking_id = f"BK-{uuid.uuid4().hex[:8].upper()}"
        body = {
            "summary": summary or f"{service} ({booking_id})",
            "description": (
                f"Booking ID: {booking_id}\nPhone: {phone}\nService: {service}"
            ),
            "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
            "extendedProperties": {
                "private": {"booking_id": booking_id, "phone": phone}
            },
        }
        created = (
            self._svc.events()
            .insert(calendarId=self._calendar_id, body=body)
            .execute()
        )
        return Booking(
            booking_id=booking_id,
            event_id=created["id"],
            service=service,
            start=start,
            end=end,
            attendee_phone=phone,
            html_link=created.get("htmlLink"),
        )


def build_calendar() -> CalendarBackend:
    creds = os.getenv("GOOGLE_CALENDAR_CREDENTIALS")
    cal_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    if creds and Path(creds).is_file():
        logger.info("Using Google Calendar API (%s)", cal_id)
        return GoogleCalendar(Path(creds), cal_id)
    logger.warning(
        "GOOGLE_CALENDAR_CREDENTIALS missing — using in-memory demo calendar"
    )
    return MemoryCalendar()
