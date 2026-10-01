# Cab Company Lost and Found: AgentDuet + Amazon Nova 2 Sonic + n8n + Google Sheets

A real-time conversational voice automation system for taxi and rideshare companies that pairs **AgentDuet** telephony with **Amazon Nova 2 Sonic** (`amazon.nova-2-sonic-v1:0` on AWS Bedrock) for bidirectional speech-to-speech, **n8n** workflows for backend orchestration, and **Google Sheets** for persistent record storage.

The voice persona is **Maya**, a warm and professional customer service agent who manages driver found-item reports, passenger lost-item claims, and open case verifications.

## Architecture

```
                               ┌────────────────────────────────────────────────────────┐
                               │                    AgentDuet Phone                     │
                               │                   Single Telephony No.                 │
                               └───────────┬────────────────────────────────┬───────────┘
                                           │                                │
                       Inbound Driver Call │                                │ Inbound Passenger Call
                                           ▼                                ▼
                        ┌───────────────────────────────────────────────────────┐
                        │        Amazon Nova 2 Sonic Voice Agent Bridge         │
                        │        (24 kHz Bidirectional LPCM Audio Stream)        │
                        │ - Pre-intake open case lookup via n8n & Google Sheets │
                        │ - Realtime spoken conversation & barge-in handling    │
                        │ - Structured intake extraction on call completion     │
                        └───────────────────────────┬───────────────────────────┘
                                                    │
                                                    │ Structured JSON POST
                                                    ▼
                                       ┌─────────────────────────────────┐
                                       │    n8n Workflow Webhooks        │
                                       │    (Intake, Lookup, Status)     │
                                       └────────────────┬────────────────┘
                                                        │
                         ┌──────────────────────────────┴──────────────────────────────┐
                         ▼                                                             ▼
             ┌────────────────────────┐                                   ┌────────────────────────┐
             │ Google Sheets:         │                                   │ Google Sheets:         │
             │ Found_Items            │                                   │ Lost_Items             │
             └───────────┬────────────┘                                   └───────────┬────────────┘
                         │                                                             │
                         └──────────────────────────────┬──────────────────────────────┘
                                                        ▼
                                       ┌─────────────────────────────────┐
                                       │   Local Matching Engine         │
                                       │ - Category compatibility        │
                                       │ - Attribute & route overlap     │
                                       │ - Trip time window proximity    │
                                       └────────────────┬────────────────┘
                                                        │ High confidence (>= 0.75)
                                                        ▼
                                       ┌─────────────────────────────────┐
                                       │ One-Way Outbound Notification   │
                                       │ Customer: Call back to confirm  │
                                       │ Driver: Informational update    │
                                       └─────────────────────────────────┘
```

## System Workflow

1. **Single Telephone Hotline**:
   Both drivers and passengers dial into the same AgentDuet phone number.
2. **Pre-Intake Check**:
   Before initiating intake, the agent checks if the incoming phone number has an open or pending case in Google Sheets via n8n:
   * If an open case exists: Maya greets the caller with their case details and asks for 1 or 2 verifying details to resolve or reopen the case.
   * If no case exists: Maya greets the caller warmly and asks if they are a driver reporting a found item or a customer reporting a lost item.
3. **Conversational Speech via Amazon Nova 2 Sonic**:
   * Caller microphone audio is streamed directly to Amazon Nova 2 Sonic at 24 kHz Linear PCM.
   * Synthesized speech audio output from Nova 2 Sonic is streamed back to the caller in real time over the AgentDuet connection.
   * Instant barge-in: when the caller speaks while Maya is talking, Nova signals interruption, immediately clearing AgentDuet's outbound audio buffer.
4. **Driver Intake Flow**:
   Collects item type, 2 distinguishing details, approximate trip time or shift, route or pickup/dropoff area, seat position where found, and callback number.
5. **Customer Intake Flow**:
   Collects item type, 2 distinguishing details, approximate trip time or date, route or pickup/dropoff area, callback number, and **Ride ID** (from trip receipt or SMS). Maya explicitly reminds the caller: *"Matches are not confirmed live on this call; our automated system will scan driver reports and notify you by phone if a potential match is found."*
6. **Structured Transmission**:
   On call conclusion, the bridge extracts structured JSON attributes (`<INTAKE_DATA>`) and posts them to the n8n intake webhook.
7. **n8n Storage and Matching**:
   * Stores driver reports in the `Found_Items` sheet and customer reports in the `Lost_Items` sheet.
   * Evaluates category compatibility, route overlap, and distinguishing detail tokens.
8. **Edge Condition: Proactive Driver Alert on Unmatched Ride**:
   When a customer logs a lost item that does not match any existing found report, the system queries the `Rides` sheet using their `ride_id`:
   * Identifies the assigned driver and their phone number.
   * Dispatches a proactive one-way outbound call to that driver: *"A passenger from ride {ride_id} has reported a lost {item_type}. Please check your vehicle when safe to do so. If you locate the item, please call our lost-and-found hotline back to log it."*
