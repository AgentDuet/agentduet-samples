# Vapi Integration with AgentDuet

Connect AgentDuet inbound phone calls directly to Vapi conversational AI agents.

AgentDuet provides telephony carrier connectivity and real-time audio streaming. Vapi orchestrates conversation logic, speech-to-text, LLM intelligence, and low-latency voice synthesis over its WebSocket transport (`provider: "vapi.websocket"`).

## Architecture

```
Phone Call (Caller)
       │
       ▼ (telephony audio)
   AgentDuet (Carrier & Media Engine)
       │
       ▼ (16 kHz 16-bit Linear PCM via WebSocket)
  Vapi Integration Bridge (Python)
       │
       ▼ (wss://api.vapi.ai/{call_id}/transport)
  Vapi Conversational AI Assistant
```

## Features

- **Telephony Carrier Control**: AgentDuet manages inbound phone numbers, SIP trunks, call answering, and hangup signals.
- **Low-Latency Audio Streaming**: 16 kHz 16-bit Mono Linear PCM streamed bidirectionally between caller and Vapi.
- **Instant Barge-In**: User speech triggers instant clearing of AgentDuet audio buffers (`call.clear_send_audio_buffer()`).
- **Farewell Audio Protection**: Ensures the assistant's final goodbye audio fully plays to the caller before terminating the carrier session.
- **Graceful Lifecycle Management**: Bidirectional termination handles caller hangup (`on_hangup`) and agent-initiated hangup (`hang` or `end-call`).

## Prerequisites

- Python 3.12+
- AgentDuet API key and connector UUID from [AgentDuet Console](https://agentduet.com)
- Vapi Private API Key and Assistant ID from [Vapi Dashboard](https://dashboard.vapi.ai)

## Setup

1. Clone or navigate to the sample directory:
   ```bash
   cd agentduet-samples/integrations/vapi
   ```

2. Create and activate a virtual environment:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install "agentduet==1.0.0" httpx websockets python-dotenv
   ```

4. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your credentials:
   ```ini
   AGENTDUET_API_KEY=your_agentduet_api_key
   AGENTDUET_CONNECTOR_UUID=your_connector_uuid
   VAPI_API_KEY=your_vapi_api_key
   VAPI_ASSISTANT_ID=your_vapi_assistant_id
   ```

5. Run the bridge:
   ```bash
   python main.py
   ```

6. Dial your AgentDuet phone number. The call connects directly to your Vapi assistant.
