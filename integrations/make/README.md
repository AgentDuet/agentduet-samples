# Hotel Reservations Voice Agent: AgentDuet + Gemini Live + Make.com

A conversational realtime voice reservation system for hospitality that connects AgentDuet telephony with Google Gemini Live speech-to-speech AI and Make.com automation scenarios for live room inventory lookup and booking confirmations.

## System Architecture

```
                               ┌────────────────────────────────────────────────────────┐
                               │                    Guest Telephone                     │
                               │                Inbound Phone Connection                │
                               └───────────────────────────┬────────────────────────────┘
                                                           │
                                                           ▼
                                        ┌──────────────────────────────────────┐
                                        │ AgentDuet Telephone Connection       │
                                        │ - Bidirectional 24kHz PCM streaming  │
                                        │ - Sub-1ms buffer flush on barge-in   │
                                        └──────────────────┬───────────────────┘
                                                           │
                                                           ▼
                                        ┌──────────────────────────────────────┐
                                        │ Gemini Live Speech Model             │
                                        │ - Natural conversational voice       │
                                        │ - Dynamic intent & date recognition  │
                                        │ - Function calling (Make.com tools)  │
                                        └──────────────────┬───────────────────┘
                                                           │
                                ┌──────────────────────────┴──────────────────────────┐
                                │                                                     │
                                ▼                                                     ▼
                  ┌───────────────────────────┐                         ┌───────────────────────────┐
                  │ Tool: check_availability  │                         │ Tool: create_booking      │
                  │ - Webhook to Make.com     │                         │ - Webhook to Make.com     │
                  │ - Reads Google Sheets     │                         │ - Appends Google Sheets   │
                  │ - Returns rates & options │                         │ - Sends confirmation email│
                  └─────────────┬─────────────┘                         └─────────────┬─────────────┘
                                │                                                     │
                                └──────────────────────────┬──────────────────────────┘
                                                           │
                                                           ▼
                                        ┌──────────────────────────────────────┐
                                        │ Guest Dialogue Experience            │
                                        │ - Presents options side by side      │
                                        │ - Honest if fully booked             │
                                        │ - Confirms booking code verbally     │
                                        └──────────────────────────────────────┘
```

## Conversational Flow

1. **Inbound Call Greeting**:
   AgentDuet answers the call and nudges Gemini Live to deliver the warm opening hotel phone greeting, asking for check-in date, check-out date, and number of guests.
2. **Mid-Call Live Availability Check**:
   Before stating that any room is open, Gemini Live triggers the `check_room_availability` tool. The agent queries Make.com's custom webhook and waits for the actual response.
3. **Multi-Room Presentation**:
   When room options are returned, Gemini presents them side by side with their nightly rates (for example, *"Deluxe rooms at $150 a night, or Executive Suites at $250 a night"*) so the guest can choose.
4. **Honest Inventory Disclosure**:
   If no rooms are available for the requested dates, the agent tells the guest honestly rather than fabricating availability, and offers to check alternative dates.
5. **Guest Details Collection**:
   Once the guest selects a room type, Gemini confirms the selection and asks for their full name and email address.
6. **Make.com Execution**:
   Gemini invokes the `create_hotel_booking` tool:
   - Make.com adds a row to Google Sheets, marking those dates and room type as unavailable for future lookups.
   - Make.com sends an automated confirmation email to the guest containing the booking details and reference code.
7. **Verbal Confirmation & Polite Closing**:
   The agent confirms the booking verbally on the call, states that the confirmation email is on its way, asks if there is anything else it can help with, and ends the call naturally.

## Project Scope & Constraints

* **Inbound Calls Only**: Designed for guests dialing in to make reservations.
* **New Bookings Only**: Covers new reservations (no changes or cancellations).
* **No Payment Collection**: Secures the booking without capturing credit card numbers over voice.

## Directory Structure

* [`hotel_reservation_agent.py`](hotel_reservation_agent.py): Gemini Live voice agent bridge, system instruction, and Make.com tool dispatch.
* [`make_client.py`](make_client.py): HTTP client for Make.com custom webhooks with mock inventory mode.
* [`main.py`](main.py): AgentDuet telephony server handling incoming telephone calls with Gemini Live.
* [`make_scenario_blueprint.json`](make_scenario_blueprint.json): Blueprint definition for Make.com scenarios (Availability and Booking).
* [`test_make_integration.py`](test_make_integration.py): Automated test suite.
* [`.env.example`](.env.example): Environment variable template.

## Quickstart

1. **Virtual Environment Setup**:
   ```bash
   cd agentduet-samples/integrations/make
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   ```bash
   cp .env.example .env
   ```
   Add your AgentDuet connector credentials, `GEMINI_API_KEY`, and Make.com webhook URLs.

3. **Run Unit Tests**:
   ```bash
   python test_make_integration.py
   ```

4. **Start Telephony Service**:
   ```bash
   python main.py
   ```
