# Feedback Collection with Outbound Calls

**TransitExpress Post-Trip Feedback Agent** built with [AgentDuet](https://pypi.org/project/agentduet/1.0.0/) and [Gemini Live](https://ai.google.dev/).

An automated outbound voice agent calls passengers after a completed bus trip (e.g. from **New York City to Washington, D.C.**) to collect structured feedback:
1. **Comfort Rating:** *"On a scale of 1 to 5, how would you rate your comfort during the ride?"*
2. **Punctuality:** *"Did the bus depart and arrive on time?"*
3. **Driver & Cleanliness:** *"How satisfied were you with the driver's service and the cleanliness of the bus?"*
4. **Closing & Clean Hangup:** *"Thank you for your feedback. Have a safe day!"* (disconnects cleanly after speech completes).

## Overview

| Stage | Mode / Primitive | Action |
|---|---|---|
| Mint outbound leg | `session.make_call(Address.telco(dst))` | Creates outbound call descriptor |
| Dial passenger | `call.dial(ring_time_seconds=25)` | Places SIP call leg to passenger |
| **If Answered** | Gemini Live (`genai.aio.live`) | Bi-directional voice survey + `record_feedback` tool |
| **If Unanswered** | Disposition Logger | Logs `CALL_UNANSWERED` / `CALL_BUSY` as normal outcome |
| Wrap up | `end_call` + `call.disconnect()` | Call ends cleanly after speech playback finishes |

### What it does

- Places outbound calls to passenger lists after trip completion
- Streams bi-directional 24 kHz audio to **Gemini Live** for natural, human-like voice interaction
- Listens for passenger responses and invokes structured function calling (`record_feedback`)
- Persists structured survey outcomes and verbatim complaints to `data/feedback_results.jsonl`
- **Gracefully handles unanswered calls** as a standard business outcome rather than an error

## Handling Outbound Call Outcomes (Normal Operation)

In automated outbound calling, a significant portion of placed calls will naturally go unanswered or encounter busy signals.

Rather than treating unanswered calls as system exceptions, the agent treats all call dispositions as **standard operational outcomes**:

| Dial Outcome | Disposition Code | Operational Handling |
|---|---|---|
| **Passenger Answers** | `CONNECTED` | Runs live Gemini Live voice survey and records structured ratings. |
| **Ring Timeout** | `CALL_UNANSWERED` | Passenger did not pick up within 25s. Logged to JSONL; queued for optional SMS survey fallback. |
| **Busy Signal** | `CALL_BUSY` | Line is engaged. Logged with timestamp for scheduled retry window. |
| **Call Rejected** | `CALL_REJECTED` | Call was declined. Logged and excluded from immediate retries. |

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- Google Gemini API key (`GEMINI_API_KEY`) with Gemini Live access

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/use-cases/outbound-feedback

python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Fill `.env`:

```env
AGENTDUET_API_KEY=your-connector-api-key
AGENTDUET_CONNECTOR_UUID=your-connector-uuid
AGENTDUET_SUBSCRIBER=+1XXXXXXXXXX      # Originating line
DESTINATION_NUMBER=+1XXXXXXXXXX        # Passenger phone number
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

1. **Place Outbound Test Call:** Run `python main.py`. The agent connects to AgentDuet and dials `DESTINATION_NUMBER`.
2. **Answer the Phone:** Pick up your phone when it rings.
3. **Conversational Survey:**
   - **Agent:** *"Hi there! I'm calling from TransitExpress's virtual support team about your bus ride from New York to D.C. Do you mind if I ask you a couple of quick questions?"*
   - **Passenger:** *"Sure, go ahead."*
   - **Agent:** *"Great, thanks! On a scale of 1 to 5, how would you rate your comfort during the ride?"*
   - **Passenger:** *"I'd give it a 4."*
   - **Agent:** *"Got it. Did the bus depart and arrive on time?"*
   - **Passenger:** *"Yes, it was right on time."*
   - **Agent:** *"Understood. How satisfied were you with the driver's service and the cleanliness of the bus?"*
   - **Passenger:** *"Very satisfied, the driver was helpful and the bus was clean."*
   - **Agent:** *(Invokes `record_feedback` & `end_call`)* *"Thank you so much for your feedback. Have a wonderful day!"*
4. **Tool Execution:** Gemini Live executes `record_feedback(comfort_rating=4, on_time=true, driver_and_cleanliness="Very satisfied, the driver was helpful and the bus was clean")`.
5. **Speech Completion & Disconnect:** The agent waits until the full closing phrase finishes playing out to your ear, then executes `call.disconnect()`.
6. **Inspect Log:** View `data/feedback_results.jsonl` to verify the structured record.

## Key APIs

| API | Role |
|---|---|
| `session.make_call(Address.telco(dst))` | Mint outbound call descriptor |
| `await call.dial(ring_time_seconds=25)` | Dial destination number |
| `call.callee.audio_stream()` | Read passenger audio stream for Gemini Live |
| `await call.send_audio(chunk)` | Play Gemini Live voice to passenger |
| `await call.clear_send_audio_buffer()` | Immediate interruption handling when passenger speaks |
| `await call.disconnect()` | Hang up call cleanly once closing speech completes |
| `data/feedback_results.jsonl` | Persistent structured feedback storage |

## Related

- [Gemini Live Integration](https://docs.agentduet.com/integrations/gemini-live)
- [Call Commands](https://docs.agentduet.com/concepts/call-commands)
- [SDK Reference](https://docs.agentduet.com/reference/api-reference)
