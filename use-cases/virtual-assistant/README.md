# Sarah — Apex Retail Virtual Assistant

Warm, professional phone support for the fictional store **Apex Retail**.
Answers from [`knowledge.md`](./knowledge.md) (shipping, returns-adjacent
policies, account basics).

Requires **AgentDuet `1.0.0b10`** and **OpenAI Realtime** via `openai-agents`.

## Overview

- Load `knowledge.md` into the Realtime system instructions
- Greet as Sarah from Apex Retail on answer (kickoff message)
- Brief 1–2 sentence answers from the knowledge base
- Optional: `capture_followup_email` writes JSON under `data/followups/`
- `hang_up` drains the send buffer and closes the call

## Stack

| Piece | Choice |
|---|---|
| Telephony | AgentDuet (`SessionManager`, direct PCM) |
| Voice AI | OpenAI Realtime (`gpt-realtime-1.5`) via `openai-agents` |
| Knowledge | `knowledge.md` (local file) |

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | yes | Voice connector |
| `OPENAI_API_KEY` | yes | Realtime voice |

## Run

```bash
source .venv/bin/activate
python main.py
```

## Demo questions (match `knowledge.md`)

Try these on a live call:

- How long does standard shipping take? Is it free?
- How much is overnight shipping?
- Can I cancel my order?
- I need to change my shipping address.
- An item is missing from my order.
- My package arrived damaged — what do I do?
- Why didn’t my promo code work?
- How is sales tax calculated?
- Can I get another copy of my receipt?
- Where’s the size guide?
- When do you restock sold-out items?
- Are your products authentic?
- I forgot my password.
- Do you store my card details?

Out-of-KB (follow-up email path): ask something Sarah cannot answer (e.g. a
specific order status without details she can look up) — she should take an
email and hang up. Check `data/followups/*.json`.

## Layout

| File | Role |
|------|------|
| `main.py` | SessionManager, OpenAI PCM bridge, barge-in, greeting kickoff |
| `prompts.py` | System prompt + greeting (loads `knowledge.md`) |
| `knowledge.md` | Fixed store policy knowledge base |
| `tools.py` | `capture_followup_email` / `hang_up` |
| `data/followups/` | Local follow-up JSON (runtime) |
