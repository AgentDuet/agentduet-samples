# Driver Payment Assistant

Driver-facing phone agent for the fictional taxi company **VibeRider**.
It answers **only** from [`policy.md`](./policy.md) (payouts, tips, fees).

## Overview

- Load `policy.md` into the Gemini Live system instruction
- Refuse questions outside the policy
- No calendar, CRM, or payment APIs — policy text only

## Demo questions (match `policy.md`)

Try these on a live call:

- When do I get paid? / What’s the weekly payout schedule?
- What’s the Instant Cash-Out fee? What’s the minimum / max / daily limit?
- Do you take a cut of tips?
- Why is my money still pending?
- What if Monday is a bank holiday?
- How long do fare disputes take?
- Where do I find my 1099 / tax forms?
- Can I get paid by PayPal or Venmo?

## Stack

| Piece | Choice |
|---|---|
| Telephony | AgentDuet (`SessionManager`, direct PCM) |
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
