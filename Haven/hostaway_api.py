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

    def reservations(self, limit: int = 500, offset: int = 0, sort_order: str = "lastUpdatedOn desc") -> list[dict[str, Any]]:
        query = parse.urlencode({"limit": limit, "offset": offset, "sortOrder": sort_order})
        result = self._request("GET", f"reservations?{query}")
        return result if isinstance(result, list) else []

    def last_booked_by_listing(self) -> dict[str, str]:
        """Return {listingMapId: last_booked_date_str} for all listings using recent reservations."""
        seen: dict[str, str] = {}
        offset = 0
        while True:
            batch = self.reservations(limit=500, offset=offset)
            if not batch:
                break
            for r in batch:
                lid = str(r.get("listingMapId") or r.get("listingId") or "")
                booked_date = (r.get("createdAt") or r.get("bookingDate") or "")[:10]
                if lid and booked_date and lid not in seen:
                    seen[lid] = booked_date
            if len(batch) < 500:
                break
            offset += 500
            if len(seen) > 5000:
                break
        return seen


        result = self._request("PUT", f"listings/{listing_id}", payload)
        return result if isinstance(result, dict) else {"result": result}

    def update_pricing_settings(self, listing_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("PUT", f"listing/pricingSettings/{listing_id}", payload)
        return result if isinstance(result, dict) else {"result": result}


    def reservation_stats_by_listing(self, days_back: int = 180) -> dict[str, dict]:
        """Return per-listing reservation stats: avg lead time, avg LOS, source breakdown, total nights."""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days_back)).isoformat()
        stats: dict[str, dict] = {}
        offset = 0
        while True:
            batch = self.reservations(limit=500, offset=offset, sort_order="arrivalDate desc")
            if not batch:
                break
            older = 0
            for r in batch:
                arrival = (r.get("arrivalDate") or r.get("checkIn") or "")[:10]
                if arrival and arrival < cutoff:
                    older += 1
                    continue
                lid = str(r.get("listingMapId") or r.get("listingId") or "")
                if not lid:
                    continue
                if lid not in stats:
                    stats[lid] = {"lead_times": [], "los": [], "sources": {}, "total_nights": 0, "last_booked": ""}
                s = stats[lid]
                booked_date = (r.get("createdAt") or r.get("bookingDate") or "")[:10]
                if booked_date and (not s["last_booked"] or booked_date > s["last_booked"]):
                    s["last_booked"] = booked_date
                if arrival and booked_date:
                    try:
                        lead = (date.fromisoformat(arrival) - date.fromisoformat(booked_date)).days
                        if 0 <= lead <= 730:
                            s["lead_times"].append(lead)
                    except (ValueError, TypeError):
                        pass
                departure = (r.get("departureDate") or r.get("checkOut") or "")[:10]
                if arrival and departure:
                    try:
                        nights = (date.fromisoformat(departure) - date.fromisoformat(arrival)).days
                        if 0 < nights <= 90:
                            s["los"].append(nights)
                            s["total_nights"] += nights
                    except (ValueError, TypeError):
                        pass
                source = str(r.get("source") or r.get("channelName") or r.get("channel") or "unknown").lower()
                s["sources"][source] = s["sources"].get(source, 0) + 1
            if len(batch) < 500 or older > 50:
                break
            offset += 500
        result = {}
        for lid, s in stats.items():
            result[lid] = {
                "avg_lead_days": round(sum(s["lead_times"]) / len(s["lead_times"])) if s["lead_times"] else None,
                "avg_los": round(sum(s["los"]) / len(s["los"]), 1) if s["los"] else None,
                "total_nights": s["total_nights"],
                "sources": s["sources"],
                "last_booked": s["last_booked"],
            }
        return result


def client_from_env() -> HostawayClient:
    token = os.environ.get("HOSTAWAY_API_TOKEN")
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
