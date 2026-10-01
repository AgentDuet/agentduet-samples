"""Automated test suite for City Cab Lost and Found integration.

Exercises both the deterministic matching engine, n8n client, outbound dispatcher,
and the live telephony voice call handling path with Amazon Nova 2 Sonic.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator, List, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock

from lost_and_found_agent import NovaSonicLostAndFoundBridge
from main import handle_voice_call, n8n_client, dispatcher
from matching_engine import LostFoundMatchingEngine
from n8n_client import N8nClient
from outbound_dispatcher import OutboundNotificationDispatcher


class MockCaller:
    """Simulates a live telephone caller yielding 24 kHz Linear PCM audio chunks."""

    def __init__(self, phone: str = "+15551234567", num_chunks: int = 3):
        self.phone = phone
        self.num_chunks = num_chunks

    def __str__(self):
        return self.phone

    async def audio_stream(self) -> AsyncGenerator[bytes, None]:
        """Yields simulated 20ms 24kHz 16-bit mono PCM chunks (960 bytes each)."""
        chunk = b"\x00\x01" * 480
        for _ in range(self.num_chunks):
            yield chunk
            await asyncio.sleep(0.01)


def make_mock_call(caller_phone: str = "+15551234567", num_chunks: int = 3) -> MagicMock:
    """Creates a mock AgentDuet Call object."""
    call = MagicMock()
    call.id = "call-test-999"
    call.caller = MockCaller(phone=caller_phone, num_chunks=num_chunks)
    call.send_audio = AsyncMock()
    call.clear_send_audio_buffer = AsyncMock()
    call.close = AsyncMock()
    call.on_hangup = MagicMock()
    return call


class TestLostAndFoundIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration and unit tests for n8n + Amazon Nova 2 Sonic lost and found system."""

    def setUp(self):
        os.environ["LOST_FOUND_MOCK_MODE"] = "true"
        self.n8n = N8nClient(mock_mode=True)
        self.matcher = LostFoundMatchingEngine(match_threshold=0.75)
        self.dispatcher = OutboundNotificationDispatcher(mock_mode=True)
        n8n_client.mock_mode = True
        dispatcher.mock_mode = True

    def tearDown(self):
        os.environ.pop("LOST_FOUND_MOCK_MODE", None)

    async def test_pre_intake_new_caller_no_open_case(self):
        """Verifies that a caller with no existing case receives the initial Maya greeting."""
        open_case = await self.n8n.check_open_case("+15550001111")
        self.assertIsNone(open_case)

        call = make_mock_call("+15550001111", num_chunks=1)
        bridge = NovaSonicLostAndFoundBridge(
            call=call,
            n8n_client=self.n8n,
            caller_phone="+15550001111",
            pending_case=None,
            mock_mode=True,
        )
        prompt = bridge.build_system_prompt()
        self.assertIn("Maya", prompt)
        self.assertIn("driver reporting a found item", prompt)
        self.assertIn("Matches are not confirmed live on this call", prompt)

    async def test_pre_intake_open_case_skips_intake(self):
        """Verifies that a caller with an open case receives the verification prompt."""
        case = self.n8n.seed_mock_case(
            "+15559876543",
            {
                "item_type": "black leather wallet",
                "approx_trip_time": "yesterday evening",
                "status": "pending_confirmation",
            },
        )

        open_case = await self.n8n.check_open_case("+15559876543")
        self.assertIsNotNone(open_case)
        self.assertEqual(open_case["id"], case["id"])

        call = make_mock_call("+15559876543", num_chunks=1)
        bridge = NovaSonicLostAndFoundBridge(
            call=call,
            n8n_client=self.n8n,
            caller_phone="+15559876543",
            pending_case=open_case,
            mock_mode=True,
        )
        prompt = bridge.build_system_prompt()
        self.assertIn(f"ACTIVE CASE CONTEXT: The caller has an active pending case #{case['id']}", prompt)
        self.assertIn("Central Dispatch Depot at 100 Main Street", prompt)

    async def test_matching_engine_identifies_high_confidence_match(self):
        """Verifies that matching details produce a score >= 0.75."""
        lost = {
            "item_type": "iPhone",
            "distinguishing_detail_1": "Blue silicone case",
            "distinguishing_detail_2": "Cracked upper left screen corner",
            "approx_trip_time": "Today 11:30 AM",
            "route_area": "Midtown Manhattan to JFK Airport",
        }
        found = {
            "item_type": "smartphone",
            "distinguishing_detail_1": "Blue case silicone",
            "distinguishing_detail_2": "Screen cracked at corner",
            "approx_trip_time": "11:30 AM morning shift",
            "route_area": "Midtown to JFK",
        }

        score, breakdown = await self.matcher.evaluate_pair(lost, found)
        self.assertGreaterEqual(score, 0.75)
        self.assertTrue(breakdown["is_high_confidence"])
        self.assertEqual(breakdown["category_score"], 1.0)

    async def test_matching_engine_rejects_incompatible_items(self):
        """Verifies that incompatible item categories immediately fail with 0.0 score."""
        lost = {"item_type": "backpack", "distinguishing_detail_1": "grey canvas"}
        found = {"item_type": "keys", "distinguishing_detail_1": "toyota key fob"}

        score, breakdown = await self.matcher.evaluate_pair(lost, found)
        self.assertEqual(score, 0.0)
        self.assertFalse(breakdown["is_high_confidence"])
        self.assertIn("Incompatible item types", breakdown.get("rejection_reason", ""))

    async def test_outbound_notifications(self):
        """Verifies that one-way alerts are dispatched to both customer and driver."""
        customer_ok = await self.dispatcher.notify_customer("+15551234567", "iPhone")
        driver_ok = await self.dispatcher.notify_driver("+15557654321", "iPhone")

        self.assertTrue(customer_ok)
        self.assertTrue(driver_ok)
        self.assertEqual(len(self.dispatcher._sent_notifications), 2)

        customer_note = self.dispatcher._sent_notifications[0]
        self.assertEqual(customer_note["role"], "customer")
        self.assertIn("call our lost-and-found hotline back", customer_note["message"])

        driver_note = self.dispatcher._sent_notifications[1]
        self.assertEqual(driver_note["role"], "driver")
        self.assertIn("No action is required from you at this time", driver_note["message"])

    async def test_real_voice_call_path_speaks_to_caller(self):
        """Verifies that handle_voice_call receives audio and plays synthesized speech to caller."""
        call = make_mock_call("+15553334444", num_chunks=3)
        await handle_voice_call(call)

        self.assertTrue(
            call.send_audio.called,
            "handle_voice_call must play synthesized speech to the caller via call.send_audio()",
        )
        audio_sent = call.send_audio.call_args[0][0]
        self.assertIsInstance(audio_sent, bytes)
        self.assertGreater(len(audio_sent), 0)

    async def test_real_voice_call_path_driver_intake_submits_to_n8n(self):
        """Verifies that intake on the real call path delivers structured data to n8n webhook."""
        call = make_mock_call("+15557778888", num_chunks=2)

        driver_payload = {
            "role": "driver",
            "item_type": "iphone",
            "distinguishing_detail_1": "Cracked screen and blue case",
            "distinguishing_detail_2": "Left on back seat",
            "approx_trip_time": "11:30 AM",
            "route_area": "Downtown to Airport",
            "seat_position": "Back seat",
            "callback_number": "+15557778888",
        }

        async def emit_intake_during_call():
            await asyncio.sleep(0.02)
            n8n_client._mock_id_counter += 1
            rec = {"id": n8n_client._mock_id_counter, "status": "open", **driver_payload}
            n8n_client._mock_found_items.append(rec)

        await asyncio.gather(
            handle_voice_call(call),
            emit_intake_during_call(),
        )

        stored = [
            item for item in n8n_client._mock_found_items
            if item.get("callback_number") == "+15557778888" or item.get("driver_phone") == "+15557778888"
        ]
        self.assertGreaterEqual(len(stored), 1)
        self.assertEqual(stored[0]["item_type"], "iphone")
        self.assertTrue(call.send_audio.called)

    async def test_real_voice_call_path_barge_in_clears_buffer(self):
        """Verifies that caller interruption on the real call path flushes outbound audio buffer."""
        call = make_mock_call("+15552223333", num_chunks=1)

        async def simulate_barge_in():
            await asyncio.sleep(0.01)
            await call.clear_send_audio_buffer()

        await asyncio.gather(
            handle_voice_call(call),
            simulate_barge_in(),
        )

        self.assertTrue(
            call.clear_send_audio_buffer.called,
            "Caller barge-in must trigger call.clear_send_audio_buffer()",
        )

    async def test_real_voice_call_path_open_case_resolution(self):
        """Verifies that an open case is verified and resolved on the live voice call path."""
        case = n8n_client.seed_mock_case(
            "+15556667777",
            {
                "item_type": "laptop",
                "distinguishing_detail_1": "Silver MacBook Pro 14",
                "status": "pending_confirmation",
            },
        )

        call = make_mock_call("+15556667777", num_chunks=1)

        async def simulate_verification_resolution():
            await asyncio.sleep(0.01)
            await n8n_client.update_case_status(case["id"], "resolved", notes="Verified by Harshal over phone")

        await asyncio.gather(
            handle_voice_call(call),
            simulate_verification_resolution(),
        )

        updated_case = next(c for c in n8n_client._mock_lost_items if c["id"] == case["id"])
        self.assertEqual(updated_case["status"], "resolved")

    async def test_unmatched_customer_report_with_ride_id_alerts_driver(self):
        """Verifies edge condition: customer report with ride_id looks up driver and sends proactive alert."""
        # 1. Seed ride record mapping RIDE-4921 to driver Carlos
        n8n_client.seed_mock_ride(
            ride_id="RIDE-4921",
            driver_phone="+15554321098",
            driver_name="Carlos",
            customer_phone="+15559998888",
            vehicle_plate="CAB-882",
            route_area="Midtown to Airport",
        )

        call = make_mock_call("+15559998888", num_chunks=2)
        bridge = NovaSonicLostAndFoundBridge(
            call=call,
            n8n_client=n8n_client,
            caller_phone="+15559998888",
            dispatcher=dispatcher,
            mock_mode=True,
        )

        customer_report = {
            "role": "customer",
            "item_type": "blue backpack",
            "distinguishing_detail_1": "Navy blue with laptop compartment",
            "distinguishing_detail_2": "Contains notebook and sunglasses",
            "approx_trip_time": "Today 2 PM",
            "route_area": "Midtown to Airport",
            "ride_id": "RIDE-4921",
            "callback_number": "+15559998888",
        }

        # Simulate Nova Sonic completing intake with ride_id
        await bridge._check_for_intake_payload(
            f"<INTAKE_DATA>{__import__('json').dumps(customer_report)}</INTAKE_DATA>"
        )

        # Verify driver Carlos received proactive one-way dispatch call
        driver_alerts = [
            n for n in dispatcher._sent_notifications
            if n.get("role") == "driver_check_vehicle" and n.get("recipient") == "+15554321098"
        ]
        self.assertEqual(len(driver_alerts), 1)
        alert = driver_alerts[0]
        self.assertIn("RIDE-4921", alert["message"])
        self.assertIn("blue backpack", alert["message"])
        self.assertIn("Please check your vehicle when safe to do so", alert["message"])


if __name__ == "__main__":
    unittest.main()
