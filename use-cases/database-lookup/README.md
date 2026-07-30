# AI Receptionist with Database Lookup

Answer the call, look up the caller in a local CRM (SQLite), personalize with
**Qwen Omni**, then route to a human when needed.

## Overview

- Lookup by caller phone on arrival
- Create a lead for unknown callers
- Intent-aware conversation with CRM context in the system prompt
- Handoff via `connect()` → `close()` when transfer is detected

**State flow:** `PENDING → ANSWERED → CONNECTED →` agent leaves

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Qwen Omni Realtime (`qwen3-omni-flash-realtime`) |
| CRM | SQLite (`data/crm.sqlite`) seeded with sample customers |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, DASHSCOPE_API_KEY
python main.py
```

See [`.env.example`](./.env.example) for every variable this agent reads.
