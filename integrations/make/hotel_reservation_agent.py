import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from agentduet import (
    BufferFullError,
    Call,
    CallClosedError,
)
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from websockets import ConnectionClosed

from make_client import MakeHotelClient

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
DEFAULT_VOICE = "Zephyr"

HOTEL_SYSTEM_INSTRUCTION = (
    "You are a warm, professional voice reservations concierge for Grand Horizon Hotel. "
    "Your goal is to assist inbound callers with booking new room stays over the telephone. "
    "Keep responses conversational, concise, and natural for telephone speech.\n\n"
    "Step 1: Greet the guest warmly and ask for their check-in date, check-out date, and number of guests.\n\n"
    "Step 2: When the guest provides their dates and number of guests, you MUST call the "
    "`check_room_availability` tool before stating whether any room is open. Never fabricate "
    "availability or assume rooms are open without calling the tool.\n\n"
    "Step 3: If multiple room types are available, present them together side by side with their "
    "nightly rates (for example, 'We have Deluxe rooms at $150 a night, or Executive Suites at "
    "$250 a night') so the guest can choose.\n\n"
    "Step 4: If the tool returns that no rooms are available, tell the guest honestly that the hotel "
    "is completely booked for those dates. Offer to check different dates.\n\n"
    "Step 5: Once the guest chooses a room, confirm their choice and collect their full name and "
    "email address.\n\n"
    "Step 6: Once name and email are provided, invoke the `create_hotel_booking` tool to record "
    "the reservation in Google Sheets and trigger the confirmation email.\n\n"
    "Step 7: Verbally confirm the booking reference code, mention that the confirmation email is "
    "on its way to their inbox, and ask if there is anything else you can help with before "
    "ending the call naturally.\n\n"
    "Constraints:\n"
    "- Inbound calls only.\n"
    "- New bookings only (no changes or cancellations).\n"
    "- No payment or credit card collection over the phone."
)


def get_hotel_tool_declarations() -> List[types.FunctionDeclaration]:
    """Returns Gemini Live tool declarations mapped to Make.com webhooks."""
    return [
        types.FunctionDeclaration(
            name="check_room_availability",
            description=(
                "Queries Make.com for live hotel room inventory and rates across the requested dates. "
                "Must be called before telling the guest any room is available."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "check_in": {
                        "type": "string",
                        "description": "Guest arrival or check-in date.",
                    },
                    "check_out": {
                        "type": "string",
                        "description": "Guest departure or check-out date.",
                    },
                    "guests": {
                        "type": "integer",
                        "description": "Number of guests staying in the room.",
                    },
                },
                "required": ["check_in", "check_out"],
            },
        ),
        types.FunctionDeclaration(
            name="create_hotel_booking",
            description=(
                "Sends confirmed booking details to Make.com to record the row in Google Sheets "
                "(locking the dates and room type) and dispatch the confirmation email."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "guest_name": {
                        "type": "string",
                        "description": "Full name of the primary guest.",
                    },
                    "guest_email": {
                        "type": "string",
                        "description": "Email address for sending the booking confirmation.",
                    },
                    "check_in": {
                        "type": "string",
                        "description": "Check-in date.",
                    },
                    "check_out": {
                        "type": "string",
                        "description": "Check-out date.",
                    },
                    "guests": {
                        "type": "integer",
                        "description": "Number of guests.",
                    },
                    "room_type": {
                        "type": "string",
                        "description": "Selected room type (e.g. Deluxe Room or Executive Suite).",
                    },
                    "nightly_rate": {
                        "type": "number",
                        "description": "Quoted nightly rate in USD.",
                    },
                },
                "required": [
                    "guest_name",
                    "guest_email",
                    "check_in",
                    "check_out",
                    "room_type",
                    "nightly_rate",
                ],
            },
        ),
    ]


def build_gemini_config(voice_name: str = DEFAULT_VOICE) -> types.LiveConnectConfig:
    """Builds the Gemini Live configuration with speech and Make.com tools."""
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        system_instruction=HOTEL_SYSTEM_INSTRUCTION,
        tools=[types.Tool(function_declarations=get_hotel_tool_declarations())],
    )


