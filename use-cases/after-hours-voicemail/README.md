# After-hours Voicemail + Slack

**Northline Partners** after-hours desk on AgentDuet with **Gemini Live**.

During business hours the call is connected to the subscriber line and the agent
leaves. After hours, Riley answers, takes a message, saves it locally, and posts
to a **Slack Incoming Webhook**.

## Overview

| Mode | Behavior | State flow |
|---|---|---|
| Business hours | `connect()` → `close()` (pass-through to human line) | `NEW → LIVE → TERMINATED` |
| After hours | `answer()` → Gemini voicemail → Slack notify → `disconnect()` | `NEW → LIVE → TERMINATED` |

What it does after hours:

- Greets as Riley from Northline Partners
- Collects name, callback number, and message
- Writes a JSONL record under `voicemails/messages.jsonl`
- Notifies Slack via Incoming Webhook
- Confirms and ends the call

No outbound dial. No live human escalate on the after-hours path.

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- [Gemini API key](https://aistudio.google.com/apikey) (after-hours path)
- [Slack Incoming Webhook](https://api.slack.com/messaging/webhooks) URL

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/use-cases/after-hours-voicemail

python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Fill `.env`:

```env
AGENTDUET_API_KEY=...
AGENTDUET_CONNECTOR_UUID=...
GEMINI_API_KEY=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Optional — defaults shown
BUSINESS_TZ=UTC
BUSINESS_HOURS_START=9
BUSINESS_HOURS_END=17
BUSINESS_DAYS=0,1,2,3,4

# Force voicemail path while testing during the day
# FORCE_AFTER_HOURS=1
```

## Run

```bash
python main.py
```

Call your AgentDuet number. With `FORCE_AFTER_HOURS=1`, leave a short message
and confirm a Slack post appears in your channel.

## How it works

1. On inbound, `is_business_hours()` checks local wall clock in `BUSINESS_TZ`.
2. **Business hours:** bare `connect()` rings the subscriber; on success the
   agent `close()`s so caller and callee stay connected.
3. **After hours:** Gemini Live answers. Tools:
   - `leave_voicemail` — append JSONL + Slack webhook
   - `end_call` — goodbye grace, then `disconnect()`

## Key APIs

| Piece | Role |
|---|---|
| `call.connect()` / `call.close()` | Business-hours pass-through |
| `call.answer()` / `caller.audio_stream()` / `send_audio()` | After-hours voice loop |
| `leave_voicemail` tool | Persist + Slack notify |
| Slack Incoming Webhook | Team notification |

## Related

- [Docs: After-hours Voicemail](https://docs.agentduet.com/use-cases/after-hours-voicemail)
- [OpenClaw variant (Slack via OpenClaw)](../../integrations/openclaw/after-hours-voicemail)
- [Call commands](https://docs.agentduet.com/concepts/call-commands)
