import asyncio
import logging
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

from n8n_client import N8nReservationClient

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
DEFAULT_VOICE = "Aoede"

RESTAURANT_SYSTEM_INSTRUCTION = (
    "You are Bella, a warm, polite, and efficient AI hostess at Bella Vista Italian Bistro. "
    "Your goal is to help telephone callers reserve a dining table.\n\n"
    "Step 1: Greet the caller warmly: 'Thank you for calling Bella Vista Bistro! My name is Bella. "
    "I would be delighted to help you reserve a table. What date and time are you looking to join us?'\n\n"
    "Step 2: Collect the party size (number of guests), the guest's name, and any special requests "
    "(such as outdoor seating, high chairs, or dietary notes).\n\n"
    "Step 3: Once you have the guest's name, party size, and reservation time, invoke the "
    "`book_restaurant_table` tool to record the booking in n8n and Google Sheets.\n\n"
    "Step 4: When the tool returns the confirmation code, confirm the reservation clearly: "
    "'Table for {party_size} at {reservation_time} under {guest_name}. You are all set! "
    "Your confirmation code is {confirmation_code}. We look forward to hosting you!'\n\n"
    "Step 5: Ask if there is anything else you can help with before wishing them a wonderful day.\n\n"
    "Tone & Style:\n"
    "- Keep responses brief, conversational, and natural for telephone speech.\n"
    "- Never invent confirmation codes; always use the code returned by the tool.\n"
    "- If the caller interrupts, stop speaking immediately and listen."
)


def get_reservation_tool_declarations() -> List[types.FunctionDeclaration]:
    """Returns Gemini Live tool declarations mapped to n8n webhooks."""
    return [
        types.FunctionDeclaration(
            name="book_restaurant_table",
            description=(
                "Sends confirmed table reservation details to n8n to append a row in Google Sheets "
                "and generate a reservation confirmation code."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "guest_name": {
                        "type": "string",
                        "description": "Full name of the guest booking the table.",
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of people in the dining party.",
                    },
                    "reservation_time": {
                        "type": "string",
                        "description": "Requested date and time (for example, 'Tonight 7:30 PM' or 'Friday Oct 12 at 8 PM').",
                    },
                    "special_requests": {
                        "type": "string",
                        "description": "Optional notes such as window booth, patio seating, birthday, or allergies.",
                    },
                },
                "required": ["guest_name", "party_size", "reservation_time"],
            },
        ),
    ]


def build_gemini_config(voice_name: str = DEFAULT_VOICE) -> types.LiveConnectConfig:
    """Builds the Gemini Live configuration with speech and n8n tools."""
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        system_instruction=RESTAURANT_SYSTEM_INSTRUCTION,
        tools=[types.Tool(function_declarations=get_reservation_tool_declarations())],
    )


class RestaurantReservationVoiceAgent:
    """Conversational voice agent connecting AgentDuet telephony with Gemini Live

    speech model and n8n webhook automation.
    """

    def __init__(
        self,
        call: Call,
        gemini_session: AsyncSession,
        n8n_client: N8nReservationClient,
        caller_phone: str = "",
    ):
        self._call = call
        self._gemini = gemini_session
        self._n8n = n8n_client
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

        # Nudge Gemini Live to deliver the opening restaurant greeting first
        await self._gemini.send_client_content(
            turns=types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            "The caller has just connected on the phone line. "
                            "Greet them warmly as Bella from Bella Vista Bistro and ask "
                            "what date and time they would like to reserve a table for."
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
        """Receives Gemini Live audio output and routes tool calls to n8n."""
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
        """Dispatches Gemini function calls directly to the n8n webhook."""
        if tool_name == "book_restaurant_table":
            guest_name = str(args.get("guest_name", "Guest"))
            party_size = int(args.get("party_size", 2))
            reservation_time = str(args.get("reservation_time", "Tonight 7:00 PM"))
            special_requests = str(args.get("special_requests", "None"))
            phone_number = self._caller_phone or "+15551234567"

            return await self._n8n.book_table(
                guest_name=guest_name,
                party_size=party_size,
                reservation_time=reservation_time,
                phone_number=phone_number,
                special_requests=special_requests,
            )

        return {"error": f"Unknown tool: {tool_name}"}
