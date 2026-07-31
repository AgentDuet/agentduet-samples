# AI Agent with Human Escalation — VibeRider Support

**Alex** answers as **VibeRider** phone support on Grok Voice. When the caller is
stuck, frustrated, or asks for a person, the agent saves conversation context
and hands off with `connect()` → `close()`.

## Overview

- AI-first realtime conversation (Grok Voice)
- **VibeRider** support branding
- Tools: `save_context_and_escalate`, `hang_up`
- Context JSON written under `data/` for the human agent
- Barge-in uses `caller.audio_stream` + `clear_send_audio_buffer`
- Clean step-out after a successful `connect`

**State flow:** `NEW → LIVE` (`answer()`) → `connect()` (still `LIVE`) → agent `close()` → `TERMINATED` (or `close()` without handoff if resolved)

## Stack

| Piece | Choice |
|---|---|
| Voice AI | xAI Grok Voice (`grok-voice-think-fast-1.0`) |
| Realtime URL | `wss://api.x.ai/v1/realtime?model=…` |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, XAI_API_KEY, optional GROK_VOICE
python main.py
```

See [`.env.example`](./.env.example) for every variable this agent reads.

Uses `grok-voice-think-fast-1.0`, the Grok Realtime WebSocket URL, barge-in buffer
flush, and opens the Grok WebSocket before `answer()`.
