"""
MCP tool server for OmniBank phone servicing.

Spawned over stdio by main.py. Exposes fake bank tools the voice agent calls to
verify the caller and service fees. Everything is in-memory and fictitious.
Swap this server to change agent capabilities without touching Gemini wiring.

Demo caller: Ava Chen, card ending 4821.
"""

from __future__ import annotations

import random
import re

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("omnibank-servicing")

CURRENCY = "SGD"
WAIVER_CAP = 50.00

# Fake customer book. `last4` = last 4 of the card; `fees` = outstanding, waivable fees.
CUSTOMERS = {
    "c001": {
        "name": "Ava Chen",
        "last4": "4821",
        "tier": "Platinum",
        "card_type": "OmniBank Platinum Rewards",
        "balance": 1830.20,
        "fees": {
            "late_fee": {"label": "Late payment fee", "amount": 30.00},
            "interest_fee": {"label": "Interest charge", "amount": 45.50},
        },
    },
}

ESCALATIONS: list[dict] = []


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _last4(digits: str) -> str:
    """Last 4 digits from whatever is passed (full number, spaces, or dashes)."""
    return re.sub(r"\D", "", digits or "")[-4:]


def _find(full_name: str, last4: str):
    name, l4 = _norm(full_name), _last4(last4)
    for cid, c in CUSTOMERS.items():
        if _norm(c["name"]) == name and c["last4"] == l4:
            return cid
    return None


@mcp.tool()
def authenticate_customer(full_name: str, last4: str) -> dict:
    """Verify the caller by matching full name against card digits (server takes
    the last 4 itself). On success also returns outstanding fees, balance, and
    the agent's waiver limit."""
    cid = _find(full_name, last4)
    if not cid:
        return {
            "matched": False,
            "message": (
                "Name and card number don't match. "
                "Re-check the spelling and digits, then retry."
            ),
        }
    c = CUSTOMERS[cid]
    fees = [
        {"type": k, "label": v["label"], "amount": v["amount"]}
        for k, v in c["fees"].items()
    ]
    return {
        "matched": True,
        "customer_id": cid,
        "name": c["name"],
        "tier": c["tier"],
        "card_type": c["card_type"],
        "balance": c["balance"],
        "currency": CURRENCY,
        "outstanding_fees": fees,
        "waiver_authority_max": WAIVER_CAP,
    }


@mcp.tool()
def get_account_summary(customer_id: str) -> dict:
    """Return the authenticated caller's card, balance, outstanding fees, and waiver limit."""
    c = CUSTOMERS.get(customer_id)
    if not c:
        return {"error": "Unknown customer_id. Authenticate first."}
    fees = [
        {"type": k, "label": v["label"], "amount": v["amount"]}
        for k, v in c["fees"].items()
    ]
    return {
        "name": c["name"],
        "card_type": c["card_type"],
        "balance": c["balance"],
        "currency": CURRENCY,
        "outstanding_fees": fees,
        "waiver_authority_max": WAIVER_CAP,
    }


@mcp.tool()
def process_fee_waiver(customer_id: str, fee_types: list[str]) -> dict:
    """Waive one or more fees (by `type` from the summary). Enforces the waiver
    limit: if the requested total exceeds it, nothing is waived."""
    c = CUSTOMERS.get(customer_id)
    if not c:
        return {"error": "Unknown customer_id. Authenticate first."}
    requested, unknown = [], []
    for ft in fee_types or []:
        key = _norm(ft).replace(" ", "_")
        if key in c["fees"]:
            requested.append(key)
        else:
            unknown.append(ft)
    if unknown or not requested:
        return {
            "approved": False,
            "reason": "unknown_fee_types",
            "unknown": unknown,
            "available": list(c["fees"].keys()),
        }
    total = round(sum(c["fees"][k]["amount"] for k in requested), 2)
    if total > WAIVER_CAP:
        return {
            "approved": False,
            "reason": "exceeds_waiver_authority",
            "requested_total": total,
            "waiver_authority_max": WAIVER_CAP,
            "per_fee": {k: c["fees"][k]["amount"] for k in requested},
            "currency": CURRENCY,
        }
    waived = [
        {"type": k, "label": c["fees"][k]["label"], "amount": c["fees"][k]["amount"]}
        for k in requested
    ]
    for k in requested:
        c["balance"] = round(c["balance"] - c["fees"][k]["amount"], 2)
        del c["fees"][k]
    return {
        "approved": True,
        "waived": waived,
        "waived_total": total,
        "new_balance": c["balance"],
        "currency": CURRENCY,
    }


@mcp.tool()
def record_escalation(
    customer_id: str, summary: str, requested_fees: list[str] | None = None
) -> dict:
    """Log an escalation to a human supervisor and return a reference number."""
    ref = f"ESC-{random.randint(1000, 9999)}"
    ESCALATIONS.append(
        {
            "reference": ref,
            "customer_id": customer_id,
            "summary": summary,
            "requested_fees": requested_fees or [],
        }
    )
    return {
        "recorded": True,
        "reference": ref,
        "message": f"Escalation {ref} logged for a supervisor to review.",
    }


if __name__ == "__main__":
    mcp.run()
