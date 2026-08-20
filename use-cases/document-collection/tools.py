"""Remittance compliance notification recording, audit trails, and disposition tracking."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "data" / "remittance_notifications.jsonl"


@dataclass
class RemittanceNotificationRecord:
    """Structured record of an outbound foreign remittance compliance notification attempt."""

    remittance_id: str
    recipient_phone: str
    amount_display: str
    sender_name: str
    bank_name: str = "Omni Bank"
    call_status: str = "COMPLETED"  # "COMPLETED", "INCOMPLETE", "UNANSWERED", "BUSY", "REJECTED"
    disposition: str = "CONNECTED"  # "CONNECTED", "CALL_UNANSWERED", "CALL_BUSY", "RECIPIENT_HANGUP"
    dial_time: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    acknowledged: bool = False
    user_intent: Optional[str] = None
    call_duration_seconds: float = 0.0

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))


def save_remittance_record(
    record: RemittanceNotificationRecord,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    """Append structured notification record to JSONL storage."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")
        logger.info(
            "💾 Saved %s notification record for %s (Status: %s, Disposition: %s) to %s",
            record.remittance_id,
            record.recipient_phone,
            record.call_status,
            record.disposition,
            log_path.name,
        )
    except Exception as exc:
        logger.warning("Failed to persist remittance record: %s", exc)


def handle_confirm_notification_tool(
    remittance_id: str,
    recipient_phone: str,
    amount_display: str,
    sender_name: str,
    args: dict[str, Any],
    duration_seconds: float = 0.0,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[dict[str, Any], RemittanceNotificationRecord]:
    """Execute Gemini Live confirm_notification_delivered tool call."""
    acknowledged = bool(args.get("acknowledged", True))
    user_intent = args.get("user_intent", "Acknowledged compliance portal requirement")

    record = RemittanceNotificationRecord(
        remittance_id=remittance_id,
        recipient_phone=recipient_phone,
        amount_display=amount_display,
        sender_name=sender_name,
        call_status="COMPLETED",
        disposition="CONNECTED",
        acknowledged=acknowledged,
        user_intent=str(user_intent) if user_intent else None,
        call_duration_seconds=round(duration_seconds, 2),
    )

    save_remittance_record(record, log_path=log_path)
    return {
        "status": "success",
        "remittance_id": remittance_id,
        "acknowledged": acknowledged,
    }, record


def log_outbound_disposition(
    remittance_id: str,
    recipient_phone: str,
    amount_display: str,
    sender_name: str,
    disposition_code: str,
    log_path: Path = DEFAULT_LOG_PATH,
) -> RemittanceNotificationRecord:
    """Log an unanswered, busy, or rejected outbound call as a normal operational outcome."""
    record = RemittanceNotificationRecord(
        remittance_id=remittance_id,
        recipient_phone=recipient_phone,
        amount_display=amount_display,
        sender_name=sender_name,
        call_status="UNANSWERED",
        disposition=disposition_code,
        call_duration_seconds=0.0,
    )
    save_remittance_record(record, log_path=log_path)
    return record