9. **Pending Confirmation & One-Way Voice Alerts**:
   When a high-confidence match is detected (score >= 0.75):
   * Marks both records as `pending_confirmation` in Google Sheets.
   * Triggers an automated one-way call to the customer asking them to call back to confirm.
   * Triggers an informational one-way call to the driver letting them know a potential match was found.
10. **Customer Callback Resolution**:
    When the customer dials back in, the pre-intake check detects their pending case, asks for 1 to 2 verifying details, and marks the case `resolved`.
11. **Automatic Expiration**:
    Scheduled cron in n8n queries unmatched records older than 14 days and marks their status as `expired`.

## Constraints Observed

* **Voice Only**: Telephony voice calls only, no WhatsApp.
* **Google Sheets Persistence**: Easy-to-view spreadsheets via n8n, no complex database tokens or infrastructure.
* **Zero OpenAI Dependencies**: 100% powered by Amazon Nova 2 Sonic on AWS Bedrock and local deterministic matching algorithms.
* **No Live Outbound Confirmation Calls**: Outbound calls are automated one-way alerts advising the caller to dial back in.

## Project Structure

* [`lost_and_found_agent.py`](lost_and_found_agent.py): Amazon Nova 2 Sonic bidirectional speech bridge, prompt configurations for Maya, barge-in buffer flush, and structured intake extraction.
* [`main.py`](main.py): AgentDuet telephony server with 24 kHz audio bridge and FastAPI dispatcher webhook endpoints.
* [`matching_engine.py`](matching_engine.py): Pure algorithmic multi-attribute scoring engine (category compatibility, token overlap, route overlap, time proximity) with zero external LLM dependencies.
* [`n8n_client.py`](n8n_client.py): Async HTTP client for n8n webhooks and Google Sheets operations with mock fallback.
* [`outbound_dispatcher.py`](outbound_dispatcher.py): AgentDuet one-way outbound voice call dispatcher.
* [`n8n_workflow.json`](n8n_workflow.json): Ready-to-import n8n workflow definition with Google Sheets nodes.
* [`google_sheets_template.json`](google_sheets_template.json): Spreadsheet layout reference for `Found_Items`, `Lost_Items`, and `Rides`.
* [`test_lost_and_found.py`](test_lost_and_found.py): Automated test suite exercising matching rules and the live voice call handling path.

## Setup Instructions

### 1. Environment Setup
```bash
cd agentduet-samples/integrations/n8n
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
cp .env.example .env
```
Edit `.env` with your AgentDuet connector credentials, AWS credentials (with Bedrock access to Amazon Nova 2 Sonic), and n8n webhook URLs:
```ini
AGENTDUET_API_KEY=your_agentduet_api_key
AGENTDUET_CONNECTOR_UUID=your_agentduet_connector_uuid
AGENTDUET_SUBSCRIBER=+15551234567

AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
AWS_REGION=us-east-1
NOVA_SONIC_MODEL_ID=amazon.nova-2-sonic-v1:0
NOVA_SONIC_VOICE_ID=amy

N8N_INTAKE_WEBHOOK_URL=http://localhost:5678/webhook/lost-and-found-intake
N8N_LOOKUP_WEBHOOK_URL=http://localhost:5678/webhook/lost-and-found-lookup
N8N_STATUS_WEBHOOK_URL=http://localhost:5678/webhook/lost-and-found-status
```

### 3. Create Your Google Sheet
Create a new Google Sheet named `City_Cab_Lost_and_Found` with three tabs (see [`google_sheets_template.json`](google_sheets_template.json)):
* **Tab 1: `Found_Items`**
  Columns: `Timestamp`, `Item_Type`, `Detail_1`, `Detail_2`, `Approx_Trip_Time`, `Route_Area`, `Seat_Position`, `Driver_Phone`, `Status`
* **Tab 2: `Lost_Items`**
  Columns: `Timestamp`, `Item_Type`, `Detail_1`, `Detail_2`, `Approx_Trip_Time`, `Route_Area`, `Customer_Phone`, `Ride_ID`, `Status`
* **Tab 3: `Rides`**
  Columns: `Ride_ID`, `Driver_Name`, `Driver_Phone`, `Vehicle_Plate`, `Customer_Phone`, `Route_Area`

### 4. Import n8n Workflow
* Open your n8n dashboard.
* Click **Add Workflow** -> **Import from File**.
* Select [`n8n_workflow.json`](n8n_workflow.json).
* Connect your Google Sheets account in the Google Sheets nodes (1-click OAuth).
* Set your Google Sheet Document ID.
* Activate the workflow.

### 5. Run the Test Suite
```bash
python test_lost_and_found.py
```

### 6. Start the Service
```bash
python main.py
```
