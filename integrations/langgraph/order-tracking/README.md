# ParcelPilot order tracking (LangGraph)

Inbound voice agent for **order tracking** and gated order modifications.

- **AgentDuet** — telephony PCM
- **Gemini Live** — speech-to-speech dialogue + tool calls
- **LangGraph** — call-scoped order state, mock DB tools, fulfillment policy

No live store APIs and no WhatsApp/SMS setup. Seed orders live in [`data/orders.json`](data/orders.json).

Requires **AgentDuet `1.0.0b10`**, **Python 3.12+**, and a **Gemini API key**.

## Demo orders

| Order | Zip | Fulfillment | What you can demo |
|-------|-----|-------------|-------------------|
| **#1001** | `94107` | `unfulfilled` | Spoken status; address change or cancel |
| **#1002** | `10001` | `fulfilled` | Spoken status; modifications politely declined |

## Design: AI vs LangGraph

| Step | Owner |
|------|--------|
| Greeting, dialogue, short spoken replies | **Gemini Live** (system prompt) |
| Authenticate order id + zip | **LangGraph tool** `authenticate_order` → mock DB |
| Shipping status facts | **Mock DB** via tool result (`shipping_summary`) |
| Modification policy gate | **LangGraph tools** `check_fulfillment_status` / `change_shipping_address` / `cancel_order` |
| End call on goodbye | **LangGraph tool** `hang_up` → drain audio → `call.close()` |

```
Caller speech ──► Gemini Live (talk + toolUse)
                      │
                      ▼
              LangGraph OrderSession
                 ├─ authenticate_order      → data/orders.json
                 ├─ check_fulfillment_status
                 ├─ change_shipping_address → mock DB (unfulfilled only)
                 ├─ cancel_order            → mock DB (unfulfilled only)
                 └─ hang_up                 → close the call
```

PCM audio stays on the AgentDuet ↔ Gemini bridge. Graph state holds compact
order facts only (order id, auth, fulfillment) with a MemorySaver checkpoint
keyed by call id.

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
| `GEMINI_API_KEY` | yes | Gemini Live |
| `GEMINI_LIVE_MODEL` | no | Default `models/gemini-3.1-flash-live-preview` |

## Run

```bash
source .venv/bin/activate
python main.py
```

Call your AgentDuet connector number.

## Demo scripts

1. **Track + change (unfulfilled)** — “Order 1001, zip 94107.”  
   → Status in one or two sentences.  
   → “Change my address to 88 Folsom Street, San Francisco, California, 94105.”  
   → Address updated.

2. **Cancel (unfulfilled)** — Authenticate 1001, then “Please cancel the order.”  
   → Cancellation succeeds.

3. **Policy block (fulfilled)** — “Order 1002, zip 10001. Cancel it.”  
   → Status spoken; cancel politely declined.

4. **Hang up** — “Bye” / “Hang up the call.”  
   → Short goodbye, then the agent ends the call.

## Layout

| File | Role |
|------|------|
| `main.py` | SessionManager + Gemini Live bridge + tool dispatch |
| `graph.py` | LangGraph `OrderSession`, tools, checkpointed state |
| `orders.py` + `data/orders.json` | Mock order database |
| `prompts.py` | System prompt + opening greeting |

## Notes

- Restarting `main.py` reloads seed orders from disk (in-memory cancels/address edits reset).
