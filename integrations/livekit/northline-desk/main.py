"""
AgentDuet x LiveKit bridge: phone PCM to LiveKit room (Gemini Live via agent worker).

AgentDuet owns the call. LiveKit Agents + Gemini Live own the conversation.
Run agent_worker.py first, then this process.
"""

from __future__ import annotations

import asyncio
import array
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from livekit import api, rtc

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
NUM_CHANNELS = 1
# ~20 ms frames when buffering AgentDuet chunks for LiveKit
FRAME_SAMPLES = 480
AGENT_NAME = "agentduet-phone"
# Simple energy gate to clear AgentDuet playback on barge-in
BARGE_RMS_THRESHOLD = int(os.getenv("BARGE_RMS_THRESHOLD", "400"))


def _require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")


def _pcm_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    acc = 0
    for s in samples:
        acc += s * s
    return (acc / len(samples)) ** 0.5


def _access_token(*, identity: str, room_name: str, name: str) -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name(name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


class PhoneLiveKitBridge:
    """Bidirectional PCM bridge: AgentDuet Call ↔ LiveKit room tracks."""

    def __init__(self, call: Call, room_name: str):
        self._call = call
        self._room_name = room_name
        self._room = rtc.Room()
        self._source: Optional[rtc.AudioSource] = None
        self._pcm_buf = bytearray()
        self._terminated = False
        self._agent_speaking = False
        self._tasks: list[asyncio.Task] = []
        self._lkapi: Optional[api.LiveKitAPI] = None

    async def _on_hangup(self, _evt) -> None:
        self._terminated = True
        for t in self._tasks:
            t.cancel()
        try:
            await self._room.disconnect()
        except Exception:
            pass

    async def run(self) -> None:
        self._call.on_hangup(self._on_hangup)
        self._lkapi = api.LiveKitAPI(
            os.environ["LIVEKIT_URL"],
            os.environ["LIVEKIT_API_KEY"],
            os.environ["LIVEKIT_API_SECRET"],
        )

        try:
            await self._lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=self._room_name,
                    empty_timeout=120,
                    max_participants=4,
                )
            )
            logger.info("Created LiveKit room %s", self._room_name)

            await self._lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=self._room_name,
                )
            )
            logger.info("Dispatched agent %s", AGENT_NAME)

            token = _access_token(
                identity=f"phone-{self._call.id}",
                room_name=self._room_name,
                name="AgentDuet bridge",
            )
            await self._room.connect(os.environ["LIVEKIT_URL"], token)
            logger.info("Bridge joined room %s", self._room_name)

            self._source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS, queue_size_ms=200)
            track = rtc.LocalAudioTrack.create_audio_track("phone-caller", self._source)
            await self._room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )

            @self._room.on("track_subscribed")
            def _on_track(
                track: rtc.Track,
                _pub: rtc.RemoteTrackPublication,
                participant: rtc.RemoteParticipant,
            ) -> None:
                if track.kind != rtc.TrackKind.KIND_AUDIO:
                    return
                if participant.identity.startswith("phone-"):
                    return
                logger.info("Subscribing to agent audio from %s", participant.identity)
                self._tasks.append(
                    asyncio.create_task(self._from_livekit(track))
                )

            # Agent may already be in the room with tracks published.
            for p in self._room.remote_participants.values():
                for pub in p.track_publications.values():
                    if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                        self._tasks.append(
                            asyncio.create_task(self._from_livekit(pub.track))
                        )

            self._tasks.append(asyncio.create_task(self._to_livekit()))
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        try:
            if getattr(self._room, "isconnected", lambda: True)():
                await self._room.disconnect()
        except Exception:
            pass
        if self._lkapi is not None:
            try:
                await self._lkapi.room.delete_room(
                    api.DeleteRoomRequest(room=self._room_name)
                )
            except Exception:
                logger.debug("delete_room failed (ok if already gone)", exc_info=True)
            await self._lkapi.aclose()

    async def _push_pcm_to_livekit(self, pcm: bytes) -> None:
        assert self._source is not None
        self._pcm_buf.extend(pcm)
        bytes_per_frame = FRAME_SAMPLES * 2  # int16 mono
        while len(self._pcm_buf) >= bytes_per_frame:
            chunk = bytes(self._pcm_buf[:bytes_per_frame])
            del self._pcm_buf[:bytes_per_frame]
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                samples_per_channel=FRAME_SAMPLES,
            )
            await self._source.capture_frame(frame)

    async def _to_livekit(self) -> None:
        try:
            async for chunk in self._call.caller.audio_stream():
                if self._terminated:
                    break
                # Barge-in: caller speaking while agent audio was playing.
                if self._agent_speaking and _pcm_rms(chunk) >= BARGE_RMS_THRESHOLD:
                    try:
                        await self._call.clear_send_audio_buffer()
                    except CallClosedError:
                        return
                    if self._source is not None:
                        clear = getattr(self._source, "clear_queue", None) or getattr(
                            self._source, "clear_audio_buffer", None
                        )
                        if clear is not None:
                            clear()
                    self._agent_speaking = False
                await self._push_pcm_to_livekit(chunk)
        except CallClosedError:
            pass
        except Exception:
            if not self._terminated:
                logger.exception("stream to LiveKit failed")

    async def _from_livekit(self, track: rtc.Track) -> None:
        try:
            stream = rtc.AudioStream.from_track(
                track=track,
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
            )
            async with stream:
                async for event in stream:
                    if self._terminated:
                        break
                    pcm = bytes(event.frame.data)
                    if not pcm:
                        continue
                    self._agent_speaking = True
                    try:
                        await self._call.send_audio(pcm)
                    except BufferFullError:
                        logger.warning("AgentDuet send buffer full; drop chunk")
                    except CallClosedError:
                        return
        except Exception:
            if not self._terminated:
                logger.exception("stream from LiveKit failed")


async def handle_call(call: Call) -> None:
    answered = await call.answer()
    if not answered:
        logger.error(
            "answer failed: %s (%s)",
            answered.error_message,
            answered.error_code,
        )
        return

    room_name = f"agentduet-{call.id}-{uuid.uuid4().hex[:6]}"
    logger.info("Bridging call %s ↔ LiveKit room %s", call.id, room_name)
    await PhoneLiveKitBridge(call, room_name).run()


async def main() -> None:
    _require_env(
        "AGENTDUET_API_KEY",
        "AGENTDUET_CONNECTOR_UUID",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    )

    config = SessionManagerConfig.create(
        api_key=os.environ["AGENTDUET_API_KEY"],
        connector_uuid=os.environ["AGENTDUET_CONNECTOR_UUID"],
        call_audio=CallAudioConfig(sample_rate=SAMPLE_RATE, buffer_size=1024 * 1024),
    )
    async with SessionManager(config) as sm:
        logger.info("AgentDuet × LiveKit bridge online (start agent_worker.py first)")

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
