"""Local mock order database — no live store APIs."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).resolve().parent / "data" / "orders.json"

_ORDERS: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _ORDERS
    if _ORDERS is None:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        _ORDERS = {normalize_order_id(row["order_id"]): deepcopy(row) for row in raw}
    return _ORDERS


def reload_orders() -> None:
    """Reset in-memory mutations (useful for demos / tests)."""
    global _ORDERS
    _ORDERS = None
    _load()


def normalize_order_id(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D+", "", str(value))
    return digits.lstrip("0") or digits or str(value).strip().upper()


def normalize_zip(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D+", "", str(value))[:5]


def get_order(order_id: str) -> dict[str, Any] | None:
    return deepcopy(_load().get(normalize_order_id(order_id)))


def authenticate(order_id: str, zip_code: str) -> dict[str, Any]:
    order = _load().get(normalize_order_id(order_id))
    if not order:
        return {
            "ok": False,
            "authenticated": False,
            "error": "order_not_found",
            "agent_speak_summary": (
                "I couldn't find that order. Please try a test id like 1001 or 1002."
            ),
        }
    if normalize_zip(zip_code) != normalize_zip(order["zip_code"]):
        return {
            "ok": False,
            "authenticated": False,
            "error": "zip_mismatch",
            "agent_speak_summary": (
                "That zip code doesn't match the order. Please say the five-digit zip again."
            ),
        }
    return {
        "ok": True,
        "authenticated": True,
        "order_id": order["order_id"],
        "fulfillment_status": order["fulfillment_status"],
        "shipping_status": order["shipping_status"],
        "shipping_summary": order["shipping_summary"],
        "ship_to": deepcopy(order["ship_to"]),
        "items": list(order["items"]),
        "agent_speak_summary": order["shipping_summary"],
    }


def public_view(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": order["order_id"],
        "fulfillment_status": order["fulfillment_status"],
        "shipping_status": order["shipping_status"],
        "shipping_summary": order["shipping_summary"],
        "ship_to": deepcopy(order["ship_to"]),
        "items": list(order["items"]),
    }


def update_address(order_id: str, ship_to: dict[str, str]) -> dict[str, Any]:
    store = _load()
    key = normalize_order_id(order_id)
    order = store.get(key)
    if not order:
        return {
            "ok": False,
            "error": "order_not_found",
            "agent_speak_summary": "I couldn't find that order anymore.",
        }
    if order["fulfillment_status"] != "unfulfilled":
        return {
            "ok": False,
            "error": "policy_blocked",
            "fulfillment_status": order["fulfillment_status"],
            "agent_speak_summary": (
                "I can't change the address after fulfillment. "
                "Once it has shipped, the delivery address is locked."
            ),
        }

    required = ("line1", "city", "state", "zip")
    missing = [field for field in required if not (ship_to.get(field) or "").strip()]
    if missing:
        return {
            "ok": False,
            "error": "missing_fields",
            "missing": missing,
            "agent_speak_summary": (
                "I still need the street, city, state, and zip before I can update the address."
            ),
        }

    new_ship_to = {
        "line1": ship_to["line1"].strip(),
        "line2": (ship_to.get("line2") or "").strip(),
        "city": ship_to["city"].strip(),
        "state": ship_to["state"].strip().upper()[:2],
        "zip": normalize_zip(ship_to["zip"]),
    }
    order["ship_to"] = new_ship_to
    order["zip_code"] = new_ship_to["zip"]
    return {
        "ok": True,
        "order_id": order["order_id"],
        "ship_to": deepcopy(new_ship_to),
        "fulfillment_status": order["fulfillment_status"],
        "agent_speak_summary": (
            f"Done — I updated the shipping address for order {order['order_id']} "
            f"to {new_ship_to['line1']} in {new_ship_to['city']}."
        ),
    }


def cancel_order(order_id: str) -> dict[str, Any]:
    store = _load()
    key = normalize_order_id(order_id)
    order = store.get(key)
    if not order:
        return {
            "ok": False,
            "error": "order_not_found",
            "agent_speak_summary": "I couldn't find that order anymore.",
        }
    if order["fulfillment_status"] != "unfulfilled":
        return {
            "ok": False,
            "error": "policy_blocked",
            "fulfillment_status": order["fulfillment_status"],
            "agent_speak_summary": (
                "I can't cancel after fulfillment. "
                "Once the order has shipped, cancellation isn't available."
            ),
        }
    if order.get("cancelled"):
        return {
            "ok": True,
            "already_cancelled": True,
            "order_id": order["order_id"],
            "agent_speak_summary": f"Order {order['order_id']} is already cancelled.",
        }

    order["cancelled"] = True
    order["shipping_status"] = "cancelled"
    order["shipping_summary"] = (
        f"Order {order['order_id']} was cancelled before fulfillment and will not ship."
    )
    return {
        "ok": True,
        "cancelled": True,
        "order_id": order["order_id"],
        "fulfillment_status": order["fulfillment_status"],
        "agent_speak_summary": (
            f"All set — I cancelled order {order['order_id']}. It will not ship."
        ),
    }
