# Hearthline Gas & Energy (Pipecat)

Inbound voice agent for a fictional residential gas utility.

- **AgentDuet** — telephony PCM
- **Pipecat** — pipeline + custom AgentDuet transport
- **Gemini Live** — speech-to-speech (no STT/TTS stages)

Answers only from inline `FACTS`. Out of scope → point to `https://hearthlineenergy.example.com` and **stay on the line**. Hang up only when the caller asks to end. Gas leak / CO → leave the area and call emergency services in their area (never recite a number).

Requires **AgentDuet `1.0.0b10`**, **Python 3.12+**, and a **Gemini API key**.

## Design: AI vs Pipecat

| Step | Owner |
|------|--------|
| Greeting, dialogue, short spoken replies | **Gemini Live** (system prompt + kickoff) |
| Caller PCM → model / model PCM → caller | **Pipecat** `AgentDuetTransport` |
| Grounded FACTS (hours, address, towns) | **Inline prompt** (no tools / no DB) |
| End call on goodbye | **Pipecat tool** `hang_up` → drain audio → `call.close()` |

```
Caller speech ──► Pipecat AgentDuetTransport
                      │
                      ▼
              GeminiLiveLLMService
                 ├─ grounded answers from FACTS
                 └─ hang_up → close the call
```

PCM stays on the AgentDuet ↔ Pipecat ↔ Gemini Live path. No STT/TTS stages.

## Setup

Create the venv **outside** this directory. Pipecat pulls NLTK, which blocks imports of packages that live under your current working directory (a local `.venv` counts).

```bash
cd agentduet-samples/integrations/pipecat/hearthline-gas
python3.12 -m venv ~/venvs/hearthline-gas
source ~/venvs/hearthline-gas/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | yes | Voice connector |
| `GEMINI_API_KEY` | yes | Gemini Live |
| `GEMINI_LIVE_MODEL` | no | Default `models/gemini-3.1-flash-live-preview` |
| `GEMINI_LIVE_VOICE` | no | Default `Aoede` |

## Run

```bash
source ~/venvs/hearthline-gas/bin/activate
python hearthline_gas_assistant.py
```

Call your AgentDuet connector number.

## Demo scripts

1. **Greeting** — Answer the call.  
   → “Welcome to Hearthline Gas & Energy, I’m Riley. How can I help you today?”
   → Topic list only if they ask what she can help with.

2. **Hours / address / towns** — “What are your hours?” / “Where’s the office?”  
   → Grounded answers from FACTS.

3. **Open service / visit store** — “How do I open a new account?” / “Can I come in?”  
   → Online at the website, or visit the Mapleford office during hours.

4. **Meter access tip** — “What does meter access mean?”  
   → Keep the outdoor meter clear so techs can reach it safely.

5. **Out of scope** — “What’s my balance?” / “Transfer me to a person.”  
   → Website + stay on the line (no hang up).

6. **Hang up** — “Bye” / “Hang up the call.”  
   → Short goodbye, then the agent ends the call.

## Layout

| File | Role |
|------|------|
| `hearthline_gas_assistant.py` | SessionManager + AgentDuet transport + Gemini Live pipeline |
| `requirements.txt` | `agentduet==1.0.0b10`, `pipecat-ai[google]`, `python-dotenv` |
| `.env.example` | Connector + Gemini keys |

## Notes

- Keep AgentDuet and Pipecat at 24 kHz mono PCM for Gemini Live.
- If you see NLTK `Blocked import of regex from current working directory`, recreate the venv outside this folder and reactivate it.
- Emergency guidance must never invent or speak a phone number.
