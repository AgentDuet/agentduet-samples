"""OpenAI Agents function tools for Sarah (Apex Retail)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agents import function_tool

from agentduet import Call, CallClosedError

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
FOLLOWUPS_DIR = HERE / "data" / "followups"

_CTX: dict[str, Any] = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def reset_ctx(*, call: Call) -> None:
    _CTX.clear()
    _CTX["call"] = call
    _CTX["call_id"] = call.id


@function_tool
async def capture_followup_email(email: str, topic: str) -> str:
    """Save a follow-up email when Sarah cannot answer from the knowledge base.

    Call after the customer provides an email. Then confirm and hang_up.
    """
    addr = (email or "").strip()
    subject = (topic or "").strip() or "unspecified"
    if not addr or not _EMAIL_RE.match(addr):
        return (
            "ok=false. agent_speak_summary: I need a valid email address so a "
            "manager can follow up."
        )

    FOLLOWUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    call_id = _CTX.get("call_id") or "unknown"
    path = FOLLOWUPS_DIR / f"{stamp}_{call_id}.json"
    payload = {
        "email": addr,
        "topic": subject,
        "call_id": call_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Follow-up saved %s email=%s topic=%s", path.name, addr, subject)
    return (
        f"ok=true path={path.name}. "
        f"agent_speak_summary: Thanks — I've noted {addr} and a manager will "
        "follow up about that. Have a great day!"
    )


@function_tool
async def hang_up() -> str:
    """End the call after a brief goodbye. Drains the send buffer, then closes."""
    call: Optional[Call] = _CTX.get("call")
    if call is not None:
        try:
            for _ in range(40):
                if await call.get_send_audio_buffer_size() == 0:
                    break
                await asyncio.sleep(0.1)
            await call.close()
        except CallClosedError:
            pass
    return "Call ended."


SARAH_TOOLS = [capture_followup_email, hang_up]
