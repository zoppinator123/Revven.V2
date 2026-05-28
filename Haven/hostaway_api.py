#!/usr/bin/env python3
"""
Small Hostaway Public API client for dashboard listing sync and edits.

Hostaway's v1 API uses JSON over HTTPS. This wrapper intentionally keeps writes
explicit: callers must choose the listing endpoint and payload to update.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, parse, request

BASE_URL = "https://api.hostaway.com/v1"
SNAPSHOT_PATH = Path(__file__).parent / "hostaway_api_snapshot.json"


class HostawayAPIError(RuntimeError):
    pass


class HostawayClient:
    def __init__(self, api_token: str, base_url: str = BASE_URL):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = None
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=45) as resp:
                data = resp.read().decode("utf-8")
        except error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise HostawayAPIError(f"Hostaway API {e.code}: {detail[:500]}") from e
        except error.URLError as e:
            raise HostawayAPIError(f"Hostaway API connection failed: {e.reason}") from e
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            raise HostawayAPIError("Hostaway returned a non-JSON response.") from e
        if isinstance(parsed, dict) and parsed.get("status") == "fail":
            raise HostawayAPIError(parsed.get("message") or "Hostaway API request failed.")
        return parsed.get("result", parsed) if isinstance(parsed, dict) else parsed

    def listings(self, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        query = parse.urlencode({"limit": limit, "offset": offset})
        result = self._request("GET", f"listings?{query}")
        return result if isinstance(result, list) else []

    def listing(self, listing_id: str | int) -> dict[str, Any]:
        result = self._request("GET", f"listings/{listing_id}")
        return result if isinstance(result, dict) else {}

    def calendar(self, listing_id: str | int, start_date: str, end_date: str) -> list[dict[str, Any]]:
        query = parse.urlencode({"listingMapId": listing_id, "startDate": start_date, "endDate": end_date})
        result = self._request("GET", f"listings/calendar?{query}")
        return result if isinstance(result, list) else []

    def update_listing(self, listing_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("PUT", f"listings/{listing_id}", payload)
        return result if isinstance(result, dict) else {"result": result}

    def update_pricing_settings(self, listing_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("PUT", f"listing/pricingSettings/{listing_id}", payload)
        return result if isinstance(result, dict) else {"result": result}


def client_from_env() -> HostawayClient: 18629
    token = os.environ.get("783f79c6941bd51d3ff6e9055f7525726492292cedfb043953fdf4760ed534e5")
    if not token:
        raise HostawayAPIError("Set HOSTAWAY_API_TOKEN before syncing Hostaway data.")
    return HostawayClient(token)


def read_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {}
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_snapshot(listings: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = {
        "source": BASE_URL,
        "listings_count": len(listings),
        "listings": listings,
    }
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot


def sync_read_only() -> dict[str, Any]:
    client = client_from_env()
    return write_snapshot(client.listings())


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def find_listing_for_property(property_name: str, pms_id: str | None, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    listings = snapshot.get("listings") or []
    clean_name = _clean(property_name).replace(": default", "")
    clean_pms_id = _clean(pms_id)
    for listing in listings:
        ids = {
            _clean(listing.get("id")),
            _clean(listing.get("listingMapId")),
            _clean(listing.get("externalListingId")),
        }
        if clean_pms_id and clean_pms_id in ids:
            return listing
    for listing in listings:
        title = _clean(listing.get("name") or listing.get("internalListingName") or listing.get("title"))
        if title and (title in clean_name or clean_name in title):
            return listing
    return None


def listing_summary(listing: dict[str, Any]) -> dict[str, Any]:
    editable_fields = [
        "name",
        "internalListingName",
        "description",
        "houseRules",
        "personCapacity",
        "bedroomsNumber",
        "bathroomsNumber",
        "checkInTimeStart",
        "checkOutTime",
    ]
    return {
        "listing_id": listing.get("id") or listing.get("listingMapId"),
        "name": listing.get("name") or listing.get("internalListingName") or listing.get("title"),
        "status": listing.get("status"),
        "city": listing.get("city"),
        "country": listing.get("country"),
        "editable_fields_present": [field for field in editable_fields if field in listing],
    }
