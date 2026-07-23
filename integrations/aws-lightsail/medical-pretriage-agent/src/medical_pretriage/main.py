"""Medical Pre-Triage Agent entrypoint.

Connects to AgentDuet, answers inbound calls, and bridges caller audio to
Amazon Nova 2 Sonic. Transcripts are written to CloudWatch Logs.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agentduet import (
    CallAudioConfig,
    CallEvent,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

from medical_pretriage.agent.nova_sonic import start_nova_sonic_session
from medical_pretriage.config import Settings
from medical_pretriage.health import set_ready, start_health_server, stop_health_server
from medical_pretriage.observability.cloudwatch import TranscriptLogger

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("agentduet").setLevel(getattr(logging, level, logging.INFO))


async def run() -> None:
    settings = Settings.from_env()
    _configure_logging(settings.log_level)

    start_health_server(settings.health_host, settings.health_port)

    transcripts = TranscriptLogger(
        enabled=settings.cloudwatch_enabled,
        log_group=settings.cloudwatch_log_group,
        stream_prefix=settings.cloudwatch_log_stream_prefix,
        region=settings.aws_region,
    )

    config = SessionManagerConfig.create(
        api_key=settings.agentduet_api_key,
        connector_uuid=settings.agentduet_connector_uuid,
        call_audio=CallAudioConfig(sample_rate=settings.sample_rate),  # type: ignore[arg-type]
    )

    logger.info(
        "Starting Medical Pre-Triage Agent (Nova Sonic=%s region=%s)",
        settings.nova_model_id,
        settings.aws_region,
    )

    async with SessionManager(config) as sm:
        set_ready(True)
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

            await start_nova_sonic_session(
                call,
                settings=settings,
                transcripts=transcripts,
                participant=participant,
                subscriber=noti.subscriber,
            )

        try:
            await sm.run_forever()
        finally:
            set_ready(False)
            stop_health_server()


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
