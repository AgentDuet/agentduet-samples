import asyncio
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
import httpx
import websockets

from agentduet import (
    BufferFullError,
    Call,
    CallAudioConfig,
    CallClosedError,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

logging.getLogger("agentduet").setLevel(logging.DEBUG)
logging.getLogger("agentduet.session_manager_connection").setLevel(logging.DEBUG)
logging.getLogger("agentduet.voice_session").setLevel(logging.DEBUG)
logging.getLogger("agentduet.call").setLevel(logging.DEBUG)


class VapiIntegration:
    """Bridges AgentDuet inbound phone calls with Vapi conversational AI agents.

    AgentDuet handles the telephony carrier connection and bidirectional 16 kHz PCM audio.
    Vapi handles conversation logic, speech-to-text, LLM inference, and text-to-speech.
    Communication occurs over Vapi's WebSocket transport (provider: "vapi.websocket").
    """

    def __init__(
        self,
        call: Call,
        assistant_id: str,
        api_key: str,
        vapi_base_url: str = "https://api.vapi.ai",
    ):
        self._call = call
        self._assistant_id = assistant_id
        self._api_key = api_key
        self._vapi_base_url = vapi_base_url.rstrip("/")

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._inbound_task: Optional[asyncio.Task] = None
        self._outbound_task: Optional[asyncio.Task] = None

        self._running = False
        self._terminated = False
        self._vapi_call_id: Optional[str] = None

        # Farewell playback tracking for graceful termination
        self._farewell_active = False
        self._farewell_bytes = 0
        self._first_farewell_chunk_time: Optional[float] = None
        self._last_farewell_chunk_time: Optional[float] = None

    def start_farewell_tracking(self):
        """Marks the start of farewell audio tracking to ensure complete playback."""
        self._farewell_active = True
        self._farewell_bytes = 0
        self._first_farewell_chunk_time = None
        self._last_farewell_chunk_time = None

    async def _create_vapi_call(self) -> str:
        """Calls Vapi REST API to initiate a WebSocket transport call.

        Returns:
            The WebSocket connection URL (websocketCallUrl).
        """
        url = f"{self._vapi_base_url}/call"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "assistantId": self._assistant_id,
            "transport": {
                "provider": "vapi.websocket",
                "audioFormat": {
                    "format": "pcm_s16le",
                    "container": "raw",
                    "sampleRate": 16000,
                },
            },
        }

        logger.info("Initiating Vapi call for assistant: %s", self._assistant_id)

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=10.0)

            if resp.status_code not in (200, 201):
                logger.error(
                    "Vapi call creation failed: status=%s, response=%s",
                    resp.status_code,
                    resp.text,
                )
                raise RuntimeError(
                    f"Vapi API call initiation failed with status {resp.status_code}: {resp.text}"
                )

            data = resp.json()
            self._vapi_call_id = data.get("id")
            logger.info("Vapi call created: id=%s", self._vapi_call_id)

            transport_info = data.get("transport", {})
            websocket_url = transport_info.get("websocketCallUrl") or data.get("websocketCallUrl")

            if not websocket_url:
                raise ValueError(
                    f"Vapi response missing websocketCallUrl in transport: {data}"
                )

            return websocket_url

    def _is_ws_open(self) -> bool:
        """Checks if the WebSocket connection is active across all websockets versions."""
        if self._ws is None:
            return False
        if hasattr(self._ws, "closed"):
            return not self._ws.closed
        if hasattr(self._ws, "state"):
            return self._ws.state == websockets.State.OPEN
        return True

    async def _send_control_message(self, message: dict):
        """Sends a JSON control message to Vapi over the WebSocket transport."""
        if self._is_ws_open():
            try:
                payload = json.dumps(message)
                logger.debug("Sending control message to Vapi: %s", payload)
                await self._ws.send(payload)
            except Exception:
                logger.exception("Failed to send control message to Vapi: %s", message)

    async def _stream_caller_to_vapi(self):
        """Streams inbound PCM audio chunks from the caller's phone to Vapi."""
        try:
            async for chunk in self._call.caller.audio_stream():
                if not self._running or self._terminated:
                    break
                if chunk and self._is_ws_open():
                    await self._ws.send(chunk)
        except asyncio.CancelledError:
            logger.debug("Caller audio streaming task cancelled")
            raise
        except CallClosedError:
            logger.debug("AgentDuet call closed: stopping audio stream to Vapi")
        except Exception:
            logger.exception("Error streaming caller audio to Vapi")
            raise
        finally:
            logger.debug("Caller audio stream to Vapi ended")

    async def _receive_from_vapi(self):
        """Receives audio chunks and JSON control messages from Vapi over WebSocket."""
        try:
            async for message in self._ws:
                if not self._running or self._terminated:
                    break

                if isinstance(message, bytes):
                    # Binary message: raw 16-bit 16 kHz PCM audio from Vapi assistant
                    if self._farewell_active:
                        now = asyncio.get_running_loop().time()
                        if self._first_farewell_chunk_time is None:
                            self._first_farewell_chunk_time = now
                        self._last_farewell_chunk_time = now
                        self._farewell_bytes += len(message)

                    try:
                        await self._call.send_audio(message)
                    except BufferFullError:
                        logger.warning(
                            "AgentDuet send buffer full: dropping frame or backpressure"
                        )
                    except CallClosedError:
                        logger.debug("AgentDuet call closed while sending audio")
                        break

                elif isinstance(message, str):
                    # Text message: JSON control or status event
                    await self._handle_vapi_text_message(message)

        except asyncio.CancelledError:
            logger.debug("Vapi receive task cancelled")
            raise
        except websockets.ConnectionClosed as e:
            logger.info("Vapi WebSocket connection closed: code=%s, reason=%s", e.code, e.reason)
        except Exception:
            logger.exception("Error receiving data from Vapi WebSocket")
            raise
        finally:
            logger.debug("Vapi receiver task ended")

    async def _handle_vapi_text_message(self, raw_message: str):
        """Handles JSON control frames and server events from Vapi."""
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("Unrecognized non-JSON text frame from Vapi: %s", raw_message[:100])
            return

        msg_type = data.get("type") or data.get("message", {}).get("type")
        # TEMP DEBUG: log the full raw payload so real Vapi event/type names
        # can be confirmed and the checks below updated to match reality.
        logger.debug("Vapi RAW event: %s", data)

        if msg_type == "user-interrupted":
            # Barge-in: user spoke while assistant was responding
            logger.info("Vapi detected user interruption: clearing playback buffer")
            try:
                await self._call.clear_send_audio_buffer()
            except CallClosedError:
                pass

        elif msg_type in ("hang", "end-call"):
            # Assistant ended call
            logger.info("Vapi requested call termination (type=%s)", msg_type)
            await self._handle_agent_initiated_hangup()

        elif msg_type == "transcript":
            role = data.get("role") or data.get("message", {}).get("role", "transcript")
            transcript_text = (
                data.get("transcript")
                or data.get("message", {}).get("transcript", "")
            )
            logger.info("[%s]: %s", role.capitalize(), transcript_text)

        elif msg_type == "speech-update":
            status = data.get("status") or data.get("message", {}).get("status", "")
            logger.debug("Vapi speech status: %s", status)

    async def _handle_agent_initiated_hangup(self):
        """Drains farewell audio before closing the telephony call."""
        if self._terminated:
            return
        self.start_farewell_tracking()
        await self.wait_for_farewell_complete()
        await self.teardown()

    async def wait_for_farewell_complete(self, max_wait: float = 8.0):
        """Waits until farewell speech finishes physical playback before disconnecting."""
        loop = asyncio.get_running_loop()
        start = loop.time()

        # Wait up to 2.0s for first farewell chunk
        while self._first_farewell_chunk_time is None:
            if loop.time() - start > 2.0:
                logger.info("No farewell audio received within 2.0s, proceeding to disconnect")
                return
            await asyncio.sleep(0.05)

        # Wait until Vapi stops emitting chunks (quiet for 0.5s)
        while True:
            await asyncio.sleep(0.05)
            time_since_last_chunk = loop.time() - self._last_farewell_chunk_time
            if time_since_last_chunk >= 0.5:
                break
            if loop.time() - start > max_wait:
                logger.warning("Max wait reached while receiving farewell audio")
                break

        # Physical playback duration at 16 kHz 16-bit Mono (32,000 bytes/sec)
        bytes_per_sec = 32000
        audio_duration = self._farewell_bytes / bytes_per_sec
        elapsed = loop.time() - self._first_farewell_chunk_time
        remaining = audio_duration - elapsed + 0.3  # 300ms cushion

        logger.info(
            "Farewell audio: %d bytes (%.2fs), remaining playback: %.2fs",
            self._farewell_bytes,
            audio_duration,
            max(0.0, remaining),
        )

        if remaining > 0:
            await asyncio.sleep(remaining)

    async def teardown(self):
        """Cleanly tears down both WebSocket and AgentDuet connections."""
        if self._terminated:
            return
        self._terminated = True
        self._running = False

        # Cancel background streaming tasks
        for task in (self._inbound_task, self._outbound_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Send end-call to Vapi and close WebSocket
        if self._is_ws_open():
            try:
                await self._send_control_message({"type": "end-call"})
                await self._ws.close()
            except Exception:
                pass

        # Disconnect phone call
        try:
            await self._call.close()
        except Exception:
            pass

        logger.info("Call %s and Vapi bridge cleanly torn down", self._call.id)

    async def run(self):
        """Initiates the Vapi session, establishes WebSocket transport, and runs audio loops."""
        self._running = True

        # Register callback for when the phone caller hangs up.
        # AgentDuet's on_hangup handler is called with one positional
        # argument (the call.terminated event payload) - accept it even
        # though we don't need its contents here.
        @self._call.on_hangup
        async def on_caller_hangup(event=None):
            logger.info("Caller hung up call %s: notifying Vapi", self._call.id)
            await self._send_control_message({"type": "end-call"})
            await self.teardown()

        # Step 1: Create Vapi call via REST API
        websocket_url = await self._create_vapi_call()

        # Step 2: Connect to Vapi WebSocket transport
        logger.info("Connecting to Vapi WebSocket transport: %s", websocket_url)
        async with websockets.connect(websocket_url) as ws:
            self._ws = ws
            logger.info("Connected to Vapi WebSocket for call %s", self._call.id)

            # Step 3: Run inbound and outbound audio loops concurrently
            self._inbound_task = asyncio.create_task(self._stream_caller_to_vapi())
            self._outbound_task = asyncio.create_task(self._receive_from_vapi())

            done, pending = await asyncio.wait(
                [self._inbound_task, self._outbound_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self.teardown()


async def main():
    assistant_id = os.getenv("VAPI_ASSISTANT_ID")
    if not assistant_id:
        raise ValueError("VAPI_ASSISTANT_ID environment variable must be set")

    api_key = os.getenv("VAPI_API_KEY")
    if not api_key:
        raise ValueError("VAPI_API_KEY environment variable must be set")

    agentduet_api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")

    # Vapi WebSocket transport uses 16 kHz 16-bit Linear PCM audio
    config = SessionManagerConfig.create(
        api_key=agentduet_api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=16000,
            buffer_size=1024 * 1024,  # 1MB buffer
        ),
    )

    async with SessionManager(config) as sm:
        logger.info("SessionManager started: %s", sm.id)

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification):
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            logger.info("Incoming call received: id=%s, caller=%s", call.id, call.caller)

            try:
                result = await call.answer()
                if not result:
                    logger.error(
                        "Failed to answer call %s: %s (%s)",
                        call.id,
                        result.error_message,
                        result.error_code,
                    )
                    return

                integration = VapiIntegration(
                    call=call,
                    assistant_id=assistant_id,
                    api_key=api_key,
                )
                await integration.run()
            except Exception:
                logger.exception("Error handling call with Vapi integration")
                await call.close()
                raise

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())