class HotelReservationVoiceAgent:
    """Conversational voice agent connecting AgentDuet telephony with Gemini Live

    speech model and Make.com webhook automation.
    """

    def __init__(
        self,
        call: Call,
        gemini_session: AsyncSession,
        make_client: MakeHotelClient,
        caller_phone: str = "",
    ):
        self._call = call
        self._gemini = gemini_session
        self._make = make_client
        self._caller_phone = caller_phone
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._terminated = False

    async def _on_hangup(self, _evt: Any) -> None:
        """Cleans up audio tasks and Gemini session on call hangup."""
        logger.info("Call %s hangup event received", self._call.id)
        self._terminated = True
        try:
            await self._gemini.close()
        except Exception:
            pass

        for task in (self._send_task, self._recv_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, genai_errors.APIError):
                    pass

    async def run(self) -> None:
        """Starts bidirectional audio streaming between AgentDuet and Gemini Live."""
        self._call.on_hangup(self._on_hangup)

        # Nudge Gemini Live to deliver the opening phone greeting first
        await self._gemini.send_client_content(
            turns=types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            "The guest has just connected on the phone line. "
                            "Greet them warmly as Grand Horizon Hotel Reservations and ask "
                            "for their check-in date, check-out date, and number of guests."
                        )
                    )
                ],
            ),
            turn_complete=True,
        )

        self._send_task = asyncio.create_task(self._stream_to_gemini())
        self._recv_task = asyncio.create_task(self._receive_from_gemini())

        await asyncio.gather(self._send_task, self._recv_task, return_exceptions=True)

    async def _stream_to_gemini(self) -> None:
        """Pipes incoming caller audio chunks from AgentDuet to Gemini Live."""
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._gemini.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except ConnectionClosed:
            pass
        except Exception as e:
            if not self._terminated:
                logger.exception("Error streaming caller audio to Gemini: %s", e)

    async def _receive_from_gemini(self) -> None:
        """Receives Gemini Live audio output and routes tool calls to Make.com."""
        try:
            while not self._terminated:
                async for response in self._gemini.receive():
                    if self._terminated:
                        return

                    # Handle tool calls triggered by the LLM
                    if response.tool_call:
                        responses = []
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})
                            logger.info("LLM triggered tool: %s with args %s", fc.name, args)
                            tool_result = await self.dispatch_tool(fc.name, args)
                            responses.append(
                                types.FunctionResponse(
                                    id=fc.id,
                                    name=fc.name,
                                    response=tool_result,
                                )
                            )
                        await self._gemini.send_tool_response(function_responses=responses)
                        continue

                    server_content = response.server_content
                    if not server_content:
                        continue

                    # Handle caller barge-in / interruption: flush buffer instantly
                    if server_content.interrupted:
                        logger.debug("Caller interrupted agent: clearing audio buffer")
                        await self._call.clear_send_audio_buffer()
                        break

                    # Pipe synthesized model voice back to caller telephone
                    if server_content.model_turn:
                        for part in server_content.model_turn.parts or []:
                            if part.inline_data and isinstance(part.inline_data.data, bytes):
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Send audio buffer full, dropping chunk")
                                except CallClosedError:
                                    return
        except (ConnectionClosed, genai_errors.APIError):
            logger.debug("Gemini session closed normally")
        except asyncio.CancelledError:
            raise
        except CallClosedError:
            logger.debug("Telephone call closed by caller")
        except Exception as e:
            if not self._terminated:
                logger.exception("Error receiving from Gemini: %s", e)

    async def dispatch_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches Gemini function calls directly to Make.com webhooks."""
        if tool_name == "check_room_availability":
            check_in = str(args.get("check_in", ""))
            check_out = str(args.get("check_out", ""))
            guests = int(args.get("guests", 1))
            return await self._make.check_availability(
                check_in=check_in,
                check_out=check_out,
                guests=guests,
            )

        if tool_name == "create_hotel_booking":
            booking_payload = dict(args)
            if not booking_payload.get("caller_phone"):
                booking_payload["caller_phone"] = self._caller_phone
            return await self._make.create_booking(booking_payload)

        return {"error": f"Unknown tool: {tool_name}"}
