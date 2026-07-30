# AI Agent with Human Escalation

Grok Voice handles the call first. When the caller is stuck, frustrated, or asks
for a person, the agent saves conversation context and hands off with
`connect()` → `close()`.

## Overview

- AI-first realtime conversation (Grok Voice)
- Tools: `save_context_and_escalate`, `hang_up`
- Context JSON written under `data/` for the human agent
- Clean step-out after a successful `connect`

**State flow:** `PENDING → ANSWERED → CONNECTED →` agent leaves (or close if resolved)

## Stack

| Piece | Choice |
|---|---|
| Voice AI | xAI Grok Voice (`grok-voice-latest`) |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, XAI_API_KEY, optional GROK_VOICE
python main.py
```

See [`.env.example`](./.env.example) for every variable this agent reads.
