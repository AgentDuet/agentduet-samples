"""
OmniBank phone concierge: AgentDuet + Gemini Live + MCP tools.

Tools (authenticate, account summary, fee waiver, escalation) live in
mcp_tools_server.py over stdio. Swap that server to change capabilities
without touching the model wiring.

Demo caller: Ava Chen, card ending 4821.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets import ConnectionClosed

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

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
MODEL = "models/gemini-3.1-flash-live-preview"
VOICE = "Zephyr"
MCP_SERVER = HERE / "mcp_tools_server.py"
AGENT_NAME = "May"

genai_client = genai.Client(
    vertexai=False,
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
)

PERSONA = (
    f"You are {AGENT_NAME}, a warm, professional phone concierge for OmniBank credit cards. "
    "Keep replies short and natural. Verify the caller BEFORE any account help:\n"
    "1. Ask for their full name as registered with the bank and the last 4 digits of their "
    "card (any number of digits is fine; never comment on how many they give).\n"
    "2. Read the details back and WAIT for confirmation: spell out BOTH first and last name "
    "letter by letter (e.g. 'Ava, A-V-A, Chen, C-H-E-N, card ending "
    "4-8-2-1, is that right?'), so any mishearing is caught. Only then call "
    "authenticate_customer with the name and digits exactly as the caller gave them.\n"
    "3. authenticate_customer returns the outstanding fees, balance, and your waiver limit. "
    "Use those figures; never invent numbers.\n"
    "4. You may waive at most 50 dollars of fees in total. If the caller wants more (e.g. "
    "both fees), do the arithmetic aloud, explain the limit, and offer to waive one fee or "
    "escalate.\n"
    "5. NEVER claim an action you have not performed with a tool: to waive, call "
    "process_fee_waiver and report only what it returns; to escalate, call "
    "record_escalation and read back the exact reference number it returns."
)


def _clean_schema(node: Any) -> Any:
    """Drop JSON-Schema keys Gemini rejects so MCP inputSchema maps to FunctionDeclaration."""
    if isinstance(node, dict):
        return {
            k: _clean_schema(v)
            for k, v in node.items()
            if k not in ("title", "$schema", "additionalProperties")
        }
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


class McpTools:
    """Stdio MCP client and Gemini tool declarations."""

    def __init__(self) -> None:
        self.session: Optional[ClientSession] = None
        self.declarations: list[types.FunctionDeclaration] = []

    async def connect(self, stack: AsyncExitStack) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER)],
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        self.session = await stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        tools = (await self.session.list_tools()).tools
        self.declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=(t.description or "").strip(),
                parameters_json_schema=_clean_schema(t.inputSchema),
            )
            for t in tools
        ]
        logger.info("MCP tools available: %s", [t.name for t in tools])

    async def dispatch(self, name: str, args: dict) -> dict:
        assert self.session is not None
        result = await self.session.call_tool(name, args or {})
        if result.structuredContent:
            return dict(result.structuredContent)
        if result.content and getattr(result.content[0], "text", None):
            try:
                return json.loads(result.content[0].text)
            except (ValueError, TypeError):
                return {"result": result.content[0].text}
        return {"status": "ok"}


class McpAgentIntegration:
    """Bridges call audio to Gemini Live and routes tool calls to MCP."""

    def __init__(self, call: Call, gemini_session: AsyncSession, mcp: McpTools):
        self._call = call
        self._gemini = gemini_session
        self._mcp = mcp
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

    async def _on_hangup(self, _evt) -> None:
        logger.info("Call hung up")
        try:
            await self._gemini.close()
        except Exception:
            logger.exception("Error closing Gemini session")
        for task in (self._send_task, self._recv_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, genai_errors.APIError):
                    pass

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self._gemini.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text="The caller has just connected. Greet them.")],
            ),
            turn_complete=True,
        )
        self._send_task = asyncio.create_task(self._stream_to_gemini())
        self._recv_task = asyncio.create_task(self._receive_from_gemini())
        await asyncio.gather(self._send_task, self._recv_task, return_exceptions=True)

    async def _stream_to_gemini(self) -> None:
        try:
            async for audio_chunk in self._call.caller.audio_stream():
                await self._gemini.send_realtime_input(
                    audio=types.Blob(
                        data=audio_chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("Error streaming to Gemini")
            raise

    async def _receive_from_gemini(self) -> None:
        try:
            while True:
                async for response in self._gemini.receive():
                    if response.tool_call:
                        for fc in response.tool_call.function_calls:
                            args = dict(fc.args or {})
                            logger.info("tool call: %s(%s)", fc.name, args)
                            try:
                                result = await self._mcp.dispatch(fc.name, args)
                            except Exception as e:
                                logger.exception("MCP dispatch failed")
                                result = {"error": str(e)}
                            await self._gemini.send_tool_response(
                                function_responses=[
                                    types.FunctionResponse(
                                        id=fc.id, name=fc.name, response=result
                                    )
                                ]
                            )
                        continue
                    server_content = response.server_content
                    if not server_content:
                        continue
                    if server_content.interrupted:
                        await self._call.clear_send_audio_buffer()
                        break
                    if server_content.model_turn:
                        for part in server_content.model_turn.parts or []:
                            if part.inline_data and isinstance(
                                part.inline_data.data, bytes
                            ):
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning(
                                        "Send buffer full; dropping audio chunk"
                                    )
        except (ConnectionClosed, genai_errors.APIError):
            logger.debug("Gemini session closed")
        except asyncio.CancelledError:
            raise
        except CallClosedError:
            logger.debug("Call closed; stopping stream from Gemini")
        except Exception:
            logger.exception("Error streaming from Gemini")
            raise


def build_live_config(mcp: McpTools) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=PERSONA)]),
        tools=[types.Tool(function_declarations=mcp.declarations)],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def main() -> None:
    missing = [
        k
        for k in ("AGENTDUET_API_KEY", "AGENTDUET_CONNECTOR_UUID")
        if not os.getenv(k)
    ]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise SystemExit("Set GEMINI_API_KEY (or GOOGLE_API_KEY)")

    async with AsyncExitStack() as stack:
        mcp = McpTools()
        await mcp.connect(stack)
        live_config = build_live_config(mcp)

        config = SessionManagerConfig.create(
            api_key=os.environ["AGENTDUET_API_KEY"],
            connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
            call_audio=CallAudioConfig(
                sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024
            ),
        )
        sm = await stack.enter_async_context(SessionManager(config))
        logger.info("OmniBank MCP desk online (%s)", sm.id)

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            logger.info("Incoming call=%s caller=%s", call.id, call.caller)
            try:
                async with genai_client.aio.live.connect(
                    model=MODEL, config=live_config
                ) as gemini_session:
                    result = await call.answer()
                    if not result:
                        logger.error(
                            "Failed to answer %s: %s (%s)",
                            call.id,
                            result.error_message,
                            result.error_code,
                        )
                        return
                    await McpAgentIntegration(call, gemini_session, mcp).run()
            except Exception:
                logger.exception("Error in MCP agent integration")
                try:
                    await call.close()
                except Exception:
                    pass
                raise

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
