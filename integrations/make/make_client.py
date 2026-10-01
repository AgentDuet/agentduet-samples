import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class MakeHotelClient:
    """Client for communicating with Make.com webhooks for live room availability

    and booking confirmation.
    """

    def __init__(
        self,
        availability_webhook_url: Optional[str] = None,
        booking_webhook_url: Optional[str] = None,
        api_key: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.availability_webhook_url = availability_webhook_url or os.getenv(
            "MAKE_AVAILABILITY_WEBHOOK_URL",
            "https://hook.eu1.make.com/your-availability-webhook-id",
        )
        self.booking_webhook_url = booking_webhook_url or os.getenv(
            "MAKE_BOOKING_WEBHOOK_URL",
            "https://hook.eu1.make.com/your-booking-webhook-id",
        )
        self.api_key = api_key or os.getenv("MAKE_API_KEY", "")
        self.mock_mode = (
            mock_mode or os.getenv("MAKE_MOCK_MODE", "false").lower() == "true"
        )

        # Mock inventory state for offline testing
        # Structure: date_key -> list of booked room types
        self._mock_booked_dates: Dict[str, List[str]] = {
            "2026-12-31": ["Deluxe Room", "Executive Suite"],  # Fully booked
            "2026-12-25": ["Deluxe Room", "Executive Suite"],  # Fully booked
        }
        self._mock_bookings: List[Dict[str, Any]] = []

    async def check_availability(
        self,
        check_in: str,
        check_out: str,
        guests: int = 1,
    ) -> Dict[str, Any]:
        """Queries Make.com webhook for live room availability across the requested dates.

        Waits for a real response before returning.

        Returns:
            Dict containing:
                available (bool): True if at least one room type is open.
                room_options (list): List of available room dictionaries with rates.
        """
        payload = {
            "event": "check_availability",
            "check_in": check_in,
            "check_out": check_out,
            "guests": guests,
        }

        if self.mock_mode:
            logger.info("Mock mode: checking availability for %s to %s", check_in, check_out)
            # Check if dates are in the mock booked table
            if check_in in self._mock_booked_dates or check_out in self._mock_booked_dates:
                return {
                    "available": False,
                    "room_options": [],
                    "message": "No rooms available for selected dates.",
                }

            # Return two room options side by side with nightly rates
            return {
                "available": True,
                "room_options": [
                    {
                        "room_type": "Deluxe Room",
                        "nightly_rate": 150,
                        "currency": "USD",
                        "description": "Spacious room with king bed and city view",
                    },
                    {
                        "room_type": "Executive Suite",
                        "nightly_rate": 250,
                        "currency": "USD",
                        "description": "Luxury suite with separate lounge area and panoramic view",
                    },
                ],
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            logger.info(
                "Calling Make.com availability webhook at %s for %s to %s",
                self.availability_webhook_url,
                check_in,
                check_out,
            )
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    self.availability_webhook_url,
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info("Make.com availability response: %s", data)
                    return data
                logger.error("Make.com availability webhook returned %d: %s", resp.status_code, resp.text)
                return {"available": False, "room_options": [], "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.exception("Failed to query Make.com availability webhook: %s", e)
            return {"available": False, "room_options": [], "error": str(e)}

    async def create_booking(self, booking_data: Dict[str, Any]) -> Dict[str, Any]:
        """Sends confirmed booking details to Make.com.

        Make.com scenario:
        1. Records row in Google Sheets (locking dates and room type).
        2. Sends confirmation email to guest.
        3. Returns booking reference ID to caller.
        """
        payload = {
            "event": "create_booking",
            "guest_name": booking_data.get("guest_name"),
            "guest_email": booking_data.get("guest_email"),
            "check_in": booking_data.get("check_in"),
            "check_out": booking_data.get("check_out"),
            "guests": booking_data.get("guests", 1),
            "room_type": booking_data.get("room_type"),
            "nightly_rate": booking_data.get("nightly_rate"),
            "caller_phone": booking_data.get("caller_phone", ""),
        }

        if self.mock_mode:
            booking_id = f"BK-{uuid.uuid4().hex[:6].upper()}"
            record = {
                "booking_id": booking_id,
                "status": "confirmed",
                "email_sent": True,
                **payload,
            }
            self._mock_bookings.append(record)
            # Lock date range in mock state
            c_in = booking_data.get("check_in", "")
            r_type = booking_data.get("room_type", "")
            if c_in:
                if c_in not in self._mock_booked_dates:
                    self._mock_booked_dates[c_in] = []
                self._mock_booked_dates[c_in].append(r_type)

            logger.info("Mock mode: booking %s created successfully for %s", booking_id, payload["guest_name"])
            return {
                "status": "confirmed",
                "booking_id": booking_id,
                "email_sent": True,
                "guest_name": payload["guest_name"],
                "room_type": payload["room_type"],
            }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            logger.info(
                "Calling Make.com booking webhook at %s for guest %s",
                self.booking_webhook_url,
                payload.get("guest_name"),
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.booking_webhook_url,
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info("Make.com booking response: %s", data)
                    return data
                logger.error("Make.com booking webhook returned %d: %s", resp.status_code, resp.text)
                return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.exception("Failed to send booking to Make.com: %s", e)
            return {"status": "error", "message": str(e)}
