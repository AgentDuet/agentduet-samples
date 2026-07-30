"""Medical Pre-Triage sample entrypoint.

Connects to AgentDuet, answers inbound calls, and bridges caller audio to
Amazon Nova 2 Sonic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agentduet import (
    CallAudioConfig,
    CallEvent,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

from nova_sonic import NovaSettings, start_nova_sonic_session

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
load_dotenv()

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("agentduet").setLevel(getattr(logging, level, logging.INFO))


async def run() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _configure_logging(log_level)

    api_key = os.environ["AGENTDUET_API_KEY"]
    connector_uuid = os.environ["AGENTDUET_CONNECTOR_UUID"]
    settings = NovaSettings.from_env()

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(sample_rate=settings.sample_rate),  # type: ignore[arg-type]
    )

    logger.info(
        "Starting Medical Pre-Triage (Nova Sonic=%s region=%s rate=%s)",
        settings.nova_model_id,
        settings.aws_region,
        settings.sample_rate,
    )

    async with SessionManager(config) as sm:
        logger.info("AgentDuet connected. Waiting for inbound calls...")

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification) -> None:
            participant = str(noti.participant)
            logger.info(
                "Incoming call %s from %s (subscriber=%s)",
                noti.call_id,
                participant,
                noti.subscriber,
            )

            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)

            @call.on_hangup
            def on_hangup(_evt) -> None:
                logger.info("Call %s hung up", call.id)

            @call.on_call_event(CallEvent.ERROR)
            def on_error(evt) -> None:
                logger.error(
                    "Call %s error: %s %s",
                    call.id,
                    evt.get("error_code"),
                    evt.get("error_message"),
                )

            await start_nova_sonic_session(call, settings=settings)

        await sm.run_forever()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
