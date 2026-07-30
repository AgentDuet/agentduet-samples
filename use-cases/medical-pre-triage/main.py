"""
Medical Pre-Triage — AgentDuet + Amazon Nova Sonic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Any, Optional

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from dotenv import load_dotenv
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

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

from prompts import CALL_START_KICKOFF, SYSTEM_PROMPT

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
MODEL_ID = "amazon.nova-2-sonic-v1:0"
REGION = os.getenv("AWS_REGION", "us-east-1")


class NovaTriage:
    def __init__(self, call: Call, client: BedrockRuntimeClient):
        self._call = call
        self._client = client
        self._stream = None
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self._active = False
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

    async def send_event(self, event_json: str) -> None:
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )
        await self._stream.input_stream.send(chunk)

    async def start_session(self) -> None:
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )
        self._active = True
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "sessionStart": {
                            "inferenceConfiguration": {
                                "maxTokens": 1024,
                                "topP": 0.9,
                                "temperature": 0.7,
                            }
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
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
                                "voiceId": "matthew",
                                "encoding": "base64",
                                "audioType": "SPEECH",
                            },
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
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
        )
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "textInput": {
                            "promptName": self.prompt_name,
                            "contentName": self.content_name,
                            "content": SYSTEM_PROMPT,
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": self.content_name,
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
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
        )
        # Kick off greeting without waiting for caller audio.
        kick = str(uuid.uuid4())
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "contentStart": {
                            "promptName": self.prompt_name,
                            "contentName": kick,
                            "type": "TEXT",
                            "interactive": True,
                            "role": "USER",
                            "textInputConfiguration": {"mediaType": "text/plain"},
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "textInput": {
                            "promptName": self.prompt_name,
                            "contentName": kick,
                            "content": CALL_START_KICKOFF,
                        }
                    }
                }
            )
        )
        await self.send_event(
            json.dumps(
                {
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": kick,
                        }
                    }
                }
            )
        )

    async def end_session(self) -> None:
        if not self._stream:
            return
        try:
            await self.send_event(
                json.dumps(
                    {
                        "event": {
                            "contentEnd": {
                                "promptName": self.prompt_name,
                                "contentName": self.audio_content_name,
                            }
                        }
                    }
                )
            )
            await self.send_event(
                json.dumps({"event": {"promptEnd": {"promptName": self.prompt_name}}})
            )
            await self.send_event(json.dumps({"event": {"sessionEnd": {}}}))
        except Exception:
            pass
        finally:
            try:
                await self._stream.input_stream.close()
            except Exception:
                pass
            self._stream = None

    async def _on_hangup(self, _evt: Any) -> None:
        if not self._active:
            return
        self._active = False
        for t in (self._send_task, self._recv_task):
            if t:
                t.cancel()
        await asyncio.gather(
            *[t for t in (self._send_task, self._recv_task) if t],
            return_exceptions=True,
        )
        await self.end_session()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self.start_session()
        self._send_task = asyncio.create_task(self._stream_up())
        self._recv_task = asyncio.create_task(self._stream_down())
        await asyncio.gather(self._send_task, self._recv_task, return_exceptions=True)

    async def _stream_up(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if not self._active:
                    break
                blob = base64.b64encode(chunk).decode("ascii")
                await self.send_event(
                    json.dumps(
                        {
                            "event": {
                                "audioInput": {
                                    "promptName": self.prompt_name,
                                    "contentName": self.audio_content_name,
                                    "content": blob,
                                }
                            }
                        }
                    )
                )
        except CallClosedError:
            pass
        except Exception:
            if self._active:
                logger.exception("Error streaming to Nova")

    async def _stream_down(self) -> None:
        try:
            while self._active and self._stream:
                output = await self._stream.await_output()
                result = await output[1].receive()
                if not (result.value and result.value.bytes_):
                    continue
                payload = json.loads(result.value.bytes_.decode("utf-8"))
                event = payload.get("event") or {}
                if "textOutput" in event:
                    text = event["textOutput"].get("content", "")
                    if '{ "interrupted" : true }' in text:
                        await self._call.clear_send_audio_buffer()
                    elif text.strip():
                        logger.info("CareLine: %s", text)
                elif "audioOutput" in event:
                    audio = base64.b64decode(event["audioOutput"]["content"])
                    try:
                        await self._call.send_audio(audio)
                    except BufferFullError:
                        logger.warning("Buffer full — drop audio")
                    except CallClosedError:
                        break
        except (CallClosedError, asyncio.CancelledError):
            pass
        except Exception:
            if self._active:
                logger.exception("Error receiving from Nova")


async def main() -> None:
    aws_config = Config(
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        region=REGION,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    bedrock = BedrockRuntimeClient(config=aws_config)

    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("Medical pre-triage agent online")

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            try:
                answered = await call.answer()
                if not answered:
                    logger.error(
                        "answer failed: %s (%s)",
                        answered.error_message,
                        answered.error_code,
                    )
                    return
                await NovaTriage(call, bedrock).run()
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await call.close()
                except Exception:
                    pass

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
