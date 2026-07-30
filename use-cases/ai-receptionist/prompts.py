"""Prompt helpers for Meridian Clinic AI receptionist."""

from __future__ import annotations

import os
from typing import Any

COMPANY_NAME = os.getenv("COMPANY_NAME", "Meridian Clinic")


def build_system_prompt(
    *,
    customer: dict[str, Any] | None,
    open_tickets: list[dict[str, Any]],
    lead_created: bool,
    caller_number: str | None,
) -> str:
    lines = [
        f"You are the AI receptionist for {COMPANY_NAME}.",
        "You answer inbound phone calls professionally and concisely.",
        "Keep replies short and spoken — one or two sentences per turn.",
        f"Caller ID on file: {caller_number or 'unknown'}.",
        "Never ask for a callback number; the caller's phone is already known.",
        "",
        "Your goals:",
        "1. Greet the caller (by name if they are a known customer).",
        "2. Understand their intent: billing, sales, support, or general inquiry.",
        "3. Use get_open_tickets when a known customer may have account issues.",
        "4. Use record_intent once you understand what they need.",
        "5. If they want a person, are frustrated, or need something you cannot do, "
        "say you will connect them and call transfer_to_human.",
        "6. For unknown callers, ask their name and use update_lead_name.",
        "",
        "You may answer simple clinic questions yourself (hours, location, departments).",
        "Do not invent medical advice, appointments, or account data.",
        "Never claim you connected them without calling transfer_to_human.",
    ]

    if customer:
        lines.extend(
            [
                "",
                "KNOWN CUSTOMER (from CRM):",
                f"- Name: {customer.get('name')}",
                f"- Company: {customer.get('company', 'n/a')}",
                f"- Preferred department: {customer.get('preferred_department', 'general')}",
                f"- Customer id: {customer.get('id')}",
            ]
        )
        if customer.get("notes"):
            lines.append(f"- Notes: {customer.get('notes')}")
        if open_tickets:
            ticket_summary = "; ".join(
                f"{t.get('id')}: {t.get('subject')}" for t in open_tickets
            )
            lines.append(f"- Open tickets ({len(open_tickets)}): {ticket_summary}")
            lines.append(
                "Mention open tickets briefly in your greeting if relevant."
            )
    elif lead_created:
        lines.append("")
        lines.append(
            "UNKNOWN CALLER: a new lead record was created. "
            "Welcome them and ask how you can help. Collect their name with update_lead_name."
        )

    return "\n".join(lines)


def build_tool_specs() -> list[dict[str, Any]]:
    """Qwen Omni realtime tool definitions (OpenAI-compatible function schema)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_open_tickets",
                "description": "Fetch open support tickets for the known customer",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_intent",
                "description": "Record classified caller intent for routing logs",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "enum": ["billing", "sales", "support", "general"],
                        },
                        "notes": {"type": "string"},
                    },
                    "required": ["intent"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_lead_name",
                "description": "Save the caller name for a new lead",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transfer_to_human",
                "description": (
                    "Connect caller to a human agent. Say you will connect them, "
                    "then invoke this tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                        "department": {
                            "type": "string",
                            "enum": ["billing", "sales", "support", "general"],
                        },
                    },
                    "required": ["reason"],
                },
            },
        },
    ]
