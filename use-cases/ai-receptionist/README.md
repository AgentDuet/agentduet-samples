# Meridian Clinic AI Receptionist

Answer the call, look up the caller in CRM, personalize with **Qwen Omni**, then
route to a human when needed via `connect()` → `close()`.

Brand: **Meridian Clinic**.

## Overview

- Lookup by caller phone on arrival (`call.participant`)
- Known caller → greet by name; surface open tickets
- Unknown caller → create a lead; collect name with `update_lead_name`
- Tools: `get_open_tickets`, `record_intent`, `update_lead_name`, `transfer_to_human`
- Handoff: `connect()` rings original callee, then `close()` leaves the AI

**State flow:** `NEW → LIVE` (`answer()`) → `connect()` (still `LIVE`) → agent `close()` → `TERMINATED`

## CRM backends

| `CRM_BACKEND` | Backend |
|---|---|
| `json` (default) | Local `data/customers.json` + `data/leads.json` |
| `pipedrive` | Live Pipedrive when `PIPEDRIVE_API_TOKEN` + `PIPEDRIVE_COMPANY_DOMAIN` set |

### Seed customers (JSON)

| Phone | Name | Notes |
|---|---|---|
| `+65 XXXXXXXX` | Claudia | Known; 2 open tickets |
| `+65 XXXXXXXX` | Joy | 1 open billing ticket |
| `+65 XXXXXXXX` | Amy | No open tickets |
| `+65 XXXXXXXX` | Alex | Prefers sales |

Replace the placeholders in `data/customers.json` with your test CLI numbers before a known-caller demo.

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Qwen Omni Realtime (`qwen3.5-omni-flash-realtime`) |
| CRM | JSON (default) or Pipedrive |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env   # AGENTDUET_*, DASHSCOPE_API_KEY
python main.py
```

## Demo scripts

1. **Known caller** — call from Claudia’s seeded number (`+65 XXXXXXXX` in `data/customers.json`) → personalized greeting mentioning open tickets.
2. **New caller** — unknown number → lead in `data/leads.json`, ask for name.
3. **Human handoff** — say *"I need to speak to someone"* → `transfer_to_human` → `connect` / `close`.

## Layout

| File | Role |
|------|------|
| `main.py` | SessionManager, Qwen PCM bridge, tool dispatch, handoff |
| `prompts.py` | System prompt + Qwen tool specs |
| `crm.py` | JSON CRM + `create_crm_backend()` |
| `pipedrive_crm.py` | Optional Pipedrive backend |
| `data/customers.json` | Seed customers + tickets |
| `data/leads.json` | New callers |
| `data/reception_calls.json` | Call audit log (created at runtime) |
