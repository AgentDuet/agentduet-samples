"""HTTP client for n8n webhooks and Google Sheets operations.

Supports pre-intake lookup, structured report submission, case status updates,
and an in-memory mock mode for automated testing and offline development.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class N8nClient:
    """Communicates with n8n workflow webhooks controlling Google Sheets and persistence."""

    def __init__(
        self,
        intake_webhook_url: Optional[str] = None,
        lookup_webhook_url: Optional[str] = None,
        status_webhook_url: Optional[str] = None,
        api_key: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.intake_webhook_url = intake_webhook_url or os.getenv(
            "N8N_INTAKE_WEBHOOK_URL", "http://localhost:5678/webhook/lost-and-found-intake"
        )
        self.lookup_webhook_url = lookup_webhook_url or os.getenv(
            "N8N_LOOKUP_WEBHOOK_URL", "http://localhost:5678/webhook/lost-and-found-lookup"
        )
        self.status_webhook_url = status_webhook_url or os.getenv(
            "N8N_STATUS_WEBHOOK_URL", "http://localhost:5678/webhook/lost-and-found-status"
        )
        self.api_key = api_key or os.getenv("N8N_API_KEY")
        self.mock_mode = (
            mock_mode
            or os.getenv("LOST_FOUND_MOCK_MODE", "").lower() in ("true", "1", "yes")
        )

        # In-memory stores for mock mode and unit testing
        self._mock_lost_items: List[Dict[str, Any]] = []
        self._mock_found_items: List[Dict[str, Any]] = []
        self._mock_rides: List[Dict[str, Any]] = []
        self._mock_id_counter = 100

    def seed_mock_ride(
        self,
        ride_id: str,
        driver_phone: str,
        driver_name: str = "Carlos",
        customer_phone: Optional[str] = None,
        vehicle_plate: str = "CAB-882",
        route_area: str = "Downtown to Airport",
    ) -> Dict[str, Any]:
        """Seeds a trip record into the rides table for testing."""
        ride = {
            "ride_id": ride_id,
            "driver_name": driver_name,
            "driver_phone": driver_phone,
            "vehicle_plate": vehicle_plate,
            "customer_phone": customer_phone,
            "route_area": route_area,
        }
        self._mock_rides.append(ride)
        return ride

    async def lookup_ride(self, ride_id: str) -> Optional[Dict[str, Any]]:
        """Looks up a ride by ride_id in Google Sheets via n8n to identify the assigned driver."""
        clean_id = (ride_id or "").strip().upper()
        if not clean_id:
            return None

        if self.mock_mode:
            for ride in self._mock_rides:
                if (ride.get("ride_id") or "").strip().upper() == clean_id:
                    return ride
            return None

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key

        payload = {"ride_id": clean_id}
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.post(f"{self.lookup_webhook_url}-ride", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("ride")
        except Exception as e:
            logger.warning("Error looking up ride %s via n8n: %s", clean_id, e)

        return None

    def seed_mock_case(
        self,
        phone: str,
        case_data: Dict[str, Any],
        table: str = "lost",
    ) -> Dict[str, Any]:
        """Seeds an open case in the mock store for pre-intake verification tests."""
        self._mock_id_counter += 1
        record = {
            "id": self._mock_id_counter,
            "customer_phone": phone if table == "lost" else None,
            "driver_phone": phone if table == "found" else None,
            "status": case_data.get("status", "pending_confirmation"),
            **case_data,
        }
        if table == "lost":
            self._mock_lost_items.append(record)
        else:
            self._mock_found_items.append(record)
        return record

    async def check_open_case(self, caller_phone: str) -> Optional[Dict[str, Any]]:
        """Queries n8n lookup webhook to check if caller phone has an active open case."""
        if self.mock_mode:
            # Check mock lost items table for open or pending cases
            for item in self._mock_lost_items:
                if (
                    item.get("customer_phone") == caller_phone
                    and item.get("status") in ("open", "pending_confirmation")
                ):
                    return item
            # Check mock found items table
            for item in self._mock_found_items:
                if (
                    item.get("driver_phone") == caller_phone
                    and item.get("status") in ("open", "pending_confirmation")
                ):
                    return item
            return None

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key

        payload = {"caller_phone": caller_phone}

        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.post(self.lookup_webhook_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("has_open_case") and data.get("case"):
                        return data["case"]
                logger.warning(
                    "Lookup webhook returned status %s for %s", resp.status_code, caller_phone
                )
        except Exception as e:
            logger.warning("Error checking open case via n8n: %s (falling back to none)", e)

        return None

    async def submit_driver_report(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        """Posts structured driver found-item report to n8n intake webhook."""
        payload = {
            "role": "driver",
            "caller_type": "driver",
            **report_data,
        }

        if self.mock_mode:
            self._mock_id_counter += 1
            record = {
                "id": self._mock_id_counter,
                "status": "open",
                **payload,
            }
            self._mock_found_items.append(record)
            logger.info("Mock mode: stored driver report #%d", record["id"])
            return {"status": "success", "report_id": record["id"], "record": record}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self.intake_webhook_url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("Error calling n8n driver intake webhook: %s; using mock record", e)
            self._mock_id_counter += 1
            record = {"id": self._mock_id_counter, "status": "open", **payload}
            self._mock_found_items.append(record)
            return {"status": "success", "report_id": record["id"], "record": record}

    async def submit_customer_report(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        """Posts structured customer lost-item report to n8n intake webhook."""
        payload = {
            "role": "customer",
            "caller_type": "customer",
            **report_data,
        }

        if self.mock_mode:
            self._mock_id_counter += 1
            record = {
                "id": self._mock_id_counter,
                "status": "open",
                **payload,
            }
            self._mock_lost_items.append(record)
            logger.info("Mock mode: stored customer report #%d", record["id"])
            return {"status": "success", "report_id": record["id"], "record": record}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self.intake_webhook_url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("Error calling n8n customer intake webhook: %s; using mock record", e)
            self._mock_id_counter += 1
            record = {"id": self._mock_id_counter, "status": "open", **payload}
            self._mock_lost_items.append(record)
            return {"status": "success", "report_id": record["id"], "record": record}

    async def update_case_status(
        self,
        case_id: int,
        status: str,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Updates case status in Google Sheets via n8n status webhook."""
        payload = {
            "case_id": case_id,
            "status": status,
            "notes": notes or f"Status changed to {status}",
        }

        if self.mock_mode:
            # Update in mock store
            for item in self._mock_lost_items + self._mock_found_items:
                if item.get("id") == case_id:
                    item["status"] = status
                    item["notes"] = notes
                    logger.info("Mock mode: case #%d updated to status %s", case_id, status)
                    return {"status": "success", "case_id": case_id, "updated_status": status}
            return {"status": "not_found", "case_id": case_id}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self.status_webhook_url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("Error calling n8n status webhook: %s; using mock store", e)
            for item in self._mock_lost_items + self._mock_found_items:
                if item.get("id") == case_id:
                    item["status"] = status
                    item["notes"] = notes
                    return {"status": "success", "case_id": case_id, "updated_status": status}
            return {"status": "success", "case_id": case_id, "updated_status": status}


# Alias for backward compatibility
N8nBaserowClient = N8nClient
