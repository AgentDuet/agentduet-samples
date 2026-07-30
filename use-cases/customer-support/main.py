"""VibeRider Resolve — AgentDuet + Amazon Nova 2 Sonic.

Rider/driver voice support for lost items and ride complaints.
Cases are queued during the call; Pipedrive (or mock JSON) is written on hangup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import requests
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
from pipedrive import PipedriveClient
from tools import SupportTools

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


async def notify_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (
        os.getenv("TELEGRAM_SUPPORT_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    if not token or not chat:
        logger.info(
            "Telegram skipped (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). %s", text
        )
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send() -> None:
        resp = requests.post(
            url, json={"chat_id": chat, "text": text}, timeout=15
        )
        if resp.status_code >= 400:
            logger.error("Telegram send failed %s: %s", resp.status_code, resp.text)

    await asyncio.to_thread(_send)


async def run() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _configure_logging(log_level)

    api_key = os.environ["AGENTDUET_API_KEY"]
    connector_uuid = os.environ["AGENTDUET_CONNECTOR_UUID"]
    settings = NovaSettings.from_env()
    pipedrive = PipedriveClient()

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(sample_rate=settings.sample_rate),  # type: ignore[arg-type]
    )

    logger.info(
        "Starting VibeRider Resolve (Nova Sonic=%s region=%s rate=%s pipedrive_mock=%s)",
        settings.nova_model_id,
        settings.aws_region,
        settings.sample_rate,
        pipedrive.mock_mode,
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
            phone = (
                (call.participant.value if call.participant else None)
                or (noti.participant.value if noti.participant else None)
                or "unknown"
            )

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

            tools = SupportTools(pipedrive=pipedrive, caller_phone=phone)
            try:
                result = await start_nova_sonic_session(
                    call, settings=settings, tools=tools
                )
                if result:
                    await notify_telegram(f"VibeRider Resolve case finalized: {result}")
            except Exception:
                logger.exception("Call %s failed", call.id)
                try:
                    await tools.finalize_after_call()
                except Exception:
                    logger.exception("Finalize after failed call errored")
                try:
                    await call.close()
                except Exception:
                    pass

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
