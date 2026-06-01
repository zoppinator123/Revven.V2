#!/usr/bin/env python3
"""
KCity (Knoxville City) surge date DSO task generator.

Generates Date Specific Override tasks for UT Football weekends and
holiday surge dates across all active KCity listings, segmented by
bedroom count and demand level.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

SOURCE = "kcity_surge_dso"

# ---------------------------------------------------------------------------
# Surge calendar — all dates are 2025/2026 season
# ---------------------------------------------------------------------------

# Each entry: (start_iso, end_iso, label, demand_level)
# demand_level: "high" or "medium"
# 2025 season (historical reference — may already be in PriceLabs for 3BR 349115)
SURGE_DATES_2025: list[tuple[str, str, str, str]] = [
    ("2025-09-04", "2025-09-05", "UT Football", "high"),
    ("2025-09-18", "2025-09-19", "UT Football", "high"),
    ("2025-09-24", "2025-09-27", "UT Football", "high"),
    ("2025-10-01", "2025-10-04", "UT Football", "high"),
    ("2025-10-09", "2025-10-11", "UT Football", "high"),
    ("2025-10-15", "2025-10-17", "UT Football (BIGGEST)", "high"),
    ("2025-10-23", "2025-10-25", "UT Football", "high"),
    ("2025-10-29", "2025-11-01", "UT Football", "high"),
    ("2025-11-05", "2025-11-08", "UT Football", "high"),
    ("2025-11-13", "2025-11-15", "UT Football", "high"),
    ("2025-11-19", "2025-11-22", "UT Football – Rivalry", "high"),
    ("2025-12-11", "2025-12-12", "Holiday Weekend", "high"),
    ("2025-12-18", "2025-12-19", "Holiday Weekend", "high"),
    ("2025-12-24", "2025-12-27", "Christmas 2025", "high"),
    ("2025-09-06", "2025-09-07", "Post-UT Football", "medium"),
    ("2025-09-11", "2025-09-13", "UT Football Weekend", "medium"),
    ("2025-11-26", "2025-11-28", "Thanksgiving 2025", "medium"),
]

# 2026 season — UT Football schedule TBD; holiday dates are confirmed
# Football weekends follow the same Sept–Nov Saturday pattern each year.
# Update exact game dates once the 2026 schedule is released.
SURGE_DATES_2026: list[tuple[str, str, str, str]] = [
    # HIGH — UT Football weekends (approximate — update when schedule is released)
    ("2026-09-03", "2026-09-05", "UT Football Opening Weekend", "high"),
    ("2026-09-17", "2026-09-19", "UT Football", "high"),
    ("2026-09-24", "2026-09-26", "UT Football", "high"),
    ("2026-10-01", "2026-10-03", "UT Football", "high"),
    ("2026-10-08", "2026-10-10", "UT Football", "high"),
    ("2026-10-15", "2026-10-17", "UT Football (BIGGEST)", "high"),
    ("2026-10-22", "2026-10-24", "UT Football", "high"),
    ("2026-10-29", "2026-10-31", "UT Football", "high"),
    ("2026-11-05", "2026-11-07", "UT Football", "high"),
    ("2026-11-12", "2026-11-14", "UT Football", "high"),
    ("2026-11-19", "2026-11-21", "UT Football – Rivalry", "high"),
    ("2026-12-10", "2026-12-12", "Holiday Weekend", "high"),
    ("2026-12-17", "2026-12-19", "Holiday Weekend", "high"),
    ("2026-12-24", "2026-12-27", "Christmas 2026", "high"),
    # HIGH — New Year
    ("2026-12-31", "2027-01-01", "New Year's Eve 2026", "high"),
    # MEDIUM
    ("2026-09-06", "2026-09-07", "Post-UT Football", "medium"),
    ("2026-09-10", "2026-09-12", "UT Football Weekend", "medium"),
    ("2026-11-26", "2026-11-28", "Thanksgiving 2026", "medium"),
]

SURGE_DATES = SURGE_DATES_2025 + SURGE_DATES_2026

# ---------------------------------------------------------------------------
# Thresholds by bedroom count
# ---------------------------------------------------------------------------

# {bedrooms: {"medium": min_price, "high": min_price}}
THRESHOLDS: dict[int, dict[str, int]] = {
    1: {"medium": 590,  "high": 755},
    2: {"medium": 740,  "high": 890},
    3: {"medium": 850,  "high": 960},
    4: {"medium": 920,  "high": 1225},
}

# 3BR and 4BR medium dates don't reliably hit their thresholds per the analysis
SKIP_MEDIUM: set[int] = {3, 4}


def _norm(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _is_kcity(prop: Any) -> bool:
    return "kcity" in _norm(getattr(prop, "name", "") or "")


def _beds(prop: Any) -> int:
    try:
        return int(getattr(prop, "bedrooms", 0) or 0)
    except (TypeError, ValueError):
        return 0


def generate_dso_tasks(portfolio: list[Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    kcity_props = [p for p in portfolio if getattr(p, "active", False) and _is_kcity(p)]

    actions: list[dict[str, Any]] = []
    skipped_past = 0
    skipped_medium_br = 0

    for start_str, end_str, event, demand in SURGE_DATES:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        if end < today:
            skipped_past += 1
            continue

        for prop in kcity_props:
            beds = _beds(prop)
            if beds not in THRESHOLDS:
                continue
            if demand == "medium" and beds in SKIP_MEDIUM:
                skipped_medium_br += 1
                continue

            min_price = THRESHOLDS[beds][demand]
            listing_id = str(getattr(prop, "listing_id", "") or "").strip()
            pms_name = str(getattr(prop, "pms_name", "") or "").strip()
            action_id = f"kcity_dso::{listing_id}::{start_str}::{end_str}::{demand}"

            actions.append({
                "id": action_id,
                "property": getattr(prop, "name", ""),
                "display_name": getattr(prop, "name", ""),
                "listing_id": listing_id,
                "pms_name": pms_name,
                "group": getattr(prop, "customization_group", "") or "",
                "subgroup": getattr(prop, "customization_sub_group", "") or "",
                "group_label": " / ".join(v for v in [
                    getattr(prop, "customization_group", "") or "",
                    getattr(prop, "customization_sub_group", "") or "",
                ] if v),
                "system": "PriceLabs DSO",
                "source": SOURCE,
                "type": f"kcity_dso_{demand}",
                "priority": "high" if demand == "high" else "medium",
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reviewed_at": None,
                "target_dates": f"{start_str} → {end_str}",
                "suggestion": f"Set {demand.upper()} DSO for {event} ({start_str} → {end_str})",
                "current_value": f"{beds}BR listing · no DSO set",
                "proposed_value": f"Min price ${min_price:,} · {start_str} → {end_str}",
                "adjustment": f"${min_price:,} floor · {demand} demand",
                "reason": (
                    f"{beds}BR KCity listing · {event} · "
                    f"demand={demand.upper()} · threshold=${min_price:,} "
                    f"(>{THRESHOLDS[beds]['medium']:,} med / >{THRESHOLDS[beds]['high']:,} high)"
                ),
                "implementation": (
                    f"In PriceLabs, open this listing's calendar, select {start_str} → {end_str}, "
                    f"and set a minimum price DSO of ${min_price:,}. "
                    f"Do not set a fixed price — allow the algorithm to go higher."
                ),
                "bedrooms": beds,
                "demand_level": demand,
                "event_label": event,
                "pricelabs_payload": {
                    "kind": "kcity_dso_min_price",
                    "start_date": start_str,
                    "end_date": end_str,
                    "min_price": min_price,
                },
            })

    # Sort: high demand first, then by date, then by bedroom count
    rank = {"high": 0, "medium": 1}
    actions.sort(key=lambda a: (rank.get(a["demand_level"], 9), a["target_dates"], a["bedrooms"]))

    future_surges = [s for s in SURGE_DATES if date.fromisoformat(s[1]) >= today]
    summary = {
        "total_listings": len(kcity_props),
        "total_tasks": len(actions),
        "high_tasks": sum(1 for a in actions if a["demand_level"] == "high"),
        "medium_tasks": sum(1 for a in actions if a["demand_level"] == "medium"),
        "surge_windows": len(future_surges),
        "skipped_past": skipped_past,
        "bedrooms_breakdown": {
            str(br): sum(1 for a in actions if a["bedrooms"] == br)
            for br in sorted(THRESHOLDS)
        },
    }
    return {"ok": True, "actions": actions, "summary": summary}
