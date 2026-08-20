"""
Personal phone assistant — AgentDuet × OpenClaw Gateway × Gemini Live.

AgentDuet owns the call. OpenClaw owns the agent (calendar, weather, gold
price, web, messaging). Gemini Live is speech in/out only: it transcribes the
caller, speaks OpenClaw's reply, and must not answer from its own knowledge.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
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

from openclaw_gateway import OpenClawGateway

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
VOICE = os.getenv("GEMINI_VOICE", "Zephyr").strip() or "Zephyr"
AGENT_NAME = os.getenv("AGENT_NAME", "Ada").strip() or "Ada"
HANDOFF_GRACE_SECONDS = float(os.getenv("HANDOFF_AUDIO_GRACE_SECONDS", "2.5"))
UTTERANCE_SILENCE_SECONDS = float(os.getenv("UTTERANCE_SILENCE_SECONDS", "1.4"))
FILLER = os.getenv("OPENCLAW_FILLER", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

GREET_PROMPT = (
    f"The user just called on the phone. Greet them in one short sentence as "
    f"{AGENT_NAME}, their personal assistant. Mention you can help with their "
    "calendar, weather, prices, and everyday questions."
)

SYSTEM_INSTRUCTION = f"""You are {AGENT_NAME}, a phone speech layer.

You do NOT know facts, calendars, weather, prices, or news. Another assistant
(OpenClaw) answers those. Your only jobs:

1. On connect, stay silent until you receive an [ASSISTANT_SCRIPT] message,
   then speak that script word for word.
2. After that, stay silent while the caller talks. Do not answer questions,
   do not guess gold prices or weather, do not invent meetings.
3. When you receive [ASSISTANT_SCRIPT], speak it naturally in your voice.
   Do not add facts, caveats, or extra offers.
4. If the script is a goodbye, or the caller is clearly done, call end_call
   after you finish speaking.
5. If you receive [FILLER], say only that short phrase, then wait.

