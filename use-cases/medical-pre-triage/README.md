# Medical Pre-Triage

Phone intake agent (**CareLine**) that collects symptoms, checks for red flags,
and gives an urgency recommendation. It is **guidance only — not a diagnosis**.

## Overview

- Answer with Amazon Nova 2 Sonic (speech-to-speech) over a direct PCM bridge
- One short question per turn; clear urgency outcome
- Emergency red flags → tell the caller to hang up and call emergency services
- Barge-in via `clear_send_audio_buffer()`
- Transcripts and barge-in events log to stdout (INFO/DEBUG)

## Stack

| Piece | Choice |
|---|---|
| Telephony | AgentDuet (`SessionManager`, `process_call`) |
| Voice AI | Amazon Nova 2 Sonic (`amazon.nova-2-sonic-v1:0`) |
| Audio | 24 kHz PCM (configurable via `AUDIO_SAMPLE_RATE`) |

## Layout

| File | Role |
|---|---|
| `main.py` | AgentDuet entry; answers inbound calls |
| `nova_sonic.py` | Bidirectional Nova 2 Sonic bridge |
| `prompts.py` | CareLine system prompt + call-start kickoff |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, AWS_*
python main.py
```

See [`.env.example`](./.env.example). Enable Bedrock model access for
`amazon.nova-2-sonic-v1:0` in your region. Credentials come from the standard
AWS environment variables (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, or
an equivalent credentials provider).
