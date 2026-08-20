"""Feedback recording, structured storage, and disposition tracking for outbound feedback."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "data" / "feedback_results.jsonl"


@dataclass
class TripFeedbackRecord:
    """Structured record of an outbound passenger survey attempt."""

    trip_id: str
    passenger_phone: str
    call_status: str  # "COMPLETED", "INCOMPLETE", "UNANSWERED", "BUSY", "REJECTED"
    disposition: str  # "CONNECTED", "CALL_UNANSWERED", "CALL_BUSY", "PASSENGER_HANGUP", etc.
    origin: str = "New York City"
    destination: str = "Washington, D.C."
    dial_time: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    comfort_rating: Optional[int] = None
    on_time: Optional[bool] = None
    driver_and_cleanliness: Optional[str] = None
    complaint_logged: bool = False
    complaint_details: Optional[str] = None
    call_duration_seconds: float = 0.0

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))


def save_feedback_record(
    record: TripFeedbackRecord,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    """Append structured feedback record to JSONL storage."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")
        logger.info(
            "💾 Saved Trip %s (%s -> %s) feedback record (status: %s, disposition: %s) to %s",
            record.trip_id,
            record.origin,
            record.destination,
            record.call_status,
            record.disposition,
            log_path.name,
        )
    except Exception as exc:
        logger.warning("Failed to persist feedback record: %s", exc)


def handle_record_feedback_tool(
    trip_id: str,
    passenger_phone: str,
    args: dict[str, Any],
    duration_seconds: float = 0.0,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[dict[str, Any], TripFeedbackRecord]:
    """Execute Gemini Live record_feedback tool call."""
    try:
        comfort_rating = int(args.get("comfort_rating", 5))
    except (ValueError, TypeError):
        comfort_rating = 5

    on_time = bool(args.get("on_time", True))
    driver_and_cleanliness = str(args.get("driver_and_cleanliness", "Satisfied"))
    complaint = args.get("complaint_details", "")
    complaint_str = str(complaint).strip() if complaint else None
    complaint_logged = bool(complaint_str) or not on_time or (comfort_rating is not None and comfort_rating <= 2)

    record = TripFeedbackRecord(
        trip_id=trip_id,
        passenger_phone=passenger_phone,
        call_status="COMPLETED",
        disposition="CONNECTED",
        origin="New York City",
        destination="Washington, D.C.",
        comfort_rating=comfort_rating,
        on_time=on_time,
        driver_and_cleanliness=driver_and_cleanliness,
        complaint_logged=complaint_logged,
        complaint_details=complaint_str,
        call_duration_seconds=round(duration_seconds, 2),
    )

    save_feedback_record(record, log_path=log_path)
    return {
        "status": "success",
        "trip_id": trip_id,
        "comfort_rating": comfort_rating,
        "on_time": on_time,
        "driver_and_cleanliness": driver_and_cleanliness,
        "complaint_logged": complaint_logged,
    }, record


def log_outbound_disposition(
    trip_id: str,
    passenger_phone: str,
    disposition_code: str,
    log_path: Path = DEFAULT_LOG_PATH,
) -> TripFeedbackRecord:
    """Log an unanswered, busy, or rejected outbound call as a normal operational outcome."""
    record = TripFeedbackRecord(
        trip_id=trip_id,
        passenger_phone=passenger_phone,
        call_status="UNANSWERED",
        disposition=disposition_code,
        origin="New York City",
        destination="Washington, D.C.",
        call_duration_seconds=0.0,
    )
    save_feedback_record(record, log_path=log_path)
    return record
