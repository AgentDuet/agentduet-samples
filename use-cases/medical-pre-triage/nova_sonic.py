"""Bridge an AgentDuet Call to Amazon Nova 2 Sonic bidirectional streaming."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from agentduet import Call, CallClosedError
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import (
    Config,
    HTTPAuthSchemeResolver,
    SigV4AuthScheme,
)
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

from prompts import CALL_START_KICKOFF, SYSTEM_PROMPT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NovaSettings:
    """Bedrock / Nova Sonic settings for one process."""

    aws_region: str
    nova_model_id: str
    nova_voice_id: str
    sample_rate: int
    endpointing_sensitivity: str

    @classmethod
    def from_env(cls) -> NovaSettings:
        sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "24000"))
        if sample_rate not in (8000, 16000, 24000):
            raise RuntimeError("AUDIO_SAMPLE_RATE must be 8000, 16000, or 24000")

        endpointing = os.getenv("NOVA_ENDPOINTING_SENSITIVITY", "HIGH").strip().upper()
        if endpointing not in {"HIGH", "MEDIUM", "LOW"}:
            raise RuntimeError(
                "NOVA_ENDPOINTING_SENSITIVITY must be HIGH, MEDIUM, or LOW"
            )

        return cls(
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            nova_model_id=os.getenv("NOVA_MODEL_ID", "amazon.nova-2-sonic-v1:0"),
            nova_voice_id=os.getenv("NOVA_VOICE_ID", "tiffany"),
            sample_rate=sample_rate,
            endpointing_sensitivity=endpointing,
        )


class NovaSonicSession:
    """Owns one Nova 2 Sonic bidirectional stream for a single phone call."""

    def __init__(
        self,
        *,
        call: Call,
        settings: NovaSettings,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.call = call
        self.settings = settings
        self.system_prompt = system_prompt

        self.prompt_name = str(uuid.uuid4())
        self.system_content_name = str(uuid.uuid4())
        self.kickoff_content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self._client: BedrockRuntimeClient | None = None
        self._stream: Any = None
        self._active = False
        self._role: str | None = None
        self._generation_stage: str | None = None
        self._send_lock = asyncio.Lock()

    def _build_client(self) -> BedrockRuntimeClient:
        region = self.settings.aws_region
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{region}.amazonaws.com",
            region=region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")},
        )
        return BedrockRuntimeClient(config=config)

    async def _send_event(self, event: dict[str, Any]) -> None:
        if not self._stream or not self._active:
            return
        payload = json.dumps(event)
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload.encode("utf-8"))
        )
        async with self._send_lock:
            await self._stream.input_stream.send(chunk)

    async def start(self) -> None:
        self._client = self._build_client()
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(
                model_id=self.settings.nova_model_id
            )
        )
        self._active = True

        rate = self.settings.sample_rate
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
                            "endpointingSensitivity": (
                                self.settings.endpointing_sensitivity
                            )
                        },
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "promptStart": {
                        "promptName": self.prompt_name,
                        "textOutputConfiguration": {"mediaType": "text/plain"},
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": rate,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": self.settings.nova_voice_id,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                        "toolUseOutputConfiguration": {
                            "mediaType": "application/json"
                        },
                        "toolConfiguration": {"tools": []},
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
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
                        "contentName": self.system_content_name,
                        "content": self.system_prompt,
                    }
                }
            }
        )
        await self._send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
                    }
                }
            }
        )
        # Text kickoff so the agent greets immediately (does not wait for caller audio).
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
                        "content": CALL_START_KICKOFF,
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
                            "sampleRateHertz": rate,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )
        logger.info(
            "Nova Sonic session started call_id=%s model=%s rate=%s endpointing=%s",
            self.call.id,
            self.settings.nova_model_id,
            rate,
            self.settings.endpointing_sensitivity,
        )

    async def send_audio(self, pcm: bytes) -> None:
        if not self._active or not pcm:
            return
        encoded = base64.b64encode(pcm).decode("utf-8")
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

    async def _handle_output_event(self, event: dict[str, Any]) -> None:
        if "contentStart" in event:
            content_start = event["contentStart"]
            self._role = content_start.get("role")
            self._generation_stage = None
            additional = content_start.get("additionalModelFields")
            if additional:
                try:
                    fields = (
                        json.loads(additional)
                        if isinstance(additional, str)
                        else additional
                    )
                    self._generation_stage = fields.get("generationStage")
                except (TypeError, json.JSONDecodeError):
                    pass
            return

        if "textOutput" in event:
            text = event["textOutput"].get("content", "")
            if '{ "interrupted" : true }' in text or '{"interrupted": true}' in text:
                logger.info("Barge-in signal for call %s", self.call.id)
                try:
                    await self.call.clear_send_audio_buffer()
                except CallClosedError:
                    pass
                return

            role = self._role or "ASSISTANT"
            stage = self._generation_stage or "FINAL"
            if role == "ASSISTANT" and stage == "SPECULATIVE":
                logger.debug(
                    "speculative assistant text call_id=%s: %s", self.call.id, text
                )
                return

            logger.info(
                "transcript call_id=%s role=%s stage=%s: %s",
                self.call.id,
                role,
                stage,
                text,
            )
            return

        if "audioOutput" in event:
            audio_b64 = event["audioOutput"].get("content")
            if not audio_b64:
                return
            pcm = base64.b64decode(audio_b64)
            try:
                await self.call.send_audio(pcm)
            except CallClosedError:
                self._active = False
            return

        if "contentEnd" in event:
            stop_reason = event["contentEnd"].get("stopReason")
            if stop_reason == "INTERRUPTED":
                logger.info("contentEnd INTERRUPTED for call %s", self.call.id)
                try:
                    await self.call.clear_send_audio_buffer()
                except CallClosedError:
                    pass
            return

        if "usageEvent" in event:
            logger.debug("usageEvent call_id=%s %s", self.call.id, event["usageEvent"])

    async def process_responses(self) -> None:
        """Read Nova Sonic output until the stream ends."""
        assert self._stream is not None
        try:
            while self._active:
                try:
                    output = await self._stream.await_output()
                    result = await output[1].receive()
                except StopAsyncIteration:
                    break
                except Exception as exc:  # noqa: BLE001 — stream end / transport
                    if self._active:
                        logger.warning(
                            "Nova Sonic receive ended for call %s: %s",
                            self.call.id,
                            exc,
                        )
                    break

                if not result.value or not result.value.bytes_:
                    continue
                try:
                    payload = json.loads(result.value.bytes_.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                event = payload.get("event")
                if not event:
                    continue
                await self._handle_output_event(event)
        finally:
            self._active = False

    async def close(self) -> None:
        if not self._stream:
            return
        try:
            if self._active:
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
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error while closing Nova Sonic session: %s", exc)
        finally:
            self._active = False
            try:
                await self._stream.input_stream.close()
            except Exception:  # noqa: BLE001
                pass


async def start_nova_sonic_session(call: Call, *, settings: NovaSettings) -> None:
    """Answer the call and bridge audio ↔ Nova 2 Sonic until hangup."""
    answered = await call.answer()
    if not answered:
        logger.error(
            "Failed to answer call %s: %s (%s)",
            call.id,
            getattr(answered, "error_message", None),
            getattr(answered, "error_code", None),
        )
        return

    session = NovaSonicSession(call=call, settings=settings)

    try:
        await session.start()
        logger.info("Call %s answered; Nova Sonic bridge running", call.id)

        async def to_nova() -> None:
            async for chunk in call.caller.audio_stream():
                await session.send_audio(chunk)

        async def from_nova() -> None:
            await session.process_responses()

        await asyncio.gather(to_nova(), from_nova())
    except CallClosedError:
        logger.info("Call %s closed during Nova Sonic session", call.id)
    except Exception:
        logger.exception("Nova Sonic session failed for call %s", call.id)
    finally:
        await session.close()
        try:
            await call.close()
        except Exception:  # noqa: BLE001
            pass
