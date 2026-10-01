# AgentDuet + n8n Restaurant Table Reservation

This reference implementation connects an **AgentDuet** voice agent with **Google Gemini Live** and **n8n** to automate restaurant table reservations over the telephone.

When a caller dials the restaurant, an AI hostess named **Bella** answers in real time, collects party details, triggers an n8n webhook, appends the reservation to a Google Sheet, and returns a confirmation code to the caller.

---

## Architecture

```text
[Telephone Caller] 
       ↕ (PSTN / SIP)
[AgentDuet Telephony Engine]
       ↕ (Bidirectional WebSocket: wss:// • 24 kHz LPCM)
[Voice Agent Runtime (Gemini Live)]
       ↓ (HTTPS POST Webhook)
[n8n 3-Node Workflow]
       ↓ (Append Row)
[Google Sheets ("Reservations" tab)]
       ↓ (JSON Response with Confirmation Code)
[Voice Agent confirms to Caller on the phone]
```

---

## Workflow in n8n (3 Simple Nodes)

The workflow consists of just 3 straightforward nodes:

1. **Reservation Webhook**: Catches the incoming reservation payload (`guest_name`, `party_size`, `reservation_time`, `phone_number`, `special_requests`).
2. **Insert into Reservations**: Appends a new row to your `Reservations` Google Sheet tab.
3. **Respond to AgentDuet**: Generates a confirmation code (for example, `RES-4921`) and returns it back to the voice agent in real time.

---

## Google Sheet Setup (Single Tab)

Create a Google Sheet and name the first tab **`Reservations`**. Add these column headers in Row 1:

| Timestamp | Guest_Name | Party_Size | Reservation_Time | Phone_Number | Special_Requests | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |

---

## Quickstart

### 1. Clone the repository and navigate to this folder

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/integrations/n8n
```

### 2. Set up your Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Import the Workflow into n8n

1. In your n8n workspace, click **Add Workflow** > **Import from File**.
2. Select [`n8n_workflow.json`](./n8n_workflow.json).
3. Double-click the **Insert into Reservations** node:
   * **Credential to connect with**: Select your Google Sheets credential.
   * **Document**: Select your spreadsheet.
   * **Sheet**: Select `Reservations`.
4. Click **Save** (top right) and set the workflow to **Active**.
5. Copy the **Production Webhook URL** from the Webhook node (e.g. `https://your-n8n.cloud/webhook/restaurant-reservation`).

### 4. Configure Environment Variables

Copy the example file:

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```ini
# AgentDuet credentials (from https://agentduet.com)
AGENTDUET_API_KEY=your_agentduet_api_key
AGENTDUET_CONNECTOR_UUID=your_connector_uuid

# Google Gemini Live API key (from https://aistudio.google.com)
GEMINI_API_KEY=your_gemini_api_key

# n8n Webhook URL (from Step 3)
N8N_RESERVATION_WEBHOOK_URL=https://your-n8n-instance.app.n8n.cloud/webhook/restaurant-reservation
```

---

## Testing

Run the included test suite to verify the client, tool declarations, and webhook connectivity:

```bash
python test_restaurant_reservation.py
```

Or run with pytest:

```bash
pytest test_restaurant_reservation.py -v
```

---

## Run Live

Start the telephony bridge:

```bash
python main.py
```

When a phone call arrives on your AgentDuet phone number:
1. Bella greets the caller:
   > *"Thank you for calling Bella Vista Bistro! My name is Bella. What date and time would you like to reserve a table for?"*
2. The caller specifies their party size and time.
3. Bella invokes the n8n webhook, saves the reservation to your Google Sheet, and speaks the confirmation code directly to the caller.
