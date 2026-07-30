# AgentDuet use-case samples

Runnable voice agents for common telephony patterns, built on
[`agentduet` 1.0.0b9](https://pypi.org/project/agentduet/1.0.0b9/).

Each use case is self-contained: `README.md`, `main.py`, `requirements.txt`,
and `.env.example`.

## Catalog

| Folder | What it does | AI model |
|---|---|---|
| [`customer-support`](./customer-support) | Answer FAQs, open tickets, escalate to a human | **OpenAI Realtime** |
| [`appointment-booking`](./appointment-booking) | Book against Google Calendar; confirm with booking IDs | **Gemini Live** |
| [`medical-pre-triage`](./medical-pre-triage) | Symptom intake and urgency guidance (not a diagnosis) | **Amazon Nova Sonic** |
| [`database-lookup`](./database-lookup) | CRM lookup by caller number, personalize, route | **Qwen Omni** |
| [`human-escalation`](./human-escalation) | AI-first talk; escalate with saved context | **Grok Voice** |
| [`viberider-payment-assistant`](./viberider-payment-assistant) | VibeRider driver payment Q&A from a fixed policy file | **Gemini Live** |

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
