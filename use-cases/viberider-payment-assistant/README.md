# VibeRider Payment Assistant

Simple driver-facing phone agent for the fictional taxi company **VibeRider**.
It answers **only** from [`policy.md`](./policy.md) (payouts, tips, fees).

## Overview

- Load `policy.md` into the Gemini Live system instruction
- Refuse questions outside the policy
- No calendar, CRM, or payment APIs — policy text only

## Example questions this covers

- How do drivers get paid?
- Do you take a cut of tips?
- When is the weekly payout?
- What is the instant payout fee?

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Gemini Live |
| Knowledge | `policy.md` (local file) |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, GEMINI_API_KEY
python main.py
```

See [`.env.example`](./.env.example) for every variable this agent reads.
