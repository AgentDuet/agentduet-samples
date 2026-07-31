# VibeRider Resolve (customer-support)

Voice support for **riders and drivers**: lost items and ride complaints.  
Pipedrive ticket created **after the call ends** (or mock JSON if Pipedrive is unset).

Caller phone comes from the inbound CLI (`call.participant` / notification).

Requires **AgentDuet `1.0.0b10`**, **Python 3.12+**, and **Amazon Nova 2 Sonic** on Bedrock.

## Supported cases

| Caller | Lost | Complaint categories |
|--------|------|----------------------|
| **Rider** | Item left in car (phone, bag, wallet, keys) | `rude_driver`, `unsafe_driving`, `dirty_vehicle`, `bad_route`, `refused_stop` |
| **Driver** | — | `abusive_rider`, `vehicle_damage` |

## Design: AI vs code

| Step | Owner |
|------|--------|
| Dialogue, role/intent, slot collection | **AI** (Nova 2 Sonic) |
| Ride lookup | **Code** `lookupRecentRide` |
| Case registration | **Code** `registerCase` (queues for hangup) |
| Pipedrive deal | **Code** `finalize_after_call` on hangup |
| Caller phone on ticket | **Code** from call CLI |

```
Caller speech ──► Amazon Nova 2 Sonic (talk + toolUse)
                      │
                      ▼
              tools.py (code)
                 ├─ lookupRecentRide  → data/recent_rides.json
                 └─ registerCase      → queue case
                      │
                      ▼ hangup
              finalize_after_call() → Pipedrive or data/cases/*.json
```

## Stack

| Piece | Choice |
|---|---|
| Voice AI | Amazon Nova 2 Sonic (`amazon.nova-2-sonic-v1:0`) via Bedrock bidirectional stream |
| CRM | Pipedrive (optional) or local mock JSON |

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | yes | Voice connector |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | yes | Bedrock SigV4 |
| `AWS_REGION` | no | Default `us-east-1` |
| `NOVA_MODEL_ID` / `NOVA_VOICE_ID` | no | Sonic model + voice |
| `AUDIO_SAMPLE_RATE` | no | Default `24000` (uplink + downlink) |
| `PIPEDRIVE_API_TOKEN` | no | Real CRM (else mock under `data/cases/`) |
| `PIPEDRIVE_COMPANY_DOMAIN` | no | Deal URLs |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | no | Notify when a case is finalized |

## Run

```bash
source .venv/bin/activate
python main.py
```

## Demo scripts

Rides in [`data/recent_rides.json`](data/recent_rides.json): **VR88421**, **VR88455**, **VR88490**, **VR88510**.

1. **Rider lost** — “I'm a rider. I left my black backpack on ride V R 88421.”  
   → Lost deal after hangup (`case_type=lost`).

2. **Rider complaint** — “I'm a rider. The driver was rude on ride 88455.”  
   → Complaint (`rude_driver`) for Amy / Alex.

3. **Driver complaint** — “I'm a driver. A rider spilled a drink and damaged the seat, ride 88421.”  
   → Complaint with `caller_role=driver`, category `vehicle_damage`.

## Layout

| File | Role |
|------|------|
| `main.py` | SessionManager + start Nova session |
| `nova_sonic.py` | AgentDuet ↔ Nova 2 Sonic bridge + tool dispatch |
| `prompts.py` | System prompt + greeting kickoff |
| `tools.py` | Nova tool schemas + `SupportTools` |
| `rides.py` + `data/recent_rides.json` | Mock rides |
| `pipedrive.py` | Deal + notes (or mock JSON) |
