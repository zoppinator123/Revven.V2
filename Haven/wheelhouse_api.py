#!/usr/bin/env python3
"""
Read-only Wheelhouse Revenue Management API client for the dashboard.
"""

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.usewheelhouse.com/ss_api/v1"
SNAPSHOT_PATH = Path(__file__).parent / "wheelhouse_api_snapshot.json"
MARKETING_PATH = Path(__file__).parent / "marketing_links.csv"


class WheelhouseAPIError(RuntimeError):
    pass


@dataclass
class WheelhouseClient:
    api_key: str
    base_url: str = BASE_URL
    timeout: int = 30

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        req = Request(url, headers={
            "Accept": "application/json",
            "X-Integration-Api-Key": self.api_key,
        })
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else None
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise WheelhouseAPIError(f"Wheelhouse API {e.code}: {body[:300]}") from e
        except URLError as e:
            raise WheelhouseAPIError(f"Wheelhouse API connection failed: {e.reason}") from e

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = Request(url, data=body, method=method, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Integration-Api-Key": self.api_key,
        })
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise WheelhouseAPIError(f"Wheelhouse API {e.code}: {body_text[:500]}") from e
        except URLError as e:
            raise WheelhouseAPIError(f"Wheelhouse API connection failed: {e.reason}") from e

    def listings(
        self,
        *,
        exclude_inactive: bool = True,
        include_managed_listings: bool = True,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        listings: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.get("/listings", {
                "exclude_inactive": str(exclude_inactive).lower(),
                "include_managed_listings": str(include_managed_listings).lower(),
                "per_page": per_page,
                "page": page,
            })
            if not isinstance(batch, list):
                raise WheelhouseAPIError("Wheelhouse /listings returned an unexpected response.")
            listings.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return listings

    def listing_kpis(self, listing_id: str, channel: str, days: int = 60) -> dict[str, Any]:
        return self.get(f"/listings/{listing_id}/kpis", {"channel": channel, "days": days})

    def listing_preferences(self, listing_id: str, channel: str) -> dict[str, Any]:
        return self.get(f"/preferences/{listing_id}", {"channel": channel})

    def update_preferences(self, listing_id: str, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json_request("PUT", f"/preferences/{listing_id}", params={"channel": channel}, payload=payload)

    def set_custom_rate(self, listing_id: str, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json_request("PUT", f"/listings/{listing_id}/custom_rates", params={"channel": channel}, payload=payload)

    def preferences_batch(self, listing_ids: list[str], channel: str) -> list[dict[str, Any]]:
        if not listing_ids:
            return []
        return self.get("/preferences", {"channel": channel, "listing_ids": listing_ids})

    def price_calendar(
        self,
        listing_id: str,
        channel: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"channel": channel}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self.get(f"/listings/{listing_id}/price_calendar", params)

    def reservations(
        self,
        listing_id: str,
        channel: str,
        start_date: str | None = None,
        end_date: str | None = None,
        date_filter_type: str = "stay_date",
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "channel": channel,
            "date_filter_type": date_filter_type,
            "per_page": per_page,
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self.get(f"/listings/{listing_id}/reservations", params)


def client_from_env() -> WheelhouseClient:
    api_key = os.environ.get("WHEELHOUSE_API_KEY")
    if not api_key:
        raise WheelhouseAPIError("Set WHEELHOUSE_API_KEY before syncing Wheelhouse API data.")
    return WheelhouseClient(api_key=api_key)


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _normalize_title(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.removeprefix(".off ").removeprefix(".lt ").removeprefix(".onb ").removeprefix(".fema ")
    text = re.sub(r":\s*default$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _matches_listing(listing: dict[str, Any], listing_id: str) -> bool:
    if not listing_id:
        return False
    channel_ids = listing.get("channel_ids") or {}
    ids = {
        _clean_id(listing.get("id")),
        _clean_id(channel_ids.get("airbnb")),
        _clean_id(channel_ids.get("vrbo")),
        _clean_id(channel_ids.get("tripadvisor")),
    }
    return listing_id in ids


def find_listing_for_channel_id(
    channel_id: str,
    preferred_channel: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    snapshot = snapshot if snapshot is not None else read_snapshot()
    preferred = (preferred_channel or "").lower()
    fallback = None
    for listing in snapshot.get("listings", []):
        if not _matches_listing(listing, _clean_id(channel_id)):
            continue
        channel = str(listing.get("channel") or "").lower()
        if preferred and preferred in channel:
            return listing
        fallback = fallback or listing
    return fallback


def find_listing_for_property(
    property_name: str,
    *,
    streamline_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    snapshot = snapshot if snapshot is not None else read_snapshot()
    clean_streamline = _clean_id(streamline_id)
    target = _normalize_title(property_name)

    if clean_streamline:
        for listing in snapshot.get("listings", []):
            ids = {
                _clean_id(listing.get("id")),
                _clean_id(listing.get("wheelhouse_id")),
                *(_clean_id(v) for v in (listing.get("channel_ids") or {}).values()),
            }
            if clean_streamline in ids:
                return listing

    for listing in snapshot.get("listings", []):
        title = _normalize_title(listing.get("title") or listing.get("nickname"))
        if title and target and (title == target or title in target or target in title):
            return listing
    return None


def find_preference_for_listing(
    listing: dict[str, Any] | None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not listing:
        return None
    snapshot = snapshot if snapshot is not None else read_snapshot()
    listing_id = _clean_id(listing.get("id"))
    channel = str(listing.get("channel") or "").lower()
    for pref in snapshot.get("preferences", []):
        pref_id = _clean_id(pref.get("listing_id") or pref.get("partner_listing_id"))
        pref_channel = str(pref.get("channel") or "").lower()
        if pref_id == listing_id and (not pref_channel or not channel or pref_channel == channel):
            return pref
    return None


def summarize_preferences(pref: dict[str, Any] | None) -> dict[str, Any]:
    if not pref:
        return {}

    def _preset(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("type") or ("enabled" if value else "configured"))
        if value is None:
            return "unknown"
        return str(value)

    min_stay_rules = pref.get("minimum_stay_rules_v3") or []
    min_price_rules = pref.get("minimum_price_rules_v3") or []
    max_price_rules = pref.get("maximum_price_rules_v3") or []
    occupancy = pref.get("occupancy_pacing") or {}
    long_term = pref.get("long_term_discounts") or {}

    return {
        "base_price": pref.get("base_price"),
        "base_price_adjustment": pref.get("base_price_adjustment"),
        "automatic_rate_posting_enabled": pref.get("automatic_rate_posting_enabled"),
        "weekly_discount": pref.get("weekly_discount"),
        "monthly_discount": pref.get("monthly_discount"),
        "last_minute_discount": _preset(pref.get("last_minute_discount")),
        "far_future_premium": _preset(pref.get("far_future_premium")),
        "seasonality_adjustment": _preset(pref.get("seasonality_adjustment")),
        "day_of_week": _preset(pref.get("day_of_week")),
        "gap_night": _preset(pref.get("gap_night")),
        "min_stays_enabled": pref.get("min_stays_enabled"),
        "minimum_stay_rules_count": len(min_stay_rules),
        "minimum_price_rules_count": len(min_price_rules),
        "maximum_price_rules_count": len(max_price_rules),
        "occupancy_pacing_enabled": bool(occupancy),
        "long_term_discounts_enabled": bool(long_term.get("active")),
        "updated_at": pref.get("updated_at"),
    }


def calendar_summary(calendar: list[dict[str, Any]]) -> dict[str, Any]:
    if not calendar:
        return {}
    available = [d for d in calendar if d.get("is_available")]
    booked = [d for d in calendar if d.get("is_booked")]
    prices = [d.get("price") for d in available if isinstance(d.get("price"), (int, float))]
    longest_run: list[dict[str, Any]] = []
    current_run: list[dict[str, Any]] = []
    for day in available:
        price = day.get("price")
        stay_date = day.get("stay_date")
        if not isinstance(price, (int, float)) or not stay_date:
            continue
        if not current_run:
            current_run = [day]
            continue
        prev = current_run[-1]
        try:
            prev_date = datetime.fromisoformat(str(prev.get("stay_date"))).date()
            this_date = datetime.fromisoformat(str(stay_date)).date()
            consecutive = (this_date - prev_date).days == 1
        except ValueError:
            consecutive = False
        if prev.get("price") == price and consecutive:
            current_run.append(day)
        else:
            if len(current_run) > len(longest_run):
                longest_run = current_run
            current_run = [day]
    if len(current_run) > len(longest_run):
        longest_run = current_run
    stagnant = None
    if len(longest_run) >= 4:
        stagnant = {
            "start_date": longest_run[0].get("stay_date"),
            "end_date": longest_run[-1].get("stay_date"),
            "nights": len(longest_run),
            "price": longest_run[0].get("price"),
            "suggested_nudge": max(1, longest_run[0].get("price") - 2) if isinstance(longest_run[0].get("price"), (int, float)) else None,
        }
    return {
        "days": len(calendar),
        "available_nights": len(available),
        "booked_nights": len(booked),
        "avg_available_price": round(sum(prices) / len(prices), 2) if prices else None,
        "min_available_price": min(prices) if prices else None,
        "max_available_price": max(prices) if prices else None,
        "next_available_dates": [d.get("stay_date") for d in available[:5]],
        "next_available_rates": [
            {"date": d.get("stay_date"), "price": d.get("price")}
            for d in available[:8]
        ],
        "stagnant_price_window": stagnant,
    }


def reservations_summary(reservations: list[dict[str, Any]]) -> dict[str, Any]:
    if not reservations:
        return {"count": 0, "channels": {}, "recent": []}
    channels: dict[str, int] = {}
    recent = []
    for reservation in reservations:
        source = reservation.get("source_name") or "Unknown"
        channels[source] = channels.get(source, 0) + 1
        recent.append({
            "source": source,
            "start_date": reservation.get("start_date"),
            "end_date": reservation.get("end_date"),
            "booked_at": reservation.get("booked_at"),
            "total_price": reservation.get("total_price"),
            "currency": reservation.get("currency"),
        })
    return {
        "count": len(reservations),
        "channels": channels,
        "recent": recent[:8],
    }


def _listing_rating_maps(listings: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict]]:
    airbnb: dict[str, dict] = {}
    vrbo: dict[str, dict] = {}
    for listing in listings:
        channel = str(listing.get("channel") or "").lower()
        listing_id = _clean_id(listing.get("id"))
        channel_ids = listing.get("channel_ids") or {}
        payload = {
            "rating": listing.get("star_rating"),
            "reviews": listing.get("num_reviews"),
            "photos": listing.get("num_photos"),
            "thumb_url": listing.get("thumb_url") or "",
            "title": listing.get("title") or listing.get("nickname") or "",
        }

        if listing_id and "airbnb" in channel:
            airbnb[listing_id] = payload
        if listing_id and "vrbo" in channel:
            vrbo[listing_id] = payload

        airbnb_id = _clean_id(channel_ids.get("airbnb"))
        vrbo_id = _clean_id(channel_ids.get("vrbo"))
        if airbnb_id:
            airbnb[airbnb_id] = payload
        if vrbo_id:
            vrbo[vrbo_id] = payload
    return airbnb, vrbo


def update_marketing_ratings(listings: list[dict[str, Any]], path: Path = MARKETING_PATH) -> int:
    if not path.exists():
        return 0

    airbnb, vrbo = _listing_rating_maps(listings)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    required = [
        "Airbnb Rating",
        "Airbnb Reviews",
        "Airbnb Photos",
        "Airbnb Thumb URL",
        "Vrbo Rating",
        "Vrbo Reviews",
        "Vrbo Photos",
        "Vrbo Thumb URL",
        "Airbnb Wheelhouse Match",
        "Vrbo Wheelhouse Match",
    ]
    for field in required:
        if field not in fieldnames:
            fieldnames.append(field)

    updated = 0
    for row in rows:
        airbnb_id = _clean_id(row.get("Airbnb"))
        vrbo_id = _clean_id(row.get("Vrbo") or row.get("VRBO"))

        if airbnb_id in airbnb and airbnb[airbnb_id]["rating"] is not None:
            row["Airbnb Rating"] = airbnb[airbnb_id]["rating"]
            row["Airbnb Reviews"] = airbnb[airbnb_id].get("reviews") or ""
            row["Airbnb Photos"] = airbnb[airbnb_id].get("photos") or ""
            row["Airbnb Thumb URL"] = airbnb[airbnb_id].get("thumb_url") or ""
            row["Airbnb Wheelhouse Match"] = "yes"
            updated += 1
        elif airbnb_id:
            row["Airbnb Wheelhouse Match"] = "no"

        if vrbo_id in vrbo and vrbo[vrbo_id]["rating"] is not None:
            row["Vrbo Rating"] = vrbo[vrbo_id]["rating"]
            row["Vrbo Reviews"] = vrbo[vrbo_id].get("reviews") or ""
            row["Vrbo Photos"] = vrbo[vrbo_id].get("photos") or ""
            row["Vrbo Thumb URL"] = vrbo[vrbo_id].get("thumb_url") or ""
            row["Vrbo Wheelhouse Match"] = "yes"
            updated += 1
        elif vrbo_id:
            row["Vrbo Wheelhouse Match"] = "no"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return updated


def _preferences_by_channel(client: WheelhouseClient, listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    listing_ids_by_channel: dict[str, list[str]] = {}
    for listing in listings:
        listing_id = _clean_id(listing.get("id"))
        channel = str(listing.get("channel") or "").strip()
        if listing_id and channel:
            listing_ids_by_channel.setdefault(channel, []).append(listing_id)

    preferences: list[dict[str, Any]] = []
    errors: list[str] = []
    for channel, listing_ids in listing_ids_by_channel.items():
        for start in range(0, len(listing_ids), 50):
            batch = listing_ids[start:start + 50]
            try:
                result = client.preferences_batch(batch, channel)
                if isinstance(result, list):
                    preferences.extend(result)
                else:
                    errors.append(f"{channel}: unexpected preferences response")
            except WheelhouseAPIError as e:
                errors.append(f"{channel}: {e}")
    return preferences, errors


def sync_read_only() -> dict[str, Any]:
    client = client_from_env()
    listings = client.listings()
    preferences, preference_errors = _preferences_by_channel(client, listings)
    ratings_updated = update_marketing_ratings(listings)
    snapshot = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE_URL,
        "listings_count": len(listings),
        "preferences_count": len(preferences),
        "ratings_updated": ratings_updated,
        "preference_errors": preference_errors,
        "listings": listings,
        "preferences": preferences,
    }
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot


if __name__ == "__main__":
    result = sync_read_only()
    print(
        f"Synced {result['listings_count']} listings; "
        f"cached {result['preferences_count']} preferences; "
        f"updated {result['ratings_updated']} marketing rating fields."
    )
