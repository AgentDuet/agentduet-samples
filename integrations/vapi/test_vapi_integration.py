import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import websockets

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from main import VapiIntegration


class MockCaller:
    def __init__(self, audio_chunks):
        self._chunks = audio_chunks

    async def audio_stream(self):
        for chunk in self._chunks:
            yield chunk
            await asyncio.sleep(0.01)


class MockCall:
    def __init__(self, audio_chunks=None):
        self.id = "test-call-1234"
        self.caller = MockCaller(audio_chunks or [b"\x00\x01" * 160])
        self.send_audio = AsyncMock()
        self.clear_send_audio_buffer = AsyncMock()
        self.close = AsyncMock()
        self._hangup_callbacks = []

    def on_hangup(self, callback):
        self._hangup_callbacks.append(callback)
        return callback

    async def trigger_hangup(self):
        for cb in self._hangup_callbacks:
            if asyncio.iscoroutinefunction(cb):
                await cb()
            else:
                cb()


class TestVapiIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_vapi_call_creation(self):
        """Verifies REST API initiation payload and parsing of websocketCallUrl."""
        call = MockCall()
        integration = VapiIntegration(
            call=call,
            assistant_id="asst_123",
            api_key="vapi_test_key",
            vapi_base_url="https://api.vapi.ai",
        )

        mock_response_data = {
            "id": "vapi-call-5678",
            "transport": {
                "provider": "vapi.websocket",
                "websocketCallUrl": "wss://api.vapi.ai/vapi-call-5678/transport",
            },
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = mock_response_data

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp

            ws_url = await integration._create_vapi_call()

            self.assertEqual(ws_url, "wss://api.vapi.ai/vapi-call-5678/transport")
            self.assertEqual(integration._vapi_call_id, "vapi-call-5678")

            # Verify exact request parameters
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer vapi_test_key")
            self.assertEqual(kwargs["json"]["assistantId"], "asst_123")
            self.assertEqual(kwargs["json"]["transport"]["provider"], "vapi.websocket")
            self.assertEqual(kwargs["json"]["transport"]["audioFormat"]["format"], "pcm_s16le")
            self.assertEqual(kwargs["json"]["transport"]["audioFormat"]["sampleRate"], 16000)

    async def test_vapi_interruption_clears_buffer(self):
        """Verifies that user-interrupted message clears the AgentDuet audio buffer."""
        call = MockCall()
        integration = VapiIntegration(
            call=call,
            assistant_id="asst_123",
            api_key="vapi_test_key",
        )

        # Simulate receiving a user-interrupted message from Vapi
        await integration._handle_vapi_text_message(
            json.dumps({"type": "user-interrupted", "turnId": "turn-1"})
        )

        call.clear_send_audio_buffer.assert_awaited_once()

    async def test_vapi_hangup_control_message_on_caller_hangup(self):
        """Verifies that when caller hangs up, end-call control message is sent to Vapi."""
        call = MockCall()
        integration = VapiIntegration(
            call=call,
            assistant_id="asst_123",
            api_key="vapi_test_key",
        )

        mock_ws = AsyncMock()
        mock_ws.closed = False
        integration._ws = mock_ws

        # Trigger teardown
        await integration.teardown()

        # Verify end-call message was sent
        mock_ws.send.assert_awaited()
        sent_msgs = [c.args[0] for c in mock_ws.send.call_args_list]
        self.assertTrue(any(json.loads(m).get("type") == "end-call" for m in sent_msgs if isinstance(m, str)))
        call.close.assert_awaited_once()

    async def test_vapi_bidirectional_streaming_and_termination(self):
        """Tests full end-to-end flow with a mock local WebSocket server."""
        received_audio_from_caller = []

        async def vapi_server_handler(websocket):
            # 1. Send first audio greeting
            greeting_audio = b"\x10\x00" * 320
            await websocket.send(greeting_audio)

            # 2. Receive caller audio
            async for msg in websocket:
                if isinstance(msg, bytes):
                    received_audio_from_caller.append(msg)
                    # 3. Simulate user interruption event
                    await websocket.send(json.dumps({"type": "user-interrupted"}))
                    # 4. Simulate farewell speech
                    farewell_audio = b"\x20\x00" * 320
                    await websocket.send(farewell_audio)
                    # 5. Simulate assistant hangup
                    await websocket.send(json.dumps({"type": "hang"}))
                    break

        server = await websockets.serve(vapi_server_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        ws_test_url = f"ws://127.0.0.1:{port}"

        caller_chunks = [b"\x05\x05" * 160, b"\x06\x06" * 160]
        call = MockCall(caller_chunks)
        integration = VapiIntegration(
            call=call,
            assistant_id="asst_test",
            api_key="test_key",
        )

        with patch.object(integration, "_create_vapi_call", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = ws_test_url

            run_task = asyncio.create_task(integration.run())
            await asyncio.wait_for(run_task, timeout=5.0)

        server.close()
        await server.wait_closed()

        self.assertGreater(len(received_audio_from_caller), 0)
        self.assertGreaterEqual(call.send_audio.await_count, 2)
        self.assertGreaterEqual(call.clear_send_audio_buffer.await_count, 1)
        self.assertGreaterEqual(call.close.await_count, 1)


if __name__ == "__main__":
    unittest.main()
