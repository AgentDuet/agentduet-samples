import logging
import os
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger(__name__)


class N8nReservationClient:
    """Dispatches restaurant table reservation events to an n8n webhook."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        timeout_seconds: float = 8.0,
    ):
        self.webhook_url = webhook_url or os.getenv("N8N_RESERVATION_WEBHOOK_URL", "")
        self.timeout_seconds = timeout_seconds

    async def book_table(
        self,
        guest_name: str,
        party_size: int,
        reservation_time: str,
        phone_number: str,
        special_requests: str = "None",
    ) -> Dict[str, Any]:
        """Sends a reservation request to the n8n webhook.

        If webhook_url is not configured, returns a mock confirmation so local
        testing and dry runs work seamlessly.
        """
        payload = {
            "guest_name": guest_name,
            "party_size": party_size,
            "reservation_time": reservation_time,
            "phone_number": phone_number,
            "special_requests": special_requests or "None",
        }

        if not self.webhook_url:
            logger.warning("N8N_RESERVATION_WEBHOOK_URL not configured. Returning mock confirmation.")
            return {
                "status": "confirmed",
                "confirmation_code": "RES-4921",
                "message": f"Table reserved for {guest_name} (party of {party_size}) at {reservation_time}",
                "guest_name": guest_name,
                "party_size": party_size,
                "reservation_time": reservation_time,
                "mock": True,
            }

        headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                logger.info("Reservation confirmed via n8n: %s", data.get("confirmation_code"))
                return data
        except httpx.HTTPStatusError as exc:
            logger.error("n8n webhook HTTP error: %s - %s", exc.response.status_code, exc.response.text)
            return {
                "status": "error",
                "message": f"Workflow returned HTTP {exc.response.status_code}. Check n8n execution log.",
            }
        except Exception as exc:
            logger.error("Failed to connect to n8n webhook at %s: %s", self.webhook_url, exc)
            return {
                "status": "error",
                "message": f"Unable to reach n8n webhook: {str(exc)}",
            }