Never place an outbound call. Never claim you looked something up yourself.
"""

END_TOOL = types.FunctionDeclaration(
    name="end_call",
    description=(
        "Hang up after a goodbye. Call only after you finished speaking the "
        "closing [ASSISTANT_SCRIPT], or if the caller asked to end."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)

genai_client = genai.Client(
    vertexai=False,
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
)


def _transcription_text(sc: Any) -> str:
    t = getattr(sc, "input_transcription", None)
    if t is None:
        return ""
    return (getattr(t, "text", None) or "").strip()


class PersonalAgentBridge:
    def __init__(
        self,
        call: Call,
        live: AsyncSession,
        openclaw: OpenClawGateway,
        session_user: str,
    ) -> None:
        self._call = call
        self._live = live
        self._openclaw = openclaw
        self._session_user = session_user
        self._terminated = False
        self._end = False
        self._tasks: list[asyncio.Task] = []
        self._ask_task: Optional[asyncio.Task] = None
        self._silence_task: Optional[asyncio.Task] = None
        self._user_bits: list[str] = []
        self._allow_audio = False
        self._lock = asyncio.Lock()

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        if self._ask_task:
            self._ask_task.cancel()
        if self._silence_task:
            self._silence_task.cancel()
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        self._tasks = [
            asyncio.create_task(self._to_model()),
            asyncio.create_task(self._from_model()),
        ]
        self._ask_task = asyncio.create_task(
            self._openclaw_turn(GREET_PROMPT, filler=False)
        )
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _speak(self, tag: str, text: str) -> None:
        if self._terminated or not text.strip():
            return
        self._allow_audio = True
        await self._live.send_realtime_input(text=f"{tag}\n{text.strip()}")

    async def _openclaw_turn(self, user_text: str, *, filler: bool) -> None:
        async with self._lock:
            if self._terminated:
                return
            if filler and FILLER:
                try:
                    await self._speak("[FILLER]", "One moment.")
                except Exception:
                    logger.exception("filler failed")
            try:
                reply = await self._openclaw.ask(
                    session_user=self._session_user,
                    user_text=user_text,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OpenClaw turn failed")
                reply = (
                    "I couldn't reach your assistant just now. "
                    "Try that again in a moment."
                )
            if not reply:
                reply = "I didn't get an answer back. Could you say that again?"
            try:
                await self._speak("[ASSISTANT_SCRIPT]", reply)
            except Exception:
                if not self._terminated:
                    logger.exception("speak script failed")

    def _kick_ask(self, user_text: str, *, filler: bool) -> None:
        if self._ask_task and not self._ask_task.done():
            self._ask_task.cancel()
        self._ask_task = asyncio.create_task(
            self._openclaw_turn(user_text, filler=filler)
        )

    def _flush_utterance(self) -> None:
        uttered = "".join(self._user_bits).strip()
        self._user_bits = []
        if not uttered or self._terminated:
            return
        logger.info("caller: %s", uttered)
        self._allow_audio = False
        self._kick_ask(uttered, filler=True)

    def _arm_silence_flush(self) -> None:
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

        async def _wait() -> None:
            try:
                await asyncio.sleep(UTTERANCE_SILENCE_SECONDS)
            except asyncio.CancelledError:
                return
            self._flush_utterance()

        self._silence_task = asyncio.create_task(_wait())

    async def _to_model(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={SAMPLE_RATE}",
                    )
                )
        except (ConnectionClosed, CallClosedError):
            pass
        except Exception:
            if not self._terminated:
                logger.exception("stream to Gemini failed")

    async def _from_model(self) -> None:
        try:
            while not self._terminated:
                async for response in self._live.receive():
                    if self._terminated:
                        return
                    if response.tool_call:
                        await self._handle_tools(response)
                        if self._end:
                            await asyncio.sleep(HANDOFF_GRACE_SECONDS)
                            self._terminated = True
                            try:
                                await self._call.disconnect()
                            except Exception:
                                logger.exception("disconnect after end_call failed")
                            return
                        continue

                    sc = response.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        self._allow_audio = False
                        self._user_bits = []
                        if self._ask_task and not self._ask_task.done():
                            self._ask_task.cancel()
                        if self._silence_task and not self._silence_task.done():
                            self._silence_task.cancel()
                        try:
                            await self._call.clear_send_audio_buffer()
                        except Exception:
                            pass
                        break

                    piece = _transcription_text(sc)
                    if piece:
                        self._allow_audio = False
                        self._user_bits.append(piece)
                        self._arm_silence_flush()

                    model_done = bool(
                        getattr(sc, "turn_complete", False)
                        or getattr(sc, "generation_complete", False)
                    )
                    if model_done and self._user_bits:
                        if self._silence_task and not self._silence_task.done():
                            self._silence_task.cancel()
                        self._flush_utterance()

                    if not self._allow_audio:
                        continue
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Buffer full — drop audio")
                                except CallClosedError:
                                    return
        except (ConnectionClosed, genai_errors.APIError, CallClosedError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._terminated:
                logger.exception("receive from Gemini failed")

    async def _handle_tools(self, response: Any) -> None:
        responses = []
        for fc in response.tool_call.function_calls:
            logger.info("tool %s", fc.name)
            if fc.name == "end_call":
                self._end = True
                result = {"ok": True, "end": True}
            else:
                result = {"ok": False, "error": f"unknown tool {fc.name}"}
            responses.append(
                types.FunctionResponse(id=fc.id, name=fc.name, response=result)
            )
        await self._live.send_tool_response(function_responses=responses)


def _live_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[types.Tool(function_declarations=[END_TOOL])],
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def handle_call(call: Call, openclaw: OpenClawGateway) -> None:
    from_number = call.participant.value if call.participant else "unknown"
    session_user = f"call:{call.id}"
    logger.info("Personal OpenClaw agent for %s session=%s", from_number, session_user)

    async with genai_client.aio.live.connect(
        model=MODEL, config=_live_config()
    ) as live:
        answered = await call.answer()
        if not answered:
            logger.error(
                "answer failed: %s (%s)",
                answered.error_message,
                answered.error_code,
            )
            return
        await PersonalAgentBridge(call, live, openclaw, session_user).run()


async def main() -> None:
    missing = [
        k
        for k in (
            "AGENTDUET_API_KEY",
            "AGENTDUET_CONNECTOR_UUID",
            "OPENCLAW_GATEWAY_TOKEN",
        )
        if not os.getenv(k)
    ]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise SystemExit("Set GEMINI_API_KEY (or GOOGLE_API_KEY)")

    base = (
        os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789").strip()
        or "http://127.0.0.1:18789"
    )
    model = os.getenv("OPENCLAW_MODEL", "openclaw/default").strip() or "openclaw/default"
    openclaw = OpenClawGateway(
        base_url=base,
        token=os.environ["OPENCLAW_GATEWAY_TOKEN"],
        model=model,
        timeout_s=float(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "90")),
    )
    try:
        models = await openclaw.health()
        ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
        logger.info("OpenClaw gateway ok at %s models=%s", base, ids or models)
    except Exception:
        await openclaw.aclose()
        raise SystemExit(
            f"Cannot reach OpenClaw gateway at {base} /v1/models. "
            "Enable chat completions, set OPENCLAW_GATEWAY_TOKEN, "
            "and keep the gateway on a private URL."
        )

    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    try:
        async with SessionManager(config) as sm:
            logger.info(
                "%s personal OpenClaw desk online (gateway %s)", AGENT_NAME, base
            )

            @sm.on_incoming_call
            async def on_call(noti: IncomingCallNotification) -> None:
                session = await sm.open_session(new_session_id(), noti.subscriber)
                call = await session.process_call(noti)
                try:
                    await handle_call(call, openclaw)
                except Exception:
                    logger.exception("Call %s failed", call.id)
                    try:
                        await call.close()
                    except Exception:
                        pass

            await sm.run_forever()
    finally:
        await openclaw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
