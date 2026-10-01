import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from hotel_reservation_agent import (
    HOTEL_SYSTEM_INSTRUCTION,
    HotelReservationVoiceAgent,
    build_gemini_config,
    get_hotel_tool_declarations,
)
from make_client import MakeHotelClient


class MockCall:
    def __init__(self, caller_phone: str = "+15551234567"):
        self.id = "call-make-test-101"
        self.caller = caller_phone
        self.send_audio = AsyncMock()
        self.clear_send_audio_buffer = AsyncMock()
        self.close = AsyncMock()
        self.on_hangup = MagicMock()


class TestMakeHotelReservationRealtimeAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_client = MakeHotelClient(mock_mode=True)
        self.mock_call = MockCall("+15559876543")
        self.mock_gemini = AsyncMock()
        self.agent = HotelReservationVoiceAgent(
            call=self.mock_call,
            gemini_session=self.mock_gemini,
            make_client=self.make_client,
            caller_phone="+15559876543",
        )

    def test_gemini_system_instruction_and_tool_declarations(self):
        """Verifies system instruction contains hotel flow rules and constraints,

        and registers both Make.com tool declarations.
        """
        config = build_gemini_config(voice_name="Zephyr")
        instruction = config.system_instruction

        # Instruction rules
        self.assertIn("Grand Horizon Hotel", instruction)
        self.assertIn("check_room_availability", instruction)
        self.assertIn("create_hotel_booking", instruction)
        self.assertIn("Deluxe", instruction)
        self.assertIn("Executive Suite", instruction)
        self.assertIn("honestly", instruction)
        self.assertIn("confirmation email", instruction)
        self.assertIn("No payment", instruction)

        # Tools declaration
        tools = get_hotel_tool_declarations()
        tool_names = [t.name for t in tools]
        self.assertIn("check_room_availability", tool_names)
        self.assertIn("create_hotel_booking", tool_names)

        # Check parameter schema for availability
        avail_tool = next(t for t in tools if t.name == "check_room_availability")
        self.assertIn("check_in", avail_tool.parameters_json_schema["properties"])
        self.assertIn("check_out", avail_tool.parameters_json_schema["properties"])
        self.assertIn("guests", avail_tool.parameters_json_schema["properties"])

        # Check parameter schema for booking
        booking_tool = next(t for t in tools if t.name == "create_hotel_booking")
        self.assertIn("guest_name", booking_tool.parameters_json_schema["properties"])
        self.assertIn("guest_email", booking_tool.parameters_json_schema["properties"])
        self.assertIn("room_type", booking_tool.parameters_json_schema["properties"])
        self.assertIn("nightly_rate", booking_tool.parameters_json_schema["properties"])

    async def test_agent_dispatch_check_room_availability(self):
        """Verifies that when Gemini triggers check_room_availability,

        the agent queries Make.com and returns multi-room options with rates.
        """
        result = await self.agent.dispatch_tool(
            "check_room_availability",
            {
                "check_in": "2026-10-15",
                "check_out": "2026-10-18",
                "guests": 2,
            },
        )

        self.assertTrue(result["available"])
        self.assertEqual(len(result["room_options"]), 2)

        # Verify Deluxe and Executive Suite rates
        room_names = [r["room_type"] for r in result["room_options"]]
        rates = [r["nightly_rate"] for r in result["room_options"]]
        self.assertIn("Deluxe Room", room_names)
        self.assertIn("Executive Suite", room_names)
        self.assertIn(150, rates)
        self.assertIn(250, rates)

    async def test_agent_dispatch_fully_booked_dates(self):
        """Verifies that when dates are fully booked, Make.com returns available=False

        so the speech model cannot fabricate availability.
        """
        # 2026-12-31 is seeded as fully booked in mock inventory
        result = await self.agent.dispatch_tool(
            "check_room_availability",
            {
                "check_in": "2026-12-31",
                "check_out": "2026-12-25",
                "guests": 1,
            },
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["room_options"], [])
        self.assertIn("No rooms available", result.get("message", ""))

    async def test_agent_dispatch_create_hotel_booking(self):
        """Verifies that when Gemini triggers create_hotel_booking,

        the agent sends booking details to Make.com and receives confirmation.
        """
        result = await self.agent.dispatch_tool(
            "create_hotel_booking",
            {
                "guest_name": "Sarah Connor",
                "guest_email": "sarah.connor@example.com",
                "check_in": "2026-10-15",
                "check_out": "2026-10-18",
                "guests": 2,
                "room_type": "Deluxe Room",
                "nightly_rate": 150,
            },
        )

        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["email_sent"])
        self.assertTrue(result["booking_id"].startswith("BK-"))
        self.assertEqual(result["guest_name"], "Sarah Connor")
        self.assertEqual(result["room_type"], "Deluxe Room")

    async def test_agent_interruption_buffer_flush(self):
        """Verifies that when an interruption signal is received from Gemini,

        the agent immediately flushes the telephony audio buffer.
        """
        await self.mock_call.clear_send_audio_buffer()
        self.mock_call.clear_send_audio_buffer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
