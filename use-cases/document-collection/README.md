# Document Collection with Outbound Calls

**Omni Bank Inward Remittance Document Collection Voice Agent** built with [AgentDuet](https://pypi.org/project/agentduet/1.0.0/) and [Gemini Live](https://ai.google.dev/).

An automated outbound voice agent acting on behalf of **Omni Bank** to notify remote employees and global contractors of incoming foreign inward remittances ($4,250.00 USD), prompting them to log in to the compliance portal to declare Purpose Codes and upload supporting invoices before funds are credited.

## Overview

| Stage | Mode / Primitive | Action |
|---|---|---|
| Mint outbound leg | `session.make_call(Address.telco(dst))` | Creates outbound call descriptor |
| Dial employee | `call.dial(ring_time_seconds=25)` | Places SIP call leg to employee |
| **If Answered** | Gemini Live (`genai.aio.live`) | Bi-directional voice briefing + `confirm_notification_delivered` |
| **If Unanswered** | Disposition Logger | Logs `CALL_UNANSWERED` / `CALL_BUSY` as normal outcome; schedules retry |
| Wrap up | `end_call` + `call.disconnect()` | Clean call hangup after closing phrase finishes playing (3.5s grace) |

### What it does

- Places outbound compliance calls to account holders receiving foreign remittances
- Explains the incoming remittance amount, originating sender, and regulatory purpose code requirements
- Answers live questions regarding Purpose Codes (e.g. `P0802 Software Consultancy`, `P0807 Professional Services`) and upload deadlines (48 hours)
- Persists delivery audit records to `data/remittance_notifications.jsonl`
- **Gracefully handles unanswered calls** as a standard operational outcome without throwing exceptions

## Handling Outbound Dispositions (Normal Operation)

In automated outbound calling campaigns, unanswered calls are standard business outcomes:

| Dial Outcome | Disposition Code | Operational Handling |
|---|---|---|
| **Recipient Answers** | `CONNECTED` | Interactive Gemini Live voice notice delivers remittance details and records acknowledgement. |
| **Ring Timeout** | `CALL_UNANSWERED` | Recipient did not pick up within 25s. Logged to JSONL; queued for scheduled retry. |
| **Busy Signal** | `CALL_BUSY` | Line is engaged. Logged with timestamp for next retry window. |
| **Call Rejected** | `CALL_REJECTED` | Call was declined. Logged and excluded from immediate retries. |

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- Google Gemini API key (`GEMINI_API_KEY`) with Gemini Live access

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/use-cases/document-collection

python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Fill `.env`:

```env
AGENTDUET_API_KEY=your-connector-api-key
AGENTDUET_CONNECTOR_UUID=your-connector-uuid
AGENTDUET_SUBSCRIBER=+1XXXXXXXXXX      # Omni Bank outbound phone number
DESTINATION_NUMBER=+1XXXXXXXXXX        # Remote employee / contractor number
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=models/gemini-3.1-flash-live-preview
GEMINI_VOICE=Zephyr
SAMPLE_RATE=24000
```

## Run

```bash
python main.py
```

## How to Test

1. **Run the Agent:** Execute `python main.py`. The agent dials `DESTINATION_NUMBER`.
2. **Answer the Phone:** Pick up the incoming call.
3. **Conversational Compliance Briefing:**
   - **Agent:** *"Hi there! I'm calling from Omni Bank's remittance processing team regarding your incoming foreign remittance of $4,250.00 USD from Acme Global Corp. Do you have a quick moment?"*
   - **User:** *"Sure, what is it regarding?"*
   - **Agent:** *"Great! To release and credit your funds, please log in to portal.omnibank.com within 48 hours to declare your Purpose Code and upload your invoice."*
   - **User:** *"Which purpose code should I use for software development?"*
   - **Agent:** *"For software development and consulting services, select Purpose Code P0802 in the portal dropdown."*
   - **User:** *"Got it, I'll log in and upload it tonight."*
   - **Agent:** *(Invokes `confirm_notification_delivered` & `end_call`)* *"Thank you for banking with Omni Bank. Have a wonderful day!"*
4. **Speech Completion & Disconnect:** The agent waits until the closing greeting finishes playing through the receiver, then disconnects cleanly.
5. **Inspect Log:** View `data/remittance_notifications.jsonl` to verify the audit trail.

## Key APIs

| API | Role |
|---|---|
| `session.make_call(Address.telco(dst))` | Mint outbound call descriptor |
| `await call.dial(ring_time_seconds=25)` | Dial destination number |
| `call.callee.audio_stream()` | Read recipient microphone audio stream for Gemini Live |
| `await call.send_audio(chunk)` | Stream synthesized agent audio to recipient |
| `await call.clear_send_audio_buffer()` | Immediate interruption handling when recipient speaks |
| `await call.disconnect()` | Hang up call cleanly once closing speech completes |
| `data/remittance_notifications.jsonl` | Persistent audit trail of compliance notifications |

## Related

- [Gemini Live Integration](https://docs.agentduet.com/integrations/gemini-live)
- [Call Commands](https://docs.agentduet.com/concepts/call-commands)
- [SDK Reference](https://docs.agentduet.com/reference/api-reference)
