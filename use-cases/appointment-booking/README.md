# Appointment Booking — HealthFirst Clinic (Amy)

Phone booking agent for the fictional **HealthFirst Clinic/Hospital**. **Amy**
greets callers on Gemini Live, checks open slots, books only after explicit
confirm, and optionally sends a Telegram confirmation.

## Overview

- Agent: **Amy** at **HealthFirst Clinic**
- Collects name, phone, date, time, appointment type (general checkup /
  specialist / follow-up) — never re-asks details already given
- Availability via `list_slots` during the call; `book_appointment` only after
  the caller confirms
- Calendar: Google Calendar API when credentials are set, otherwise an
  in-memory demo calendar
- Optional Telegram confirm (skipped silently if tokens are missing)
- Fallback: `connect()` staff → `close()` when the caller needs a human

**State flow:** `PENDING → ANSWERED →` close, or escalate `→ CONNECTED →` close

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Gemini Live (`gemini-3.1-flash-live-preview`) |
| Calendar | Google Calendar API (optional) / in-memory fallback |
| Confirm | Telegram (optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) |

## Demo script

Call the connector number. Amy should open with something like:

> Hi, thank you for calling HealthFirst Clinic. I'm Amy, your appointment
> assistant…

Try one-shot patient intros so Amy does not re-ask known fields:

| Caller says | Expected |
|---|---|
| "Hi, I'm Claudia from HealthFirst. I want a general checkup tomorrow at 10 AM. My number is +65 XXXXXXXX." | Acknowledge Claudia + slot; `list_slots`; ask confirm; book |
| "Hi, I'm Joy. Specialist appointment next Monday at 2 PM, phone +65 XXXXXXXX." | Fill missing pieces only; confirm before book |
| "I'm Amy, follow-up Friday morning, +65 XXXXXXXX." | Offer morning slots from `list_slots`, then confirm |
| "Hi, Alex here — book me something this week." | Ask only for missing type / time / phone (+65 XXXXXXXX) |

After a successful book, Amy reads back the booking ID. If Telegram env vars are
set, a confirmation message includes booking ID + calendar event ID.

## Google Calendar setup (optional)

1. Create a Google Cloud service account with Calendar API enabled.
2. Share the target calendar with the service account email (**Make changes to events**).
3. Download the JSON key and set paths in `.env` (see [`.env.example`](./.env.example)):
   - `GOOGLE_CALENDAR_CREDENTIALS=./calendar-service-account.json`
   - `GOOGLE_CALENDAR_ID=primary` (or the calendar id)
   - Optional: `BUSINESS_TZ=UTC` (or your clinic timezone) for local slot hours

Without credentials the agent uses an in-memory demo calendar so you can still
exercise the voice loop locally.

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill AGENTDUET_*, GEMINI_API_KEY, optional Calendar/Telegram
python main.py
```
