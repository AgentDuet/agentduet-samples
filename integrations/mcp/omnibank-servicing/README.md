# MCP: OmniBank phone tools

Give a Gemini Live phone agent tools via [MCP](https://modelcontextprotocol.io/).
AgentDuet owns the call. Gemini Live owns the conversation. An MCP stdio server
owns bank tools (authenticate, fees, escalate).

Demo caller: **Ava Chen**, card ending **4821**.

## Prerequisites

- Python **3.12+**
- AgentDuet API key + connector UUID from [agentduet.com](https://agentduet.com)
- [Gemini API key](https://aistudio.google.com/apikey)

## Setup

```bash
git clone https://github.com/AgentDuet/agentduet-samples.git
cd agentduet-samples/integrations/mcp/omnibank-servicing

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

```bash
python main.py
```

Call your AgentDuet number. Say you are Ava Chen, card ending 4821. Ask about
fees; try waiving one fee (within the $50 cap) or both (should refuse / escalate).

## Layout

```
omnibank-servicing/
├── main.py               # AgentDuet + Gemini Live + MCP client
├── mcp_tools_server.py   # FastMCP bank tools (stdio)
├── requirements.txt
└── .env.example
```

## Tools

| Tool | Role |
|---|---|
| `authenticate_customer` | Match name + last 4; return fees and waiver limit |
| `get_account_summary` | Card, balance, outstanding fees |
| `process_fee_waiver` | Waive fees up to $50 total |
| `record_escalation` | Log supervisor handoff; return reference |

## Related

- [Docs: MCP integration](https://docs.agentduet.com/integrations/mcp)
- [Gemini Live](https://docs.agentduet.com/integrations/gemini-live)
