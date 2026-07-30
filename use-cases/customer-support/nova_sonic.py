"""Bridge an AgentDuet Call to Amazon Nova 2 Sonic with tool dispatch."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from agentduet import BufferFullError, Call, CallClosedError
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
from tools import TOOL_SCHEMAS, SupportTools

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


def _is_interruption_marker(text: str) -> bool:
    return '{ "interrupted" : true }' in text or '{"interrupted": true}' in text


class NovaSonicSession:
    """Owns one Nova 2 Sonic bidirectional stream for a single phone call."""

    def __init__(
        self,
        *,
        call: Call,
        settings: NovaSettings,
        tools: SupportTools,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.call = call
        self.settings = settings
        self.tools = tools
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
        self._tool_lock = asyncio.Lock()
        self._pending_tool: dict[str, Any] | None = None
        self._tool_tasks: set[asyncio.Task] = set()

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
                        "toolConfiguration": {"tools": TOOL_SCHEMAS},
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

    def _tool_result_payload(self, result: dict) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": bool(result.get("ok", True)),
            "message": (
                result.get("agent_speak_summary")
                or result.get("message")
                or result.get("error")
                or "Done."
            ),
        }
        if "count" in result:
            payload["count"] = result["count"]
        if "rides" in result:
            compact = []
            for r in (result.get("rides") or [])[:5]:
                compact.append(
                    {
                        "ride_id": r.get("ride_id"),
                        "pickup": r.get("pickup"),
                        "dropoff": r.get("dropoff"),
                        "driver_name": r.get("driver_name"),
                        "summary": r.get("summary"),
                    }
                )
            payload["rides"] = compact
        for key in (
            "pipedrive_deferred",
            "case_type",
            "caller_role",
            "ticket_id",
            "ride_id",
            "error",
        ):
            if key in result and result[key] is not None:
                payload[key] = result[key]
        return payload

    async def _send_tool_result(self, tool_use_id: str, result: dict) -> None:
        if not self._stream or not self._active:
            logger.warning("Skipping tool result — Nova stream inactive")
            return

        payload = self._tool_result_payload(result)
        content = json.dumps(payload, ensure_ascii=True)
        content_name = str(uuid.uuid4())

        async with self._tool_lock:
            if not self._stream or not self._active:
                return
            try:
                await self._send_event(
                    {
                        "event": {
                            "contentStart": {
                                "promptName": self.prompt_name,
                                "contentName": content_name,
                                "interactive": False,
                                "type": "TOOL",
                                "role": "TOOL",
                                "toolResultInputConfiguration": {
                                    "toolUseId": tool_use_id,
                                    "type": "TEXT",
                                    "textInputConfiguration": {
                                        "mediaType": "text/plain"
                                    },
                                },
                            }
                        }
                    }
                )
                await self._send_event(
                    {
                        "event": {
                            "toolResult": {
                                "promptName": self.prompt_name,
                                "contentName": content_name,
                                "content": content,
                            }
                        }
                    }
                )
                await self._send_event(
                    {
                        "event": {
                            "contentEnd": {
                                "promptName": self.prompt_name,
                                "contentName": content_name,
                            }
                        }
                    }
                )
            except Exception:
                logger.exception("Failed to send tool result to Nova")
                self._active = False

    def _schedule_tool(
        self, tool_name: str, tool_content: dict, tool_use_id: str
    ) -> None:
        async def _run() -> None:
            try:
                if not tool_name or not tool_use_id:
                    logger.error("Invalid tool use (missing name/id)")
                    return
                result = await self.tools.execute(tool_name, tool_content or {})
                await self._send_tool_result(tool_use_id, result)
                logger.info("Tool %s completed: %s", tool_name, result)
            except Exception:
                logger.exception("Tool %s failed", tool_name)
                try:
                    await self._send_tool_result(
                        tool_use_id,
                        {
                            "ok": False,
                            "error": f"Tool {tool_name} failed",
                            "agent_speak_summary": (
                                "I hit a technical issue. "
                                "Please say the details again and I will retry."
                            ),
                        },
                    )
                except Exception:
                    logger.exception("Could not send tool error result")

        task = asyncio.create_task(_run())
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

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
            if _is_interruption_marker(text):
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
            except BufferFullError:
                logger.warning("Outgoing buffer full — dropping chunk")
            except CallClosedError:
                self._active = False
            return

        if "toolUse" in event:
            tool_use = event["toolUse"] or {}
            self._pending_tool = {
                "content": tool_use,
                "name": tool_use.get("toolName"),
                "id": tool_use.get("toolUseId"),
            }
            logger.info(
                "Tool use: %s id=%s",
                self._pending_tool["name"],
                self._pending_tool["id"],
            )
            return

        if "contentEnd" in event:
            content_end = event["contentEnd"]
            if content_end.get("type") == "TOOL" and self._pending_tool:
                pending = self._pending_tool
                self._pending_tool = None
                self._schedule_tool(
                    pending.get("name") or "",
                    pending.get("content") or {},
                    pending.get("id") or "",
                )
                return

            stop_reason = content_end.get("stopReason")
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
        if self._tool_tasks:
            await asyncio.gather(*list(self._tool_tasks), return_exceptions=True)

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


async def start_nova_sonic_session(
    call: Call,
    *,
    settings: NovaSettings,
    tools: SupportTools,
) -> dict[str, Any] | None:
    """Answer the call and bridge audio ↔ Nova 2 Sonic until hangup.

    Returns the Pipedrive finalize result (if any) after the call ends.
    """
    answered = await call.answer()
    if not answered:
        logger.error(
            "Failed to answer call %s: %s (%s)",
            call.id,
            getattr(answered, "error_message", None),
            getattr(answered, "error_code", None),
        )
        return None

    session = NovaSonicSession(call=call, settings=settings, tools=tools)
    finalize_result: dict[str, Any] | None = None

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
            finalize_result = await tools.finalize_after_call()
            if finalize_result:
                if finalize_result.get("tickets"):
                    for t in finalize_result["tickets"]:
                        if t.get("ok"):
                            logger.info(
                                "Post-call ticket ready ticket_id=%s case_type=%s "
                                "role=%s ride=%s",
                                t.get("ticket_id"),
                                t.get("case_type"),
                                t.get("caller_role"),
                                t.get("ride_id"),
                            )
                elif finalize_result.get("ok"):
                    logger.info(
                        "Post-call ticket ready ticket_id=%s case_type=%s "
                        "role=%s ride=%s",
                        finalize_result.get("ticket_id"),
                        finalize_result.get("case_type"),
                        finalize_result.get("caller_role"),
                        finalize_result.get("ride_id"),
                    )
        except Exception:
            logger.exception("Post-call ticket finalize failed")
        try:
            await call.close()
        except Exception:  # noqa: BLE001
            pass

    return finalize_result
