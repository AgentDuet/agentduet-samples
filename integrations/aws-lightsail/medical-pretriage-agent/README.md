# Medical Pre-Triage Agent

Voice agent that answers inbound phone calls via [AgentDuet](https://agentduet.com), runs speech-to-speech triage with **Amazon Nova 2 Sonic** (`amazon.nova-2-sonic-v1:0`), and writes call transcripts to **Amazon CloudWatch Logs**. Run it in Docker on your **Lightsail instance** (VM).

```text
Caller ──► AgentDuet (telephony) ──► Docker on Lightsail instance
                                      ├─ Nova 2 Sonic (Bedrock bidirectional stream)
                                      └─ CloudWatch Logs (transcripts)
```

## Features

- Inbound call handling with AgentDuet `SessionManager` / `Call`
- Real-time PCM bridge to Nova 2 Sonic with barge-in (`clear_send_audio_buffer`)
- Medical pre-triage system prompt (symptoms, red flags, urgency guidance — not a diagnosis)
- Structured transcript events in CloudWatch (`USER` / `ASSISTANT` / lifecycle)
- Docker health endpoint at `/health`

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.12+ | Local runs without Docker |
| AgentDuet API key + connector UUID | [agentduet.com](https://agentduet.com) |
| AWS credentials | IAM with Bedrock bidirectional stream + CloudWatch Logs (see below) |
| Bedrock model access | Enable `amazon.nova-2-sonic-v1:0` in a supported region (`us-east-1`, `eu-north-1`, or `ap-northeast-1`) |
| Docker | On your laptop and/or Lightsail instance |

## Quick start (local)

```bash
cp .env.example .env
# fill AGENTDUET_* and AWS credentials

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --pre -r requirements.txt

export PYTHONPATH=src
python -m medical_pretriage.main
```

Or with Docker:

```bash
cp .env.example .env
docker compose up --build
```

Call the phone number attached to your AgentDuet connector. Health check:

```bash
curl http://127.0.0.1:8080/health
```

## Deploy on your Lightsail instance

SSH into the instance, then:

```bash
# once: install Docker if needed
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER   # then re-login

git clone <your-repo-url> medical-pretriage-agent
cd medical-pretriage-agent
cp .env.example .env
# edit .env with AGENTDUET_* and AWS credentials

docker compose up -d --build
docker compose logs -f
```

The agent opens outbound connections to AgentDuet and Bedrock; it does not need a public inbound port for calls. Port `8080` is only for optional `/health` checks.

## CloudWatch transcript observability

When `CLOUDWATCH_ENABLED=true`, each call writes to:

- Log group: `/medical-pretriage/transcripts` (configurable)
- Stream: `call/YYYY-MM-DD/<call_id>`

Event shape:

```json
{
  "event_type": "transcript",
  "call_id": "...",
  "participant": "+1555...",
  "subscriber": "...",
  "role": "USER",
  "text": "I've had a fever since yesterday",
  "generation_stage": "FINAL",
  "ts": "2026-07-17T03:30:00+00:00"
}
```

Lifecycle events use `role=SYSTEM` with `text=call_started` / `hangup`.

Tail logs:

```bash
aws logs tail /medical-pretriage/transcripts --follow --region us-east-1
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `AGENTDUET_API_KEY` | _(required)_ | AgentDuet API key |
| `AGENTDUET_CONNECTOR_UUID` | _(required)_ | Connector UUID |
| `AWS_REGION` | `us-east-1` | Bedrock + CloudWatch region |
| `NOVA_MODEL_ID` | `amazon.nova-2-sonic-v1:0` | Nova 2 Sonic model id |
| `NOVA_VOICE_ID` | `tiffany` | Sonic voice |
| `NOVA_ENDPOINTING_SENSITIVITY` | `HIGH` | Turn detection: `HIGH` / `MEDIUM` / `LOW` |
| `AUDIO_SAMPLE_RATE` | `16000` | PCM rate for AgentDuet + Sonic (8000/16000/24000) |
| `CLOUDWATCH_ENABLED` | `true` | Write transcripts to CloudWatch |
| `CLOUDWATCH_LOG_GROUP` | `/medical-pretriage/transcripts` | Log group name |
| `HEALTH_PORT` | `8080` | Probe port |

## Project layout

```text
src/medical_pretriage/
  main.py                 # AgentDuet SessionManager entry
  config.py               # env settings
  health.py               # /health endpoint
  agent/
    prompts.py            # CareLine pre-triage prompt
    nova_sonic.py         # Call ↔ Nova 2 Sonic bridge
  observability/
    cloudwatch.py         # transcript PutLogEvents
Dockerfile
docker-compose.yml
```

## Safety note

This agent provides **pre-triage guidance only**. It is not a licensed clinician, does not diagnose, and must instruct callers with emergency red flags to call emergency services immediately.

## References

- [AgentDuet SDK 1.0.0b9 (PyPI)](https://pypi.org/project/agentduet/1.0.0b9/)
- [AgentDuet docs](https://docs.agentduet.com)
- [Amazon Nova Sonic getting started](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-getting-started.html)
- [Lightsail instances](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-getting-started.html)
