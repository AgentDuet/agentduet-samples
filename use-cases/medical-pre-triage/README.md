# Medical Pre-Triage

Phone intake agent that collects symptoms, checks for red flags, and gives an
urgency recommendation. It is **guidance only — not a diagnosis**.

## Overview

- Answer with Amazon Nova Sonic (speech-to-speech)
- One short question per turn; clear urgency outcome
- Emergency red flags → tell the caller to hang up and call emergency services
- Barge-in via `clear_send_audio_buffer()`

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Amazon Nova 2 Sonic (`amazon.nova-2-sonic-v1:0`) |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AGENTDUET_*, AWS_*
python main.py
```

See [`.env.example`](./.env.example). Enable Bedrock access for
`amazon.nova-2-sonic-v1:0` in your region.
