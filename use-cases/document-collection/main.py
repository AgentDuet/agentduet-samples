"""
Outbound Remittance Notification Agent — Omni Bank + AgentDuet + Gemini Live.

An automated outbound voice compliance agent acting on behalf of Omni Bank to notify
remote employees and contractors of incoming foreign remittances ($4,250.00 USD),
prompting them to log in to portal.omnibank.com, declare regulatory Purpose Codes
(e.g. P0802 Software Consultancy), upload supporting invoices, and cleanly disconnect.

Latency Optimizations:
- Pre-connects to Gemini Live during outbound dialing to eliminate connection latency on answer
- Non-blocking binary audio streaming with instant interruption flushing
- Short, punchy conversational turns
- 3.5s grace period on hangup ensures complete closing phrase playback
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession

from agentduet import (
    BufferFullError,
    Call,
    CallAudioConfig,
    CallClosedError,
    SessionManager,
    SessionManagerConfig,
)

try:
    from agentduet import Address, new_session_id
except ImportError:
    def new_session_id() -> str:
        return uuid.uuid4().hex

    class Address:  # type: ignore
        @staticmethod
        def telco(number: str) -> str:
            return number

        @staticmethod
        def sip(uri: str) -> str:
            return uri

from prompts import (
    BANK_NAME,
    CONFIRM_NOTIFICATION_TOOL,
    END_CALL_TOOL,
    PORTAL_URL,
    REMITTANCE_AMOUNT,
    REMITTANCE_REF,
    SENDER_NAME,
    SYSTEM_INSTRUCTION,
)
from tools import (
    RemittanceNotificationRecord,
    handle_confirm_notification_tool,
    log_outbound_disposition,
    save_remittance_record,
)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("remittance_notification")

SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "24000"))
MODEL = os.getenv("GEMINI_MODEL", "models/gemini-3.1-flash-live-preview")
VOICE_NAME = os.getenv("GEMINI_VOICE", "Zephyr")
HANDOFF_GRACE_SECONDS = float(os.getenv("HANDOFF_AUDIO_GRACE_SECONDS", "3.5"))

genai_client = genai.Client(
    vertexai=False,
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
)


class GeminiLiveRemittanceBridge:
    """Ultra-responsive conversational audio bridge for Omni Bank Remittance Notifications."""

    def __init__(
        self,
        call: Call,
        live: AsyncSession,
        remittance_id: str,
        recipient_phone: str,
        amount_display: str,
        sender_name: str,
    ) -> None:
        self._call = call
        self._live = live
        self._remittance_id = remittance_id
        self._recipient_phone = recipient_phone
        self._amount_display = amount_display
        self._sender_name = sender_name
        self._terminated = False
        self._end_call = False
        self._tasks: list[asyncio.Task] = []
        self._start_time = asyncio.get_running_loop().time()
        self._record_saved = False

    async def _on_hangup(self, _evt: Any) -> None:
        logger.info("Recipient hung up call %s", self._call.id)
        self._terminated = True
        duration = asyncio.get_running_loop().time() - self._start_time
        if not self._record_saved:
            logger.info("Call ended before notification acknowledgment; logging partial disposition.")
            partial_rec = RemittanceNotificationRecord(
                remittance_id=self._remittance_id,
                recipient_phone=self._recipient_phone,
                amount_display=self._amount_display,
                sender_name=self._sender_name,
                call_status="INCOMPLETE",
                disposition="RECIPIENT_HANGUP",
                call_duration_seconds=round(duration, 2),
            )
            save_remittance_record(partial_rec)
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)

        recipient_party = self._call.callee or self._call.caller

        to_model = asyncio.create_task(self._to_model(recipient_party))
        from_model = asyncio.create_task(self._from_model())
        self._tasks = [to_model, from_model]

        # Trigger immediate opening notice
        await self._live.send_realtime_input(
            text=(
                f"The phone call is connected. Greet the recipient warmly: "
                f"'Hi there! I'm calling from {BANK_NAME}'s remittance processing team regarding your incoming foreign remittance of {REMITTANCE_AMOUNT} from {SENDER_NAME}. "
                f"Do you have a quick moment?'"
            )
        )

        await asyncio.gather(to_model, from_model, return_exceptions=True)

    async def _to_model(self, party: Any) -> None:
        """Stream recipient microphone audio directly to Gemini Live."""
        try:
            async for chunk in party.audio_stream():
                if self._terminated:
                    break
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("Error streaming recipient audio to Gemini Live")

    async def _from_model(self) -> None:
        """Receive synthesized audio and handle tool calls from Gemini Live."""
        try:
            while not self._terminated:
                async for response in self._live.receive():
                    sc = response.server_content
                    if sc and sc.interrupted:
                        logger.debug("Recipient spoke; clearing audio buffer immediately")
                        await self._call.clear_send_audio_buffer()
                        break

                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Buffer full - dropping audio chunk")
                                except CallClosedError:
                                    return

                    # Handle Tool Calls
                    if response.tool_call:
                        responses = []
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})

                            if fc.name == "confirm_notification_delivered":
                                logger.info("⚡ [TOOL CALL] confirm_notification_delivered(%s)", args)
                                duration = (
                                    asyncio.get_running_loop().time() - self._start_time
                                )
                                tool_result, _ = handle_confirm_notification_tool(
                                    remittance_id=self._remittance_id,
                                    recipient_phone=self._recipient_phone,
                                    amount_display=self._amount_display,
                                    sender_name=self._sender_name,
                                    args=args,
                                    duration_seconds=duration,
                                )
                                self._record_saved = True
                                responses.append(
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response=tool_result,
                                    )
                                )

                            elif fc.name == "end_call":
                                logger.info("⚡ [TOOL CALL] end_call triggered by agent.")
                                self._end_call = True
                                responses.append(
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={"ok": True},
                                    )
                                )

                        if responses:
                            await self._live.send_tool_response(
                                function_responses=responses
                            )

                        if self._end_call:
                            logger.info(
                                "Closing turn active — waiting %.1fs for speech playback to finish completely...",
                                HANDOFF_GRACE_SECONDS,
                            )
                            await asyncio.sleep(HANDOFF_GRACE_SECONDS)
                            logger.info("👋 Closing speech complete. Disconnecting call cleanly...")
                            self._terminated = True
                            try:
                                await self._call.disconnect()
                            except Exception:
                                pass
                            return
        except CallClosedError:
            pass
        except genai_errors.APIError as e:
            logger.debug("Gemini Live API error: %s - %s", e.code, e.message)
        except Exception:
            if not self._terminated:
                logger.exception("Error receiving from Gemini Live")


async def dial_recipient_remittance_notice(
    sm: SessionManager,
    subscriber: str,
    recipient_phone: str,
    remittance_id: str = REMITTANCE_REF,
    amount_display: str = REMITTANCE_AMOUNT,
    sender_name: str = SENDER_NAME,
) -> RemittanceNotificationRecord:
    """Place an outbound remittance call with pre-connected Gemini Live session for instant pickup."""
    logger.info(
        "📞 Dialing remittance compliance call (%s - %s) to recipient %s (originating: %s)...",
        remittance_id,
        amount_display,
        recipient_phone,
        subscriber,
    )

    session = await sm.open_session(new_session_id(), subscriber)
    call = await session.make_call(Address.telco(recipient_phone))

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[
            types.Tool(
                function_declarations=[
                    CONFIRM_NOTIFICATION_TOOL,
                    END_CALL_TOOL,
                ]
            )
        ],
    )

    # Pre-connect to Gemini Live so WebSocket handshake happens while the phone is ringing
    async with genai_client.aio.live.connect(model=MODEL, config=config) as live:
        logger.info("⚡ Pre-connected Gemini Live session. Initiating call dial...")
        dial_result = await call.dial(ring_time_seconds=25)

        if dial_result:
            logger.info("✅ Recipient answered call %s. Instantly starting voice bridge...", call.id)
            bridge = GeminiLiveRemittanceBridge(
                call=call,
                live=live,
                remittance_id=remittance_id,
                recipient_phone=recipient_phone,
                amount_display=amount_display,
                sender_name=sender_name,
            )
            try:
                await bridge.run()
            finally:
                try:
                    await call.disconnect()
                except Exception:
                    pass
            return RemittanceNotificationRecord(
                remittance_id=remittance_id,
                recipient_phone=recipient_phone,
                amount_display=amount_display,
                sender_name=sender_name,
                call_status="COMPLETED",
                disposition="CONNECTED",
            )
        else:
            disposition = dial_result.error_code or "CALL_UNANSWERED"
            logger.info(
                "ℹ️ Outbound call for Remittance %s was not answered (Outcome: %s). "
                "Normal disposition logged; queued for scheduled automated retry.",
                remittance_id,
                disposition,
            )
            record = log_outbound_disposition(
                remittance_id=remittance_id,
                recipient_phone=recipient_phone,
                amount_display=amount_display,
                sender_name=sender_name,
                disposition_code=disposition,
            )
            try:
                await call.close()
            except Exception:
                pass
            return record


async def main() -> None:
    api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")
    subscriber = os.getenv("AGENTDUET_SUBSCRIBER")
    destination = os.getenv("DESTINATION_NUMBER")

    if not api_key or not connector_uuid:
        logger.error("AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID must be set.")
        return

    if not subscriber or not destination:
        logger.error("Set AGENTDUET_SUBSCRIBER (originating line) and DESTINATION_NUMBER (recipient).")
        return

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        logger.error("GEMINI_API_KEY must be set to run Gemini Live.")
        return

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=SAMPLE_RATE,
            buffer_size=1024 * 1024,
        ),
    )

    async with SessionManager(config) as sm:
        logger.info("Omni Bank Foreign Remittance Outbound Compliance Agent online.")
        await dial_recipient_remittance_notice(
            sm=sm,
            subscriber=subscriber,
            recipient_phone=destination,
            remittance_id="REM-98214-USD",
            amount_display="$4,250.00 USD",
            sender_name="Acme Global Corp",
        )
        logger.info("Remittance compliance notification job completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Remittance notification agent shutting down.")
