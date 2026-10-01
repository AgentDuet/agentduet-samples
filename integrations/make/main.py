import asyncio
import logging
import os

from dotenv import load_dotenv
from google import genai

from agentduet import (
    CallAudioConfig,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

from hotel_reservation_agent import (
    DEFAULT_VOICE,
    SAMPLE_RATE,
    HotelReservationVoiceAgent,
    build_gemini_config,
)
from make_client import MakeHotelClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Make.com client
make_client = MakeHotelClient()

# Gemini Live Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
MODEL = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview")
genai_client = genai.Client(vertexai=False, api_key=GEMINI_API_KEY)


async def main():
    agentduet_api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")

    if not agentduet_api_key or not connector_uuid:
        logger.warning(
            "AGENTDUET_API_KEY or AGENTDUET_CONNECTOR_UUID not set. "
            "Please configure your .env file with credentials from https://agentduet.com"
        )
        return

    if not GEMINI_API_KEY:
        logger.warning(
            "GEMINI_API_KEY not set. Gemini Live requires a Google AI Studio API key. "
            "Get one at https://aistudio.google.com"
        )
        return

    # 24kHz audio configuration matching Gemini Live speech output
    config = SessionManagerConfig.create(
        api_key=agentduet_api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=SAMPLE_RATE,
            buffer_size=1024 * 1024,  # 1MB buffer
        ),
    )

    gemini_live_config = build_gemini_config(voice_name=DEFAULT_VOICE)

    async with SessionManager(config) as sm:
        logger.info("AgentDuet connected: %s. Ready for conversational reservation calls.", sm.id)

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification):
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)
            caller_phone = str(getattr(call.caller, "address", call.caller) or "+15551234567")
            logger.info("Incoming reservation call %s from %s", call.id, caller_phone)

            try:
                async with genai_client.aio.live.connect(
                    model=MODEL,
                    config=gemini_live_config,
                ) as gemini_session:
                    result = await call.answer()
                    if not result:
                        logger.error(
                            "Failed to answer call %s: %s (%s)",
                            call.id,
                            result.error_message,
                            result.error_code,
                        )
                        return

                    agent = HotelReservationVoiceAgent(
                        call=call,
                        gemini_session=gemini_session,
                        make_client=make_client,
                        caller_phone=caller_phone,
                    )
                    await agent.run()
            except Exception:
                logger.exception("Error during conversational reservation call %s", call.id)
                await call.close()

        await sm.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
