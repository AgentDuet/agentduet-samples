import asyncio
import logging
import os
import re
from typing import Awaitable, Callable, Optional

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import (
    AsyncAudioInterface,
    AsyncConversation,
    ClientTools,
)

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


class AgentDuetAsyncAudioInterface(AsyncAudioInterface):
    """Bridges AgentDuet bidirectional 16 kHz PCM audio with ElevenLabs Conversational AI."""

    def __init__(self, call: Call):
        self._call = call
        self._input_task: Optional[asyncio.Task] = None
        self._running = False
        self._farewell_active = False
        self._farewell_bytes = 0
        self._first_farewell_chunk_time: Optional[float] = None
        self._last_farewell_chunk_time: Optional[float] = None

    def start_farewell_tracking(self):
        """Marks the start of farewell audio tracking to ensure full playback before disconnect."""
        self._farewell_active = True
        self._farewell_bytes = 0
        self._first_farewell_chunk_time = None
        self._last_farewell_chunk_time = None

    async def start(self, input_callback: Callable[[bytes], Awaitable[None]]):
        """Starts streaming inbound caller audio from AgentDuet to ElevenLabs.

        ElevenLabs expects 16-bit Mono PCM audio at 16 kHz.
        """
        self._running = True

        async def _stream_inbound():
            try:
                async for chunk in self._call.caller.audio_stream():
                    if not self._running:
                        break
                    if chunk:
                        await input_callback(chunk)
            except asyncio.CancelledError:
                logger.debug("ElevenLabs inbound audio streaming task cancelled")
                raise
            except CallClosedError:
                logger.debug(
                    "Call closed: stopping audio stream to ElevenLabs Conversational AI"
                )
            except Exception:
                logger.exception("Error streaming caller audio to ElevenLabs")
                raise
            finally:
                logger.debug("Inbound audio streaming task finished")

        self._input_task = asyncio.create_task(_stream_inbound())

    async def output(self, audio: bytes):
        """Streams synthesized speech from ElevenLabs to the caller over AgentDuet."""
        if not self._running or not audio:
            return

        if self._farewell_active:
            now = asyncio.get_running_loop().time()
            if self._first_farewell_chunk_time is None:
                self._first_farewell_chunk_time = now
            self._last_farewell_chunk_time = now
            self._farewell_bytes += len(audio)

        try:
            await self._call.send_audio(audio)
        except BufferFullError:
            logger.warning(
                "AgentDuet send buffer full: consider increasing buffer_size or dropping frames"
            )
        except CallClosedError:
            logger.debug("Call closed while sending audio to caller")

    async def wait_for_farewell_complete(self, max_wait: float = 8.0):
        """Waits until all farewell audio is generated and has fully played to the caller."""
        loop = asyncio.get_running_loop()
        start = loop.time()

        # Wait for farewell audio generation to begin (or timeout after 2.5s)
        while self._first_farewell_chunk_time is None:
            if loop.time() - start > 2.5:
                logger.info("No farewell audio chunk received within 2.5s, proceeding to close")
                return
            await asyncio.sleep(0.05)

        # Wait until ElevenLabs stops sending chunks (no new chunk for 0.6s)
        while True:
            await asyncio.sleep(0.1)
            time_since_last_chunk = loop.time() - self._last_farewell_chunk_time
            if time_since_last_chunk >= 0.6:
                break
            if loop.time() - start > max_wait:
                logger.warning("Max wait reached while receiving farewell audio")
                break

        # Calculate actual physical playback duration at 16 kHz 16-bit Mono (32,000 bytes/sec)
        bytes_per_sec = 32000
        audio_duration = self._farewell_bytes / bytes_per_sec
        elapsed_since_first_chunk = loop.time() - self._first_farewell_chunk_time
        remaining_playback = audio_duration - elapsed_since_first_chunk + 0.4  # 400ms cushion

        logger.info(
            "Farewell audio: %d bytes (%.2fs), remaining playback: %.2fs",
            self._farewell_bytes,
            audio_duration,
            max(0.0, remaining_playback),
        )

        if remaining_playback > 0:
            await asyncio.sleep(remaining_playback)

    async def interrupt(self):
        """Drops pending AgentDuet playback buffer immediately when caller barge-in occurs."""
        try:
            await self._call.clear_send_audio_buffer()
            logger.debug(
                "Interruption detected: cleared AgentDuet outgoing audio buffer"
            )
        except CallClosedError:
            pass

    async def stop(self):
        """Stops audio streaming and cleans up background tasks."""
        self._running = False
        if self._input_task and not self._input_task.done():
            self._input_task.cancel()
            try:
                await self._input_task
            except asyncio.CancelledError:
                pass


