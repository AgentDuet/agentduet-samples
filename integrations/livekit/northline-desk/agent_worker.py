"""
LiveKit Agents worker: Gemini Live speech-to-speech.

Run this process before main.py (the AgentDuet phone bridge).
It registers as agent_name=agentduet-phone and joins rooms when dispatched.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import google

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentduet-livekit-worker")

AGENT_NAME = "agentduet-phone"
VOICE_NAME = "Noah"
INSTRUCTIONS = (
    f"Your name is {VOICE_NAME}. You are a witty, helpful voice assistant on a phone call. "
    "Greet the caller briefly, keep answers short and conversational, and ask clarifying "
    "questions when needed."
)

server = AgentServer()


class VoiceAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    logger.info("Job for room=%s", ctx.room.name)
    await ctx.connect()

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            # Native audio S2S. Avoid generate_reply with gemini-3.1 mid-session limits.
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Puck",
            instructions=INSTRUCTIONS,
        ),
    )

    await session.start(
        agent=VoiceAssistant(),
        room=ctx.room,
    )
    logger.info("Gemini Live session started in %s", ctx.room.name)


if __name__ == "__main__":
    cli.run_app(server)
