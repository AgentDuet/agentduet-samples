# AgentDuet use-case samples

Runnable voice agents for common telephony patterns, built on
[`agentduet` 1.0.0](https://pypi.org/project/agentduet/1.0.0/).

Each use case is self-contained: `README.md`, `main.py`, `requirements.txt`,
and `.env.example`.

## Catalog

| Folder | What it does | AI model |
|---|---|---|
| [`medical-pre-triage`](./medical-pre-triage) | CareLine symptom intake and urgency guidance (not a diagnosis) | **Amazon Nova Sonic** |
| [`appointment-booking`](./appointment-booking) | HealthFirst Clinic booking with Amy; Calendar + optional Telegram | **Gemini Live** |
| [`customer-support`](./customer-support) | VibeRider Resolve — rider/driver lost items & complaints | **Amazon Nova Sonic** |
| [`ai-receptionist`](./ai-receptionist) | Meridian Clinic CRM lookup, personalize, human handoff | **Qwen Omni** |
| [`virtual-assistant`](./virtual-assistant) | Sarah — Apex Retail store policy Q&A from a fixed knowledge base | **OpenAI Realtime** |
| [`driver-payment-assistant`](./driver-payment-assistant) | VibeRider driver payment Q&A from a fixed policy file | **Gemini Live** |
| [`after-hours-voicemail`](./after-hours-voicemail) | Northline after-hours message + Slack notify; business-hours pass-through | **Gemini Live** |
| [`scam-prevention`](./scam-prevention) | SafeBank Guardian — silent call monitoring with private whisper fraud alerts | **Gemini Live / Whisper** |
| [`outbound-feedback`](./outbound-feedback) | TransitExpress — automated post-trip passenger survey with complaint triage | **Gemini Live** |
| [`document-collection`](./document-collection) | Omni Bank — outbound compliance document and purpose code collection | **Gemini Live** |
| [`human-escalation`](./human-escalation) | Apex Solutions — warm-transfer to human specialists with context preservation | **Gemini Live** |

## Prerequisites

- Python **3.12+**
- AgentDuet connector credentials (`AGENTDUET_API_KEY`, `AGENTDUET_CONNECTOR_UUID`)
- Provider keys listed in that folder's `.env.example`

```bash
cd use-cases/<name>
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
python main.py
```
