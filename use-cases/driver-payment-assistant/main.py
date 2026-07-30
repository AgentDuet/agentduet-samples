"""
Driver Payment Assistant - AgentDuet + Gemini Live.

Demo brand: VibeRider. Answers driver payout questions only from policy.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.live import AsyncSession

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
POLICY_PATH = Path(__file__).resolve().parent / "policy.md"
POLICY_TEXT = POLICY_PATH.read_text(encoding="utf-8")

genai_client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
)

SYSTEM_INSTRUCTION = f"""
You are the VibeRider driver payment phone assistant.
Speak briefly. Introduce yourself as VibeRider payments help.

CRITICAL RULES:
- Answer ONLY using the policy document below.
- If the answer is not in the policy, say you do not have that information
  and suggest Wallet → Help → Pay & wallet in the driver app (or escalate
  per the policy).
- Never invent fees, tip cuts, payout days, or bank timelines.
- Never change account details or process payouts on this call.
- Tips: riders' tips go 100% to the driver; VibeRider does not cut tips.
- Instant Cash-Out fee is $1.99 flat when stated in the policy; weekly
  payout initiates on Monday with typical bank deposit by Wednesday.

=== POLICY START ===
{POLICY_TEXT}
=== POLICY END ===
""".strip()


class PolicyBridge:
    def __init__(self, call: Call, live: AsyncSession):
        self._call = call
        self._live = live
        self._terminated = False
        self._tasks: list[asyncio.Task] = []

    async def _on_hangup(self, _evt: Any) -> None:
        self._terminated = True
        try:
            await self._live.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        await self._live.send_realtime_input(
            text=(
                "Greet the driver as VibeRider payments help and ask how you "
                "can help with payouts or tips."
            )
        )
        self._tasks = [
            asyncio.create_task(self._to_model()),
            asyncio.create_task(self._from_model()),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

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
        except Exception:
            if not self._terminated:
                logger.exception("stream to Gemini failed")

    async def _from_model(self) -> None:
        try:
            while not self._terminated:
                async for response in self._live.receive():
                    sc = response.server_content
                    if sc and sc.interrupted:
                        await self._call.clear_send_audio_buffer()
                        break
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                try:
                                    await self._call.send_audio(part.inline_data.data)
                                except BufferFullError:
                                    logger.warning("Buffer full - drop audio")
                                except CallClosedError:
                                    return
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("receive from Gemini failed")


async def handle_call(call: Call) -> None:
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
            )
        ),
        system_instruction=SYSTEM_INSTRUCTION,
    )
    async with genai_client.aio.live.connect(model=MODEL, config=config) as live:
        answered = await call.answer()
        if not answered:
            logger.error(
                "answer failed: %s (%s)",
                answered.error_message,
                answered.error_code,
            )
            return
        await PolicyBridge(call, live).run()


async def main() -> None:
    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("Driver payment assistant online")

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            try:
                await handle_call(call)
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await call.close()
                except Exception:
                    pass

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
