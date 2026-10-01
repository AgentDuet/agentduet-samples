"""AgentDuet telephony server for City Cab Lost and Found.

Bridges live inbound phone calls to Amazon Nova 2 Sonic (24 kHz LPCM) with
pre-intake case verification and instant barge-in support. Also provides an
HTTP webhook endpoint for n8n to trigger outbound one-way notifications.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agentduet import (
    Call,
    CallAudioConfig,
    CallClosedError,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

from lost_and_found_agent import NovaSonicLostAndFoundBridge
from n8n_client import N8nClient
from outbound_dispatcher import OutboundNotificationDispatcher

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize clients
n8n_client = N8nClient()
dispatcher = OutboundNotificationDispatcher()

session_manager: Optional[SessionManager] = None


class MatchNotificationPayload(BaseModel):
    """Schema for match notification requests from n8n."""
    customer_phone: str
    driver_phone: str
    item_type: str
    lost_id: Optional[int] = None
    found_id: Optional[int] = None
    confidence_score: Optional[float] = None


class UnmatchedCustomerRidePayload(BaseModel):
    """Schema for handling customer reports with no immediate found matches."""
    ride_id: str
    item_type: str
    customer_phone: Optional[str] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Lifecycle manager starting and stopping the AgentDuet telephony listener."""
    global session_manager

    api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")
    subscriber = os.getenv("AGENTDUET_SUBSCRIBER")

    if not api_key or not connector_uuid:
        logger.warning(
            "AGENTDUET_API_KEY or AGENTDUET_CONNECTOR_UUID unset; running in mock/offline mode"
        )
        yield
        return

    # Configure 24 kHz telephony audio to match Amazon Nova 2 Sonic
    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=24000,
            buffer_size=1024 * 1024,
        ),
    )

    async with SessionManager(config) as sm:
        session_manager = sm

        @sm.on_incoming_call
        async def on_call(noti: IncomingCallNotification):
            logger.info("Incoming call received from %s", noti.subscriber)
            session = await sm.open_session(new_session_id(), noti.subscriber)
            call = await session.process_call(noti)

            if not await call.answer():
                logger.error("Failed to answer incoming call %s", call.id)
                return

            asyncio.create_task(handle_voice_call(call))

        sm_task = asyncio.create_task(sm.run_forever(install_signal_handlers=False))
        logger.info(
            "AgentDuet SessionManager connected: %s for Lost and Found line (%s)",
            sm.id,
            subscriber,
        )

        yield

        sm_task.cancel()
        session_manager = None


app = FastAPI(title="City Cab Lost and Found AgentDuet Integration", lifespan=lifespan)


async def handle_voice_call(call: Call) -> None:
    """Handles an active voice call via Amazon Nova 2 Sonic."""
    caller_phone = str(call.caller)
    logger.info("Handling voice call %s from %s", call.id, caller_phone)

    # 1. Pre-intake check: query n8n lookup webhook for active open cases
    open_case = await n8n_client.check_open_case(caller_phone)
    if open_case:
        logger.info(
            "Pre-intake check: found open case #%s for caller %s",
            open_case.get("id"),
            caller_phone,
        )

    # 2. Initialize Nova Sonic speech bridge
    bridge = NovaSonicLostAndFoundBridge(
        call=call,
        n8n_client=n8n_client,
        caller_phone=caller_phone,
        pending_case=open_case,
        dispatcher=dispatcher,
    )
    call.on_hangup(bridge.on_hangup)

    try:
        await bridge.start()

        async def uplink():
            """Streams caller microphone audio to Nova 2 Sonic."""
            try:
                async for chunk in call.caller.audio_stream():
                    await bridge.send_audio(chunk)
            except CallClosedError:
                pass
            finally:
                await bridge.close()

        async def downlink():
            """Streams Nova 2 Sonic voice output back to caller."""
            await bridge.process_responses()

        await asyncio.gather(uplink(), downlink())
    except CallClosedError:
        logger.info("Call %s closed by caller", call.id)
    except Exception as e:
        logger.exception("Error handling call %s: %s", call.id, e)
    finally:
        await bridge.close()
        try:
            await call.close()
        except Exception:
            pass


@app.post("/notify-match")
async def notify_match(payload: MatchNotificationPayload) -> Dict[str, Any]:
    """Endpoint called by n8n when a high-confidence match is detected.

    Triggers an automated one-way callback alert to the customer and an
    informational update to the driver.
    """
    logger.info("Received match notification: %s", payload.model_dump())

    # 1. Notify customer to call back and confirm
    customer_ok = await dispatcher.notify_customer(
        customer_phone=payload.customer_phone,
        item_type=payload.item_type,
    )

    # 2. Notify driver with informational update
    driver_ok = await dispatcher.notify_driver(
        driver_phone=payload.driver_phone,
        item_type=payload.item_type,
    )

    return {
        "status": "success",
        "customer_notified": customer_ok,
        "driver_notified": driver_ok,
        "item_type": payload.item_type,
    }


@app.post("/check-unmatched-ride")
async def check_unmatched_ride(payload: UnmatchedCustomerRidePayload) -> Dict[str, Any]:
    """Endpoint called by n8n when a customer report has no matching found item.

    Looks up ride_id in the rides table to identify the driver, and triggers a
    proactive one-way outbound call asking the driver to inspect their vehicle.
    """
    logger.info("Checking unmatched ride report: %s", payload.model_dump())
    ride = await n8n_client.lookup_ride(payload.ride_id)
    if not ride:
        return {
            "status": "ride_not_found",
            "ride_id": payload.ride_id,
            "driver_notified": False,
        }

    driver_phone = ride.get("driver_phone")
    if not driver_phone:
        return {
            "status": "no_driver_phone",
            "ride_id": payload.ride_id,
            "driver_notified": False,
        }

    driver_ok = await dispatcher.notify_driver_unmatched_ride(
        driver_phone=driver_phone,
        ride_id=payload.ride_id,
        item_type=payload.item_type,
    )

    return {
        "status": "success",
        "ride_id": payload.ride_id,
        "driver_name": ride.get("driver_name"),
        "driver_phone": driver_phone,
        "driver_notified": driver_ok,
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "lost-and-found-agentduet"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
