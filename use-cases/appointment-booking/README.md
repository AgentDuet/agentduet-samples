# Appointment Booking and Confirmation

Conversational booking against **Google Calendar**, spoken by **Gemini Live**.
After a successful book, a Telegram message includes the calendar event ID and
booking reference.

## Overview

- Multi-turn gathering (service, date, time)
- Availability from Google Calendar free/busy + existing events
- Book only after Calendar `events.insert` succeeds
- Confirmation message includes `event_id` and booking ID
- Fallback: `connect()` staff → `close()` when booking fails

**State flow:** `PENDING → ANSWERED →` close, or escalate `→ CONNECTED →` close

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Gemini Live (`gemini-3.1-flash-live-preview`) |
| Calendar | Google Calendar API (service account) |
| Confirm | Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` / `TELEGRAM_CUSTOMER_CHAT_ID`) |

## Google Calendar setup

1. Create a Google Cloud service account with Calendar API enabled.
2. Share the target calendar with the service account email (**Make changes to events**).
3. Download the JSON key and set paths in `.env` (see [`.env.example`](./.env.example)):
   - `GOOGLE_CALENDAR_CREDENTIALS=./calendar-service-account.json`
   - `GOOGLE_CALENDAR_ID=primary` (or the calendar id)

Without credentials the agent uses an in-memory demo calendar so you can still
exercise the voice loop locally.

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill AGENTDUET_*, GEMINI_API_KEY, optional Calendar/Telegram
python main.py
```
