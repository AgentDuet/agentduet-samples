"""Outbound one-way voice notification dispatcher via AgentDuet.

Triggers automated telephone alerts when n8n detects high-confidence matches.
Customer notifications request a callback to verify details. Driver notifications
provide an informational update with no action required.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from agentduet import (
    Call,
    CallAudioConfig,
    CallClosedError,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

logger = logging.getLogger(__name__)


class OutboundNotificationDispatcher:
    """Manages one-way automated outbound phone alerts for matched items."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        connector_uuid: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.api_key = api_key or os.getenv("AGENTDUET_API_KEY")
        self.connector_uuid = connector_uuid or os.getenv("AGENTDUET_CONNECTOR_UUID")
        self.mock_mode = (
            mock_mode
            or os.getenv("LOST_FOUND_MOCK_MODE", "").lower() in ("true", "1", "yes")
            or not (self.api_key and self.connector_uuid)
        )
        self._sent_notifications: List[Dict[str, Any]] = []

    async def notify_customer(self, customer_phone: str, item_type: str) -> bool:
        """Dispatches automated one-way call asking customer to dial back and confirm."""
        message = (
            f"Hello, this is City Cab Lost and Found. We have found a potential match for your "
            f"reported {item_type}. Please call our lost-and-found hotline back to confirm the "
            f"details and claim your item. Thank you."
        )
        return await self._dispatch_call(customer_phone, "customer", message)

    async def notify_driver(self, driver_phone: str, item_type: str) -> bool:
        """Dispatches automated one-way call informing driver with no action required."""
        message = (
            f"Hello, this is City Cab Dispatch. This is an informational update regarding the "
            f"{item_type} you turned in. A passenger report has matched your item. No action is "
            f"required from you at this time, our team is currently verifying with the passenger. "
            f"Thank you for your assistance."
        )
        return await self._dispatch_call(driver_phone, "driver", message)

    async def notify_driver_unmatched_ride(
        self,
        driver_phone: str,
        ride_id: str,
        item_type: str,
    ) -> bool:
        """Dispatches proactive one-way alert asking driver to check vehicle for reported lost item."""
        message = (
            f"Hello, this is City Cab Dispatch. A passenger from ride {ride_id} has reported a lost "
            f"{item_type}. Please check your vehicle when safe to do so. If you locate the item, "
            f"please call our lost-and-found hotline back to log it. Thank you."
        )
        return await self._dispatch_call(driver_phone, "driver_check_vehicle", message)

    async def _dispatch_call(self, recipient_phone: str, role: str, message: str) -> bool:
        """Initiates an outbound phone call via AgentDuet SessionManager."""
        logger.info("Outbound notification to %s [%s]: %s", role, recipient_phone, message)

        record = {
            "recipient": recipient_phone,
            "role": role,
            "message": message,
        }
        self._sent_notifications.append(record)

        if self.mock_mode:
            logger.info("Mock mode or credentials unset: simulated one-way call to %s", recipient_phone)
            return True

        config = SessionManagerConfig.create(
            api_key=self.api_key,
            connector_uuid=self.connector_uuid,
            call_audio=CallAudioConfig(sample_rate=24000),
        )

        try:
            async with SessionManager(config) as sm:
                session = await sm.open_session(new_session_id(), recipient_phone)
                # Initiate outbound voice call
                call = await session.create_call(recipient_phone)
                logger.info("Outbound call placed with id %s to %s", call.id, recipient_phone)

                # Wait for remote party to answer
                answered = await call.wait_for_answer(timeout_secs=30)
                if not answered:
                    logger.warning("Recipient %s did not answer notification call", recipient_phone)
                    await call.close()
                    return False

                # Deliver one-way announcement and close call
                await asyncio.sleep(6.0)
                await call.close()
                return True
        except Exception as e:
            logger.exception("Failed to dispatch outbound notification to %s: %s", recipient_phone, e)
            return False
