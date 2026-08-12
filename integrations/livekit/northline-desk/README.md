# AgentDuet x LiveKit (Gemini Live S2S)

Phone calls on **AgentDuet**, conversation on **LiveKit Agents** with
**Gemini Live** speech-to-speech. A small PCM bridge copies audio both ways.

```
Caller → AgentDuet (PCM) ↔ bridge ↔ LiveKit room ↔ Gemini Live (agent worker)
```

AgentDuet keeps the phone call; LiveKit only sees bridged audio.

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- [LiveKit](https://cloud.livekit.io/) project URL + API key/secret
- [Google AI API key](https://aistudio.google.com/apikey) for Gemini Live

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/integrations/livekit/northline-desk

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with AgentDuet, LiveKit, and `GOOGLE_API_KEY`.

## Run (two processes)

**Terminal 1: LiveKit agent (Gemini Live)**

```bash
python agent_worker.py start
```

**Terminal 2: AgentDuet phone bridge**

```bash
python main.py
```

Call your AgentDuet number. Noah should answer via Gemini Live through LiveKit.

## Layout

```
northline-desk/
├── main.py            # AgentDuet: answer call, create room, bridge PCM
├── agent_worker.py    # LiveKit Agents + google.realtime.RealtimeModel
├── requirements.txt
└── .env.example
```

## How it works

1. Inbound call → AgentDuet `answer()`.
2. `main.py` creates a LiveKit room and dispatches agent `agentduet-phone`.
3. Bridge joins the room, publishes caller PCM on an `AudioSource` track.
4. `agent_worker.py` runs Gemini Live S2S in that room.
5. Bridge subscribes to the agent audio track → `call.send_audio()`.
6. On barge-in (caller energy while agent audio is playing), clear AgentDuet's
   send buffer (and the LiveKit source queue).

## Related

- [Docs: LiveKit integration](https://docs.agentduet.com/integrations/livekit)
- [LiveKit Gemini Live plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/)
- [Pipecat sample](../../pipecat/hearthline-gas): another AgentDuet PCM transport
