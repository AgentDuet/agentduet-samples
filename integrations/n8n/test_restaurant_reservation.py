import asyncio
import os
import pytest
import httpx
from dotenv import load_dotenv

from n8n_client import N8nReservationClient
from restaurant_reservation_agent import (
    build_gemini_config,
    get_reservation_tool_declarations,
)

load_dotenv()


@pytest.mark.asyncio
async def test_n8n_client_mock_mode():
    """Verifies that the client returns a mock confirmation when no URL is set."""
    client = N8nReservationClient(webhook_url="")
    result = await client.book_table(
        guest_name="Harshal",
        party_size=4,
        reservation_time="Tonight 7:30 PM",
        phone_number="+15551234567",
        special_requests="Window table",
    )

    assert result["status"] == "confirmed"
    assert "RES-" in result["confirmation_code"]
    assert result["guest_name"] == "Harshal"
    assert result["party_size"] == 4
    assert result["mock"] is True


def test_tool_declarations():
    """Verifies that the Gemini Live tool declarations are properly formatted."""
    tools = get_reservation_tool_declarations()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "book_restaurant_table"
    schema = tool.parameters_json_schema
    assert "guest_name" in schema["properties"]
    assert "party_size" in schema["properties"]
    assert "reservation_time" in schema["properties"]
    assert "required" in schema
    assert "guest_name" in schema["required"]


def test_gemini_config():
    """Verifies Gemini Live audio configuration."""
    config = build_gemini_config()
    assert config.response_modalities is not None
    assert config.system_instruction is not None
    assert len(config.tools) == 1


@pytest.mark.asyncio
async def test_live_or_mock_webhook():
    """Tests webhook dispatch using either configured URL or simulated HTTP."""
    webhook_url = os.getenv("N8N_RESERVATION_WEBHOOK_URL")

    if not webhook_url:
        # Test mock mode
        client = N8nReservationClient()
        res = await client.book_table("Alex", 2, "Tomorrow 8 PM", "+15559876543")
        assert res["status"] == "confirmed"
        return

    # Real dispatch test
    client = N8nReservationClient(webhook_url=webhook_url)
    res = await client.book_table(
        guest_name="Test Guest",
        party_size=2,
        reservation_time="Tonight 8:00 PM",
        phone_number="+15550001111",
        special_requests="Test reservation verification",
    )
    assert res.get("status") in ("confirmed", "success")


if __name__ == "__main__":
    asyncio.run(test_n8n_client_mock_mode())
    test_tool_declarations()
    test_gemini_config()
    print("[OK] All restaurant reservation tests passed!")
