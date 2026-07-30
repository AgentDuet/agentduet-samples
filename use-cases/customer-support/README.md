# Customer Support

Inbound voice support agent: answers common questions, can file a local ticket,
and escalates to a human with `connect()` → `close()`.

## Overview

- Answer the call with OpenAI Realtime
- Tools: `create_ticket`, `escalate_to_human`, `hang_up`
- Tickets stored under `data/` as JSON
- Human handoff via `connect` / `close`

## Stack

| Piece | Choice |
|---|---|
| Voice AI | OpenAI Realtime (`gpt-realtime`) via `openai-agents` |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, OPENAI_API_KEY
python main.py
```

See [`.env.example`](./.env.example) for every variable this agent reads.
