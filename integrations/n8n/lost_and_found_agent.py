"""Amazon Nova 2 Sonic conversational voice agent for City Cab Lost and Found.

Bridges AgentDuet telephony audio bidirectionally to Amazon Nova 2 Sonic
(amazon.nova-2-sonic-v1:0) on AWS Bedrock. Maya is the voice persona.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from agentduet import BufferFullError, Call, CallClosedError

from n8n_client import N8nClient

logger = logging.getLogger(__name__)

MODEL_ID = os.getenv("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
VOICE_ID = os.getenv("NOVA_SONIC_VOICE_ID", "amy")
SAMPLE_RATE = 24000


class NovaSonicLostAndFoundBridge:
    """Manages bidirectional speech-to-speech with Amazon Nova 2 Sonic for a single phone call."""

    def __init__(
        self,
        call: Call,
        n8n_client: N8nClient,
        caller_phone: str,
        pending_case: Optional[Dict[str, Any]] = None,
        dispatcher: Optional[Any] = None,
        mock_mode: Optional[bool] = None,
    ):
        self.call = call
        self.n8n_client = n8n_client
        self.caller_phone = caller_phone
        self.pending_case = pending_case
        self.dispatcher = dispatcher

        # Session tracking IDs for Nova Sonic event protocol
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.kickoff_content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self._client: Any = None
        self._stream: Any = None
        self._is_active = False
        self._closing = False
        self._send_lock = asyncio.Lock()

        # Telephony and intake data tracking
        self.collected_intake_data: Optional[Dict[str, Any]] = None
        self.case_action_performed: Optional[Dict[str, Any]] = None
        self.audio_bytes_sent_to_caller = 0
        self.audio_chunks_received_from_caller = 0
        self.transcript_lines: List[str] = []

        # Auto-detect mock mode if AWS credentials not set
        has_aws_creds = bool(
            os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        env_mock = os.getenv("LOST_FOUND_MOCK_MODE", "").lower() in ("true", "1", "yes")
        if mock_mode is not None:
            self.mock_mode = mock_mode
        else:
            self.mock_mode = env_mock or not has_aws_creds

        self._mock_downlink_queue: asyncio.Queue = asyncio.Queue()

    def build_system_prompt(self) -> str:
        """Builds system instructions for Maya guiding the telephone intake."""
        if self.pending_case:
            case_id = self.pending_case.get("id", "Unknown")
            item = self.pending_case.get("item_type", "item")
            time_str = self.pending_case.get("approx_trip_time", "your recent trip")
            return (
                "You are Maya, the warm and professional voice assistant for City Cab Lost and Found on a live phone call. "
                f"Caller phone number: {self.caller_phone}.\n"
                f"ACTIVE CASE CONTEXT: The caller has an active pending case #{case_id} regarding a {item} from {time_str}.\n"
                "CONVERSATIONAL INSTRUCTIONS:\n"
                f"1. Greet the caller warmly as Maya, state that you see their active case #{case_id} regarding a {item}, and ask them to verify 1 or 2 distinguishing details (such as color, brand, or contents) to verify their identity.\n"
                "2. If the caller confirms the details match, inform them that their case is confirmed and resolved. Tell them they can pick up the item at Central Dispatch Depot at 100 Main Street between 8:00 AM and 6:00 PM with a valid ID.\n"
                "3. If the caller states the item is not theirs, inform them you will keep their case open and continue scanning driver turn-ins.\n"
                "4. When the verification concludes, emit a structured tag:\n"
                f'<CASE_ACTION>{{"case_id": {case_id}, "status": "resolved", "notes": "Customer verified details over phone"}}</CASE_ACTION>\n'
                f'(or "status": "open" if rejected).\n'
                "Keep spoken sentences short (1-2 sentences per turn), natural, and clear."
            )

        return (
            "You are Maya, the warm and professional voice assistant for City Cab Lost and Found on a live phone call. "
            f"Caller phone number: {self.caller_phone}.\n"
            "CONVERSATIONAL INSTRUCTIONS:\n"
            "1. In your opening turn, warmly introduce yourself as Maya and ask if the caller is a driver reporting a found item, or a customer reporting a lost item.\n"
            "2. DRIVER INTAKE FLOW: Ask for item type, two distinguishing details (color, brand, markings), approximate trip time or shift, route or pickup/dropoff area, and seat position in the cab. Thank them for their honesty.\n"
            "3. CUSTOMER INTAKE FLOW: Ask for item type, two distinguishing details, approximate trip time or date, route or pickup/dropoff area, and their Ride ID (from their trip receipt or SMS). If they do not have the Ride ID, proceed gracefully without it. Emphasize clearly: 'Matches are not confirmed live on this call; our automated system will scan driver reports and notify you by phone if a potential match is found.'\n"
            "4. When all details have been collected, emit a structured tag in your output text:\n"
            f'<INTAKE_DATA>{{"role": "driver", "item_type": "...", "distinguishing_detail_1": "...", "distinguishing_detail_2": "...", "approx_trip_time": "...", "route_area": "...", "seat_position": "...", "callback_number": "{self.caller_phone}"}}</INTAKE_DATA>\n'
            f'or for customer: <INTAKE_DATA>{{"role": "customer", "item_type": "...", "distinguishing_detail_1": "...", "distinguishing_detail_2": "...", "approx_trip_time": "...", "route_area": "...", "ride_id": "...", "callback_number": "{self.caller_phone}"}}</INTAKE_DATA>\n'
            "Keep spoken responses concise (1-2 sentences per turn), natural, and direct for a telephone call."
        )

    def build_kickoff_text(self) -> str:
        """Synthetic kickoff text prompting Maya to greet the caller immediately upon connection."""
        if self.pending_case:
            item = self.pending_case.get("item_type", "item")
            return f"The call is connected. Greet the caller as Maya from City Cab Lost and Found, reference their open case regarding the {item}, and ask them to verify details."
        return "The call is connected. Greet the caller as Maya from City Cab Lost and Found and ask if they are a driver reporting a found item or a customer reporting a lost item."

    async def _send_event(self, event_dict: Dict[str, Any]) -> None:
        """Sends a JSON-encoded event chunk to the Bedrock bidirectional stream."""
        if not self._stream or not self._is_active:
            return

        from aws_sdk_bedrock_runtime.models import (
            BidirectionalInputPayloadPart,
            InvokeModelWithBidirectionalStreamInputChunk,
        )

        payload = json.dumps(event_dict)
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload.encode("utf-8"))
        )
        async with self._send_lock:
            await self._stream.input_stream.send(chunk)

    async def start(self) -> None:
        """Initializes Bedrock client and starts the Nova 2 Sonic bidirectional session."""
        self._is_active = True
        logger.info(
            "Starting Nova Sonic session for call %s (mock_mode=%s)",
            self.call.id,
            self.mock_mode,
        )

        if self.mock_mode:
            greeting_text = (
                f"Hello! This is Maya from City Cab Lost and Found. I see you have an open case regarding a {self.pending_case.get('item_type')}. Could you confirm details?"
                if self.pending_case
                else "Thank you for calling City Cab Lost and Found. My name is Maya. Are you a driver reporting a found item, or a customer reporting a lost item?"
            )
            mock_pcm = b"\x00\x00" * 4800
            await self._mock_downlink_queue.put(
                {
                    "event": {
                        "textOutput": {"content": greeting_text},
                        "audioOutput": {"content": base64.b64encode(mock_pcm).decode("utf-8")},
                    }
                }
            )
            return

        from aws_sdk_bedrock_runtime.client import (
            AsyncBedrockRuntimeClient,
            InvokeModelWithBidirectionalStreamOperationInput,
        )
        from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
        from smithy_http.aio.crt import AWSCRTHTTPClient

        transport = AWSCRTHTTPClient()
        aws_config = await AsyncBedrockRuntimeConfig.resolve(
            endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
            region=REGION,
            transport=transport,
        )
        self._client = AsyncBedrockRuntimeClient(config=aws_config)

        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )

        # 1. Session Start
        await self._send_event(
            {
                "event": {
                    "sessionStart": {
                        "inferenceConfiguration": {
                            "maxTokens": 1024,
                            "topP": 0.9,
                            "temperature": 0.7,
                        },
                        "turnDetectionConfiguration": {
                            "endpointingSensitivity": "HIGH",
                        },
                    }
                }
            }
        )

        # 2. Prompt Start with 24 kHz audio output
        await self._send_event(
            {
                "event": {
                    "promptStart": {
                        "promptName": self.prompt_name,
                        "textOutputConfiguration": {"mediaType": "text/plain"},
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": SAMPLE_RATE,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": VOICE_ID,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                    }
                }
            }
        )

        # 3. System Prompt
        await self._send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "type": "TEXT",
                        "interactive": False,
                        "role": "SYSTEM",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "content": self.build_system_prompt(),
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                    }
                }
            }
        )

        # 4. Immediate Kickoff Text so Maya greets first
        await self._send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.kickoff_content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "USER",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.kickoff_content_name,
                        "content": self.build_kickoff_text(),
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.kickoff_content_name,
                    }
                }
            }
        )

        # 5. Audio content channel for caller microphone stream
        await self._send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": SAMPLE_RATE,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )
        logger.info("Nova Sonic bidirectional session setup complete for call %s", self.call.id)

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Pipes raw caller audio chunk to Nova Sonic."""
        if not self._is_active or not pcm_chunk:
            return

        self.audio_chunks_received_from_caller += 1

        if self.mock_mode:
            if self.audio_chunks_received_from_caller % 3 == 0:
                mock_pcm = b"\x00\x00" * 2400
                await self._mock_downlink_queue.put(
                    {
                        "event": {
                            "textOutput": {"content": "Understood, recording your report."},
                            "audioOutput": {
                                "content": base64.b64encode(mock_pcm).decode("utf-8")
                            },
                        }
                    }
                )
            return

        encoded = base64.b64encode(pcm_chunk).decode("utf-8")
        await self._send_event(
            {
                "event": {
                    "audioInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "content": encoded,
                    }
                }
            }
        )

    async def process_responses(self) -> None:
        """Receives audio and text events from Nova Sonic and plays them to the caller."""
        logger.info("Starting Nova Sonic response listener for call %s", self.call.id)

        try:
            while self._is_active:
                if self.mock_mode:
                    try:
                        event_data = await asyncio.wait_for(
                            self._mock_downlink_queue.get(), timeout=0.05
                        )
                        if event_data is None:
                            break
                        await self._handle_event(event_data.get("event", {}))
                    except asyncio.TimeoutError:
                        if not self._is_active:
                            break
                        continue
                    continue

                if not self._stream:
                    await asyncio.sleep(0.05)
                    continue

                try:
                    output = await self._stream.await_output()
                    result = await output[1].receive()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    if self._is_active:
                        logger.warning("Nova stream receive error on call %s: %s", self.call.id, exc)
                    break

                if not result.value or not result.value.bytes_:
                    continue

                try:
                    data = json.loads(result.value.bytes_.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                event = data.get("event", {})
                await self._handle_event(event)

        except (CallClosedError, asyncio.CancelledError):
            pass
        except Exception as e:
            if self._is_active:
                logger.exception("Error in process_responses for call %s: %s", self.call.id, e)
        finally:
            logger.info(
                "Nova Sonic response listener finished for call %s (audio_sent=%d bytes)",
                self.call.id,
                self.audio_bytes_sent_to_caller,
            )

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """Handles parsed Nova Sonic output event."""
        # 1. Text Output: transcripts, interruption markers, structured intake tags
        if "textOutput" in event:
            text = event["textOutput"].get("content", "")
            if text:
                self.transcript_lines.append(text)

                # Barge-in detection: clear outbound audio buffer immediately
                if '{ "interrupted" : true }' in text or '{"interrupted": true}' in text or '"interrupted"' in text:
                    logger.info("Caller barge-in detected on call %s. Clearing outbound audio buffer.", self.call.id)
                    try:
                        await self.call.clear_send_audio_buffer()
                    except CallClosedError:
                        pass
                    return

                # Check for structured intake tag
                await self._check_for_intake_payload(text)

                # Check for structured case action tag
                await self._check_for_case_action_payload(text)

        # 2. Audio Output: stream speech to the caller's phone line
        if "audioOutput" in event:
            audio_b64 = event["audioOutput"].get("content")
            if audio_b64:
                pcm_bytes = base64.b64decode(audio_b64)
                self.audio_bytes_sent_to_caller += len(pcm_bytes)

                try:
                    await self.call.send_audio(pcm_bytes)
                except BufferFullError:
                    logger.warning("Send audio buffer full for call %s; dropping chunk", self.call.id)
                except CallClosedError:
                    self._is_active = False

        # 3. Content End: handle interrupted reason
        if "contentEnd" in event:
            stop_reason = event["contentEnd"].get("stopReason")
            if stop_reason == "INTERRUPTED":
                logger.info("ContentEnd INTERRUPTED for call %s; clearing buffer", self.call.id)
                try:
                    await self.call.clear_send_audio_buffer()
                except CallClosedError:
                    pass

    async def _check_for_intake_payload(self, text: str) -> None:
        """Extracts structured intake data and dispatches to n8n intake webhook."""
        if "<INTAKE_DATA>" not in text or "</INTAKE_DATA>" not in text:
            return

        match = re.search(r"<INTAKE_DATA>(.*?)</INTAKE_DATA>", text, re.DOTALL)
        if not match:
            return

        try:
            payload = json.loads(match.group(1).strip())
            role = payload.get("role", "customer").lower()
            self.collected_intake_data = payload

            logger.info("Extracted %s intake data from Nova Sonic transcript: %s", role, payload)

            if role == "driver":
                await self.n8n_client.submit_driver_report(payload)
            else:
                await self.n8n_client.submit_customer_report(payload)
                # If customer provided a ride_id, look up the ride to identify and alert the assigned driver
                ride_id = payload.get("ride_id")
                if ride_id and self.dispatcher:
                    ride = await self.n8n_client.lookup_ride(ride_id)
                    if ride and ride.get("driver_phone"):
                        logger.info(
                            "Proactive dispatch: alerting driver %s for ride %s",
                            ride.get("driver_phone"),
                            ride_id,
                        )
                        await self.dispatcher.notify_driver_unmatched_ride(
                            driver_phone=ride["driver_phone"],
                            ride_id=ride_id,
                            item_type=payload.get("item_type", "item"),
                        )
        except Exception as e:
            logger.error("Failed to parse or submit INTAKE_DATA from Nova Sonic: %s", e)

    async def _check_for_case_action_payload(self, text: str) -> None:
        """Extracts case resolution tag and updates Google Sheets via n8n webhook."""
        if "<CASE_ACTION>" not in text or "</CASE_ACTION>" not in text:
            return

        match = re.search(r"<CASE_ACTION>(.*?)</CASE_ACTION>", text, re.DOTALL)
        if not match:
            return

        try:
            payload = json.loads(match.group(1).strip())
            case_id = payload.get("case_id")
            status = payload.get("status", "resolved")
            notes = payload.get("notes", "Updated via phone call")
            self.case_action_performed = payload

            logger.info("Updating case #%s to status=%s via n8n", case_id, status)
            await self.n8n_client.update_case_status(case_id, status, notes=notes)
        except Exception as e:
            logger.error("Failed to parse or submit CASE_ACTION from Nova Sonic: %s", e)

    def trigger_mock_intake_completion(self, report_data: Dict[str, Any]) -> None:
        """Helper for tests to simulate Nova Sonic emitting an intake tag."""
        tag = f"<INTAKE_DATA>{json.dumps(report_data)}</INTAKE_DATA>"
        self._mock_downlink_queue.put_nowait(
            {"event": {"textOutput": {"content": tag}}}
        )

    def trigger_mock_case_action(self, case_action: Dict[str, Any]) -> None:
        """Helper for tests to simulate Nova Sonic emitting a case resolution tag."""
        tag = f"<CASE_ACTION>{json.dumps(case_action)}</CASE_ACTION>"
        self._mock_downlink_queue.put_nowait(
            {"event": {"textOutput": {"content": tag}}}
        )

    def trigger_mock_interruption(self) -> None:
        """Helper for tests to simulate Nova Sonic signalling barge-in."""
        self._mock_downlink_queue.put_nowait(
            {"event": {"textOutput": {"content": '{ "interrupted" : true }'}}}
        )

    async def on_hangup(self, _evt: Any = None) -> None:
        """Callback invoked when caller terminates the call."""
        logger.info("Call %s hung up; closing Nova Sonic session", self.call.id)
        await self.close()

    async def close(self) -> None:
        """Gracefully terminates Nova Sonic bidirectional stream."""
        if self._closing:
            return
        self._closing = True
        self._is_active = False

        if self.mock_mode:
            await self._mock_downlink_queue.put(None)
            return

        if not self._stream:
            return

        try:
            await self._send_event(
                {
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                        }
                    }
                }
            )
            await self._send_event(
                {"event": {"promptEnd": {"promptName": self.prompt_name}}}
            )
            await self._send_event({"event": {"sessionEnd": {}}})
        except Exception:
            pass
        finally:
            if self._stream and self._stream.input_stream:
                try:
                    await self._stream.input_stream.close()
                except Exception:
                    pass
                self._stream = None