def should_hang_up(transcript: str) -> bool:
    """Detects whether the caller intends to end the call."""
    text = transcript.lower().strip()
    cleaned = re.sub(r"[^\w\s]", "", text)
    tokens = cleaned.split()

    hangup_phrases = [
        "hang up the call",
        "hang up call",
        "hang up",
        "hangup",
        "end the call",
        "end call",
        "disconnect",
        "bye bye",
        "bye",
        "byy",
        "byee",
        "goodbye",
    ]
    for phrase in hangup_phrases:
        if phrase in cleaned:
            if f"dont {phrase}" in cleaned or f"do not {phrase}" in cleaned:
                continue
            return True

    single_word_triggers = {"bye", "byy", "goodbye", "hangup"}
    if any(token in single_word_triggers for token in tokens):
        if "dont" in tokens or "not" in tokens:
            return False
        return True

    return False


class ElevenLabsIntegration:
    """Manages an active ElevenLabs Conversational AI session for an AgentDuet call."""

    def __init__(
        self,
        call: Call,
        agent_id: str,
        api_key: Optional[str] = None,
    ):
        self._call = call
        self._agent_id = agent_id
        self._api_key = api_key
        self._audio_interface: Optional[AgentDuetAsyncAudioInterface] = None
        self._conversation: Optional[AsyncConversation] = None
        self._terminated = False
        self._hangup_scheduled = False

    async def _agent_hang_up(self, reason: str = "caller request") -> None:
        """Waits for the agent to finish speaking farewell, then closes call and session."""
        if self._hangup_scheduled or self._terminated:
            return
        self._hangup_scheduled = True
        logger.info("Hangup initiated (%s) for call %s", reason, self._call.id)

        if self._audio_interface:
            self._audio_interface.start_farewell_tracking()
            await self._audio_interface.wait_for_farewell_complete()
        else:
            await asyncio.sleep(2.0)

        if not self._terminated:
            self._terminated = True
            logger.info("Farewell speech completed: closing call %s", self._call.id)
            if self._conversation:
                try:
                    await self._conversation.end_session()
                except Exception:
                    pass
            result = await self._call.close()
            if not result:
                logger.error(
                    "Hang up failed for %s: %s (%s)",
                    self._call.id,
                    result.error_message,
                    result.error_code,
                )

    async def _on_hangup(self, evt):
        """Handles call termination from either caller or agent."""
        logger.info("Call hangup received: terminating ElevenLabs session")
        self._terminated = True
        if self._conversation:
            try:
                await self._conversation.end_session()
            except Exception:
                logger.exception("Error ending ElevenLabs conversation session")

    async def run(self):
        """Binds call lifecycle, initializes audio interface, and runs the conversation."""
        self._call.on_hangup(self._on_hangup)

        client = ElevenLabs(api_key=self._api_key)
        self._audio_interface = AgentDuetAsyncAudioInterface(self._call)

        client_tools = ClientTools()

        async def hang_up_tool(parameters: dict):
            logger.info("ElevenLabs agent invoked 'hang_up' tool")
            asyncio.create_task(self._agent_hang_up(reason="agent tool invocation"))
            return {"status": "hanging_up"}

        client_tools.register("hang_up", hang_up_tool, is_async=True)

        async def handle_agent_response(response: str):
            logger.info("Agent: %s", response)

        async def handle_user_transcript(transcript: str):
            logger.info("User: %s", transcript)
            if should_hang_up(transcript):
                asyncio.create_task(
                    self._agent_hang_up(reason=f"caller said '{transcript}'")
                )

        async def handle_latency(latency: int):
            logger.debug("Roundtrip latency: %d ms", latency)

        self._conversation = AsyncConversation(
            client=client,
            agent_id=self._agent_id,
            requires_auth=bool(self._api_key),
            audio_interface=self._audio_interface,
            client_tools=client_tools,
            callback_agent_response=handle_agent_response,
            callback_user_transcript=handle_user_transcript,
            callback_latency_measurement=handle_latency,
        )

        logger.info("Starting ElevenLabs conversation session for call %s", self._call.id)
        await self._conversation.start_session()

        try:
            conversation_id = await self._conversation.wait_for_session_end()
            logger.info("ElevenLabs conversation finished, ID: %s", conversation_id)
        except asyncio.CancelledError:
            logger.debug("ElevenLabs conversation task cancelled")
        except Exception:
            logger.exception("Error during ElevenLabs conversation execution")
        finally:
            if not self._terminated:
                await self._call.close()


async def main():
    agent_id = os.getenv("ELEVENLABS_AGENT_ID")
    if not agent_id:
        raise ValueError("ELEVENLABS_AGENT_ID environment variable must be set")

    api_key = os.getenv("ELEVENLABS_API_KEY")

    # ElevenLabs Conversational AI utilizes 16 kHz 16-bit Linear PCM audio
    config = SessionManagerConfig.create(
        api_key=os.getenv("AGENTDUET_API_KEY"),
        connector_uuid=os.getenv("AGENTDUET_CONNECTOR_UUID"),
        call_audio=CallAudioConfig(
            sample_rate=16000,
            buffer_size=1024 * 1024,  # 1MB ring buffer
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

                integration = ElevenLabsIntegration(
                    call=call,
                    agent_id=agent_id,
                    api_key=api_key,
                )
                await integration.run()
            except Exception:
                logger.exception("Error handling call with ElevenLabs integration")
                await call.close()
                raise

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
