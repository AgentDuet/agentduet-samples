# OpenClaw — Personal phone assistant

Flagship AgentDuet ↔ [OpenClaw](https://docs.openclaw.ai/) pairing.

**AgentDuet** owns the phone call. **OpenClaw** owns the assistant (calendar,
weather, gold price, web, messaging). **Gemini Live** is speech in/out only.

```
Caller → AgentDuet → Gemini Live (STT / TTS)
                         ↓ each caller turn
              POST /v1/chat/completions
              model: openclaw/default
                         ↓
              OpenClaw agent + tools / skills
              (Google Calendar, web, channels, …)
                         ↓ spoken reply
              Gemini speaks OpenClaw's text → caller
```

Ask on the call:

- “What’s on my calendar today?”
- “When’s my next meeting?”
- “What’s the gold price?”
- “What’s the weather in Austin?”
- Everyday questions your OpenClaw agent can already answer in chat

A thinner notify-only sample (Slack via CLI) lives in
[`after-hours-voicemail`](../after-hours-voicemail).

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- [Gemini API key](https://aistudio.google.com/apikey)
- [OpenClaw Gateway](https://docs.openclaw.ai/gateway) running with
  [chat completions](https://docs.openclaw.ai/gateway/openai-http-api) enabled
- Calendar / web / weather as you already configured them **in OpenClaw**
  (this sample does not talk to Google Calendar or weather APIs itself)

## OpenClaw gateway

On the OpenClaw host:

1. Gateway up: `openclaw gateway status` (default `http://127.0.0.1:18789`).
2. Enable the OpenAI-compatible endpoint in OpenClaw config:

```json5
{
  gateway: {
    http: {
      endpoints: {
        chatCompletions: { enabled: true },
      },
    },
  },
}
```

3. Restart the gateway if needed.
4. Optional: install a calendar skill and complete OAuth in OpenClaw
   (`openclaw skills search calendar`). Web/weather use OpenClaw’s existing
   tools — smoke-test those in the OpenClaw dashboard chat first.
5. Smoke-test the same path the phone will use:

```bash
curl -sS "$OPENCLAW_GATEWAY_URL/v1/models" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN"

curl -sS "$OPENCLAW_GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw/default",
    "user": "call:smoke",
    "messages": [{"role":"user","content":"What meetings do I have today?"}]
  }'
```

Keep the gateway on loopback, Tailscale, or an SSH tunnel. The bearer token is
full operator access. Do not expose it on the public internet.

Lightsail: tunnel from the laptop that runs this sample, or run `main.py` on
the instance next to the gateway:

```bash
ssh -L 18789:127.0.0.1:18789 ubuntu@<lightsail-ip>
```

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/integrations/openclaw/personal-agent

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
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=...
OPENCLAW_MODEL=openclaw/default
```

## Run

```bash
python main.py
```

Call your AgentDuet number. Ada greets from OpenClaw, then each thing you say
is a gateway chat turn. Calendar and live facts come from OpenClaw tools, not
from Gemini.

| Ask / do | Expect |
|---|---|
| (connect) | Short greeting from OpenClaw, spoken by Gemini |
| “What’s on my calendar today?” | OpenClaw calendar skill → spoken summary |
| “What’s the gold price?” / weather | OpenClaw web/tools → spoken answer |
| Follow-up on the same call | Same OpenClaw session (`user=call:<id>`) |
| “Thanks, bye” | Goodbye, then `end_call` |

## Layout

```
personal-agent/
├── main.py                # AgentDuet + Gemini speech layer
├── openclaw_gateway.py    # POST /v1/chat/completions
├── requirements.txt
└── .env.example
```

Python talks to OpenClaw over HTTP. There is no OpenClaw pip package and no
`openclaw` CLI in this sample.

## Related

- [Docs: OpenClaw integration](https://docs.agentduet.com/integrations/openclaw)
- [OpenClaw chat completions](https://docs.openclaw.ai/gateway/openai-http-api)
- [Gemini Live](https://docs.agentduet.com/integrations/gemini-live)
- [Notify-only Slack sample](../after-hours-voicemail)
