# OpenClaw — After-hours Voicemail → Slack

AgentDuet ↔ [OpenClaw](https://docs.openclaw.ai/) sample.

**AgentDuet** owns the phone call. **Gemini Live** owns the conversation.
**OpenClaw** delivers Slack via `openclaw message send`.

Install and connect OpenClaw **before** this app. There is no OpenClaw pip package.
See [Docs: OpenClaw integration](https://docs.agentduet.com/integrations/openclaw)
for install, gateway, and Slack setup.

## Overview

| Mode | Behavior |
|---|---|
| Business hours | `connect()` → `close()` (pass-through; OpenClaw idle) |
| After hours | Gemini tools → save JSONL → `openclaw message send --channel slack` |

```
Caller → AgentDuet → Gemini Live
                         ↓ leave_voicemail
              openclaw message send --channel slack
                         ↓
                      Slack channel
```

No outbound dial. No live human escalate on the after-hours path.

Companion without OpenClaw (direct Slack webhook):
[`use-cases/after-hours-voicemail`](../../../use-cases/after-hours-voicemail).

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- [Gemini API key](https://aistudio.google.com/apikey)
- [OpenClaw](https://docs.openclaw.ai/) installed with **Slack** connected
  ([Slack channel setup](https://docs.openclaw.ai/channels/slack))
- Bot invited to the target Slack channel

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/integrations/openclaw/after-hours-voicemail

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`:

```env
AGENTDUET_API_KEY=...
AGENTDUET_CONNECTOR_UUID=...
GEMINI_API_KEY=...
OPENCLAW_SLACK_TARGET=channel:C0123456789   # or #incoming-calls
FORCE_AFTER_HOURS=1                         # while testing in the daytime
```

Verify OpenClaw can post:

```bash
openclaw message send --channel slack \
  --target "$OPENCLAW_SLACK_TARGET" \
  --message "AgentDuet OpenClaw smoke test"
```

## Run

```bash
python main.py
```

Call your AgentDuet number, leave a short message, and confirm Slack receives
an *After-hours voicemail* post from OpenClaw.

## Layout

```
after-hours-voicemail/
├── main.py            # SessionManager + Gemini + OpenClaw notify
├── requirements.txt
└── .env.example
```

## Related

- [Docs: OpenClaw integration](https://docs.agentduet.com/integrations/openclaw)
- [OpenClaw install](https://docs.openclaw.ai/install/)
- [OpenClaw Slack](https://docs.openclaw.ai/channels/slack)
- [OpenClaw message CLI](https://docs.openclaw.ai/cli/message)
- [Use-case baseline (Slack webhook)](../../../use-cases/after-hours-voicemail)
