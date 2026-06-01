#!/usr/bin/env python3
"""
HVR Smokies Dashboard.
Serves the HTML dashboard and streams xAI (Grok) analysis via Server-Sent Events.
"""

import html as _html_lib
import csv
import json
import os
import re
import sys
import shutil
import threading
import uuid

import requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, ".")
from dashboard_analysis import (
    AI_API_KEY_ENV_VARS,
    AI_MODEL,
    AI_MODEL_FALLBACKS,
    GROQ_FALLBACKS,  # legacy alias, retained for any external import
    _ai_client,
    _get_ai_api_key,
    _groq_client,    # legacy alias for _ai_client
)
from xai_client import APIStatusError as AIAPIStatusError, get_active_provider_info
from wheelhouse_portfolio import load_portfolio, portfolio_summary, Property
from marketing_links import lookup as lookup_links, get_links
from pricelabs_api import PriceLabsAPIError, client_from_env
from booking_api import BookingAPIError, build_promotion_xml, client_from_env as booking_client_from_env
from hostaway_api import HostawayAPIError, client_from_env as hostaway_client_from_env
from pricelabs_monthly_pacing import SOURCE as MONTHLY_PACING_SOURCE, load_monthly_pacing
from kcity_surge_dso import generate_dso_tasks, SOURCE as KCITY_DSO_SOURCE
import supabase_store

app = Flask(__name__)
TODAY = date.today()
EARLIEST_RATE_EDIT_DATE = TODAY + timedelta(days=1)

CSV_PATH        = Path(__file__).parent / "pricelabs_portfolio.csv"
MARKETING_PATH  = Path(__file__).parent / "marketing_links.csv"
ACTION_QUEUE_PATH = Path(__file__).parent / "pricelabs_weekly_action_queue.json"
BOOKING_PROMOTIONS_PATH = Path(__file__).parent / "booking_promotion_lab.json"
MONTHLY_PACING_PATH = Path(__file__).parent / "pricelabs_report_builder_monthly.csv"
PRICELABS_API_SNAPSHOT_PATH = Path(__file__).parent / "pricelabs_api_snapshot.json"
LISTING_QUALITY_RULES_PATH = Path(__file__).parent / "listing_quality_rules.md"
HOSTAWAY_ENRICHMENT_PATH = Path(__file__).parent / "hostaway_enrichment.json"
ACTION_REPEAT_COOLDOWN_DAYS = 14
_portfolio_lock = threading.Lock()


class PricingApplyError(RuntimeError):
    pass

# Load portfolio once at startup
_PORTFOLIO: list[Property] = load_portfolio()
_PORTFOLIO_INDEX: dict[str, Property] = {p.name: p for p in _PORTFOLIO}
_SUMMARY = portfolio_summary(_PORTFOLIO)


def _resolve_property(name: str) -> Property | None:
    if not name:
        return None
    if name in _PORTFOLIO_INDEX:
        return _PORTFOLIO_INDEX[name]
    clean = name.strip().lower()
    for prop in _PORTFOLIO:
        if prop.name.strip().lower() == clean or prop.property_name.strip().lower() == clean:
            return prop
    for prop in _PORTFOLIO:
        if clean and (clean in prop.name.strip().lower() or prop.name.strip().lower() in clean):
            return prop
    return None


def _load_actions_from_json() -> list[dict]:
    if not ACTION_QUEUE_PATH.exists():
        return []
    try:
        return json.loads(ACTION_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_actions_to_json(actions: list[dict]) -> None:
    try:
        ACTION_QUEUE_PATH.write_text(json.dumps(actions, indent=2), encoding="utf-8")
    except OSError:
        # Vercel filesystem is read-only outside /tmp; Supabase is the source of
        # truth when configured, so swallow this and rely on the table.
        pass


def _load_actions() -> list[dict]:
    if supabase_store.is_enabled():
        remote = supabase_store.load_pricing_actions()
        if remote is None:
            return _load_actions_from_json()
        if remote:
            return remote
        # Empty table on first read — backfill from the bundled JSON snapshot.
        seed = _load_actions_from_json()
        if seed and supabase_store.save_pricing_actions(seed):
            return seed
        return seed
    return _load_actions_from_json()


def _save_actions(actions: list[dict]) -> None:
    if supabase_store.is_enabled():
        if supabase_store.save_pricing_actions(actions):
            return
    _save_actions_to_json(actions)


def _load_booking_promotions_from_json() -> list[dict]:
    if not BOOKING_PROMOTIONS_PATH.exists():
        return []
    try:
        return json.loads(BOOKING_PROMOTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_booking_promotions_to_json(promotions: list[dict]) -> None:
    try:
        BOOKING_PROMOTIONS_PATH.write_text(json.dumps(promotions, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_booking_promotions() -> list[dict]:
    if supabase_store.is_enabled():
        remote = supabase_store.load_booking_promotions()
        if remote is None:
            return _load_booking_promotions_from_json()
        if remote:
            return remote
        seed = _load_booking_promotions_from_json()
        if seed and supabase_store.save_booking_promotions(seed):
            return seed
        return seed
    return _load_booking_promotions_from_json()


def _save_booking_promotions(promotions: list[dict]) -> None:
    if supabase_store.is_enabled():
        if supabase_store.save_booking_promotions(promotions):
            return
    _save_booking_promotions_to_json(promotions)


def _listing_quality_rules() -> str:
    try:
        return LISTING_QUALITY_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _money(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return f"${float(value):.0f}"


def _round_to_5(value: float) -> int:
    return max(0, int(round(value / 5) * 5))


def _pct_label(pct: float) -> str:
    return f"{pct:+.0%}"


def _pct_rate(base: float, pct: float) -> int:
    return _round_to_5(base * (1 + pct))


def _floor_limited_rate(prop: Property, pct: float, base: float | None = None) -> tuple[int, bool]:
    starting_rate = base if base is not None else prop.base_price
    proposed = _pct_rate(starting_rate, pct)
    if prop.min_price and prop.min_price < starting_rate and proposed <= prop.min_price:
        return _round_to_5(prop.min_price), True
    return proposed, False


def _range_label(start_offset: int, nights: int) -> str:
    start = TODAY + timedelta(days=start_offset)
    end = start + timedelta(days=max(1, nights) - 1)
    return f"{start.strftime('%b %-d')}–{end.strftime('%b %-d')}"


def _base_delta(prop: Property, pct: float, floor: int = 10, cap: int = 35) -> int:
    return _round_to_5(min(cap, max(floor, prop.base_price * pct)))


def _upcoming_rates(prop: Property, days: int = 45) -> list[tuple[date, float]]:
    end = TODAY + timedelta(days=days)
    out: list[tuple[date, float]] = []
    for day_text, rate in prop.calendar_rates:
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if EARLIEST_RATE_EDIT_DATE <= day <= end:
            out.append((day, rate))
    return out


def _stagnant_rate_window(prop: Property, min_nights: int = 4) -> dict | None:
    rates = _upcoming_rates(prop, 45)
    if not rates:
        return None
    best: list[tuple[date, float]] = []
    current: list[tuple[date, float]] = []
    for item in rates:
        if not current:
            current = [item]
            continue
        prev_day, prev_rate = current[-1]
        day, rate = item
        if rate == prev_rate and day == prev_day + timedelta(days=1):
            current.append(item)
        else:
            if len(current) > len(best):
                best = current
            current = [item]
    if len(current) > len(best):
        best = current
    if len(best) < min_nights:
        return None
    start, rate = best[0]
    end = best[-1][0]
    suggested_rate, hit_floor = _floor_limited_rate(prop, -0.03, rate)
    return {
        "start": start,
        "end": end,
        "nights": len(best),
        "rate": rate,
        "adjustment_pct": -0.03,
        "suggested_rate": suggested_rate,
        "hit_floor": hit_floor,
        "label": f"{start.strftime('%b %-d')}-{end.strftime('%b %-d')}",
    }


def _owner_note(prop: Property) -> str:
    return "; ".join(prop.owner_restrictions)


# ─────────────────────────────────────────────────────────────────────────────
# Listing Optimizer — rich HTML generator
# ─────────────────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return _html_lib.escape(str(v) if v is not None else "", quote=True)


def _lo_grade_color(grade: str) -> str:
    if not grade or grade == "N/A":
        return "text-slate-400"
    if grade.startswith("A"):
        return "text-green-700"
    if grade.startswith("B"):
        return "text-blue-700"
    if grade.startswith("C"):
        return "text-amber-600"
    return "text-red-600"


def _lo_analyze_title(title: str, char_limit: int = 50) -> dict:
    if not title or title in ("Not available", "Not synced", ""):
        return {"grade": "N/A", "issues": [], "passes": [], "char_count": 0, "mobile_preview": ""}
    issues = []
    passes = []
    char_count = len(title)
    title_lower = title.lower()
    if char_count > char_limit:
        issues.append(f"Title is {char_count}/{char_limit} chars — exceeds {char_limit}-char hard limit")
    else:
        passes.append(f"Character count: {char_count}/{char_limit} — within limit")
    if re.search(r"\bnew[!,\s]", title_lower) or title_lower.startswith("new "):
        issues.append('"New" in title is redundant per Airbnb guidelines — wastes characters')
    if "cozy" in title_lower:
        issues.append('"Cozy" is the most overused STR adjective — replace with a specific feature')
    if re.search(r"\bsleeps\s+\d+\b", title_lower):
        issues.append('"Sleeps X" is redundant — guest capacity is shown automatically in search results')
    IMPROPER_CAP_WORDS = {
        "new", "cozy", "beautiful", "stunning", "spacious", "charming", "modern",
        "luxurious", "easy", "perfect", "amazing", "great", "best", "private",
        "quiet", "comfortable", "peaceful", "relaxing", "sleeps", "with",
        "and", "the", "for", "near", "by", "in", "at", "on",
    }
    cap_violations = sum(
        1 for w in title.split()[1:]
        if w and w[0].isupper() and w.lower() in IMPROPER_CAP_WORDS
    )
    if cap_violations >= 2:
        issues.append("Title case violation — Airbnb requires sentence case: only first word and proper nouns capitalized")
    n = len(issues)
    grade = "A" if n == 0 and char_count <= 45 else "B+" if n == 0 else "B" if n == 1 else "C" if n == 2 else "D"
    return {"grade": grade, "issues": issues, "passes": passes, "char_count": char_count, "mobile_preview": title[:32]}


def _lo_photo_grade(count, existing_grade=None) -> str:
    if existing_grade and existing_grade not in ("unknown", "N/A", "", None):
        return str(existing_grade)
    if count is None:
        return "N/A"
    count = int(count)
    if count < 10:
        return "D"
    if count < 15:
        return "C"
    if count < 25:
        return "B"
    return "A"


def _lo_review_grade(rating, reviews=None) -> str:
    if rating is None:
        return "N/A"
    r = float(rating)
    if reviews is not None and int(reviews) < 3:
        return "C"
    if r >= 4.9:
        return "A"
    if r >= 4.8:
        return "A−"
    if r >= 4.7:
        return "B+"
    if r >= 4.5:
        return "B"
    if r >= 4.3:
        return "C"
    return "D"


def _lo_pricing_grade(prop) -> str:
    if prop.urgency == "overperforming":
        return "B+"
    if prop.urgency in ("ok", "onboarding"):
        return "B"
    if prop.adj_occ_60d < 0.10 and prop.booked_14d == 0:
        return "D"
    if prop.urgency == "warning":
        return "C"
    if prop.urgency == "critical":
        return "D"
    return "B"


def _lo_occ_grade(prop) -> str:
    occ = prop.adj_occ_60d
    if occ >= 0.75:
        return "A"
    if occ >= 0.50:
        return "B"
    if occ >= 0.30:
        return "C"
    return "D"


def _hostaway_ratings(prop: Property) -> dict:
    """Compute per-channel ratings and review counts from Hostaway reviews cache."""
    enrichment = _load_hostaway_enrichment()
    lid = str(prop.listing_id or "").strip()
    reviews = (enrichment.get("reviews") or {}).get(lid) or []
    by_channel: dict[str, list] = {}
    for r in reviews:
        ch = str(r.get("channel") or "").lower()
        rating = r.get("rating")
        if rating is not None:
            by_channel.setdefault(ch, []).append(float(rating))
    result = {}
    for ch, ratings in by_channel.items():
        result[ch] = {"rating": round(sum(ratings) / len(ratings), 2), "count": len(ratings)}
    return result


def _listing_optimizer_html(prop) -> str:  # noqa: C901
    ll = lookup_links(prop.name)
    benchmark = _benchmark_for(prop)
    ha_ratings = _hostaway_ratings(prop)

    # ── Raw data ───────────────────────────────────────────────────────────────
    airbnb_title   = (ll.airbnb_headline or "")  if ll else ""
    vrbo_title     = (ll.vrbo_headline   or "")  if ll else ""
    airbnb_photos  = ll.airbnb_photos            if ll else None
    vrbo_photos    = ll.vrbo_photos              if ll else None
    # Use marketing_links ratings first, fall back to Hostaway computed ratings
    airbnb_rating  = (ll.airbnb_rating if ll and ll.airbnb_rating is not None
                      else ha_ratings.get("airbnbofficial", ha_ratings.get("airbnb", {})).get("rating"))
    airbnb_reviews = (ll.airbnb_reviews if ll and ll.airbnb_reviews is not None
                      else ha_ratings.get("airbnbofficial", ha_ratings.get("airbnb", {})).get("count"))
    vrbo_rating    = (ll.vrbo_rating if ll and ll.vrbo_rating is not None
                      else ha_ratings.get("homeaway", ha_ratings.get("vrbo", {})).get("rating"))
    vrbo_reviews   = (ll.vrbo_reviews if ll and ll.vrbo_reviews is not None
                      else ha_ratings.get("homeaway", ha_ratings.get("vrbo", {})).get("count"))
    airbnb_url     = (ll.airbnb_url or "")       if ll else ""
    vrbo_url       = (ll.vrbo_url   or "")       if ll else ""
    booking_url    = (ll.booking_url or "")      if ll else ""
    vrbo_clean   = getattr(ll, "vrbo_cleanliness",   None) if ll else None
    vrbo_checkin = getattr(ll, "vrbo_checkin",       None) if ll else None
    vrbo_comm    = getattr(ll, "vrbo_communication", None) if ll else None
    vrbo_loc     = getattr(ll, "vrbo_location",      None) if ll else None

    ab_ta = _lo_analyze_title(airbnb_title, 50)
    vr_ta = _lo_analyze_title(vrbo_title, 70)

    title_grade   = ab_ta["grade"] if airbnb_title else (vr_ta["grade"] if vrbo_title else "N/A")
    photo_grade   = _lo_photo_grade(airbnb_photos, ll.airbnb_photo_grade if ll else None)
    vrbo_pg       = _lo_photo_grade(vrbo_photos, ll.vrbo_photo_grade if ll else None)
    review_grade  = _lo_review_grade(airbnb_rating, airbnb_reviews)
    if review_grade == "N/A" and vrbo_rating is not None:
        review_grade = _lo_review_grade(vrbo_rating, vrbo_reviews)
    pricing_grade = _lo_pricing_grade(prop)
    occ_grade     = _lo_occ_grade(prop)

    def _gn(g: str) -> float:
        if not g or g == "N/A": return 5.0
        if g.startswith("A"):   return 9.0
        if g.startswith("B"):   return 7.0
        if g.startswith("C"):   return 5.0
        return 3.0

    quality_score = round((_gn(title_grade) + _gn(photo_grade) + 7.0 + 7.0 + _gn(review_grade) + _gn(occ_grade)) / 6, 1)
    quality_letter = ("A" if quality_score >= 8.5 else "B+" if quality_score >= 7.5 else
                      "B" if quality_score >= 6.5 else "C+" if quality_score >= 5.5 else
                      "C" if quality_score >= 4.5 else "D")

    def _gc(g: str) -> str:
        if not g or g == "N/A": return "rgb(var(--muted-foreground))"
        if g.startswith("A"):   return "#16A34A"
        if g.startswith("B"):   return "#2563EB"
        if g.startswith("C"):   return "#D97706"
        return "#DC2626"

    # ── Status ─────────────────────────────────────────────────────────────────
    urgency_map = {
        "critical":       ("CRITICAL",   "red",   "Immediate action needed"),
        "warning":        ("WARNING",    "amber", "Fixable issues found"),
        "overperforming": ("STRONG",     "green", "Above benchmark pace"),
        "onboarding":     ("ONBOARDING", "amber", "New listing"),
    }
    status_val, status_cls, status_sub = urgency_map.get(prop.urgency, ("MODERATE", "amber", "On track"))

    # ── Issue list ─────────────────────────────────────────────────────────────
    issue_list = []
    if ab_ta["issues"]:               issue_list.append("Title")
    if airbnb_photos is not None and int(airbnb_photos) < 20: issue_list.append("Photos")
    if airbnb_rating is not None and float(airbnb_rating) < 4.7: issue_list.append("Reviews")
    if prop.urgency in ("critical", "warning"): issue_list.append("Pricing")
    issues_count = len(issue_list)
    issues_cls   = "red" if issues_count >= 3 else ("amber" if issues_count >= 1 else "green")

    primary_rating  = airbnb_rating  if airbnb_rating  is not None else vrbo_rating
    primary_reviews = airbnb_reviews if airbnb_rating  is not None else vrbo_reviews

    occ_gap   = benchmark.get("occ_gap", 0)
    bench_cls = "green" if occ_gap >= 0 else ""
    bench_parts: list[str] = []
    if airbnb_photos is not None and int(airbnb_photos) < 20: bench_parts.append("Photo count is primary drag")
    if title_grade.startswith(("C", "D")):                    bench_parts.append("title needs work")
    if primary_rating and float(primary_rating) >= 4.75:      bench_parts.append("Rating above average")
    bench_sub = " · ".join(bench_parts) or f"vs {benchmark.get('basis', 'portfolio benchmark')}"

    # ── Priority fixes ─────────────────────────────────────────────────────────
    pf_today: list[str] = []
    pf_week:  list[str] = []
    pf_month: list[str] = []
    if airbnb_photos is not None and int(airbnb_photos) < 10:
        pf_today.append(f"Add photos immediately — only {airbnb_photos} Airbnb photos. Target 20+. Shoot bedrooms, exterior, key amenities.")
    elif airbnb_photos is not None and int(airbnb_photos) < 20:
        pf_week.append(f"Expand Airbnb photo library from {airbnb_photos} → 20+ photos. Add bedrooms, amenities, and exterior shot.")
    if len(ab_ta["issues"]) >= 2:
        pf_today.append(f"Fix Airbnb title — {len(ab_ta['issues'])} violations: {'; '.join(ab_ta['issues'][:2])}")
    elif len(ab_ta["issues"]) == 1:
        pf_week.append(f"Fix Airbnb title: {ab_ta['issues'][0]}")
    if vrbo_title and len(vr_ta["issues"]) >= 1:
        pf_week.append(f"Fix VRBO title: {vr_ta['issues'][0]}")
    if airbnb_rating is not None and float(airbnb_rating) < 4.7:
        pf_week.append(f"Rating {float(airbnb_rating):.2f}★ — add pre-arrival message and 2-hour post-check-in follow-up")
    if prop.urgency == "critical" and prop.booked_14d == 0:
        pf_today.append("Critical status + 0 pickups — verify listing is live and amenity filters are complete")
    pf_month.append("Verify amenity filter completeness in Hostaway and all OTAs (parking, pool, kitchen, washer)")
    pf_month.append("Review description first 295 chars — lead with top USP (key amenities, location, capacity)")
    if prop.urgency in ("ok", "overperforming"):
        pf_month.append("Review pricing for upcoming high-demand dates and local events")

    top3 = ([(t, "r") for t in pf_today] + [(t, "a") for t in pf_week] + [(t, "b") for t in pf_month])[:3]

    # ── CSS (all rules scoped to .lo-wrap) ────────────────────────────────────
    css = (
        ".lo-wrap,.lo-wrap *{box-sizing:border-box}"
        ".lo-wrap{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:rgb(var(--foreground));font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}"
        ".lo-wrap .platform-row{display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}"
        ".lo-wrap .plat{display:inline-flex;align-items:center;gap:6px;border-radius:20px;padding:5px 12px;font-size:12px;font-weight:600;border:1.5px solid;text-decoration:none;cursor:pointer}"
        ".lo-wrap .plat:hover{opacity:.82}"
        ".lo-wrap .plat-airbnb{background:#FFF1F2;border-color:#FECDD3;color:#BE123C}"
        ".lo-wrap .plat-vrbo{background:#EFF6FF;border-color:#BFDBFE;color:#1D4ED8}"
        ".lo-wrap .plat-booking{background:#F0FDF4;border-color:#BBF7D0;color:#166534}"
        ".lo-wrap .plat-pms{background:#F5F3FF;border-color:#DDD6FE;color:#5B21B6}"
        ".lo-wrap .plat-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}"
        ".lo-wrap .plat-dim{opacity:.35;filter:grayscale(.7)}"
        ".lo-wrap .metric-row{display:flex;gap:0;background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;overflow:hidden;margin-bottom:16px}"
        ".lo-wrap .metric-card{flex:1;padding:14px 18px;border-right:1px solid rgb(var(--border));min-width:0}"
        ".lo-wrap .metric-card:last-child{border-right:none}"
        ".lo-wrap .metric-card.status-card{border-left:3px solid #EF4444}"
        ".lo-wrap .metric-card.issues-card{border-left:3px solid #EF4444}"
        ".lo-wrap .mc-label{font-size:11px;color:rgb(var(--muted-foreground));font-weight:500;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}"
        ".lo-wrap .mc-value{font-size:18px;font-weight:700;color:rgb(var(--foreground));line-height:1.2}"
        ".lo-wrap .mc-value.red{color:#EF4444}.lo-wrap .mc-value.green{color:#16A34A}.lo-wrap .mc-value.amber{color:#D97706}"
        ".lo-wrap .mc-sub{font-size:10px;color:rgb(var(--muted-foreground));margin-top:2px}"
        ".lo-wrap .benchmark-card{background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;padding:14px 18px;margin-bottom:20px;display:inline-block;min-width:220px}"
        ".lo-wrap .bc-label{font-size:11px;color:rgb(var(--muted-foreground));font-weight:500;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}"
        ".lo-wrap .bc-value{font-size:20px;font-weight:700;color:#EF4444;margin-bottom:2px}"
        ".lo-wrap .bc-value.green{color:#16A34A}"
        ".lo-wrap .bc-sub{font-size:11px;color:rgb(var(--muted-foreground))}"
        ".lo-wrap .main-card{background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;padding:24px 28px;margin-bottom:16px}"
        ".lo-wrap .main-card-title{font-size:16px;font-weight:700;color:rgb(var(--foreground));margin-bottom:4px}"
        ".lo-wrap .main-card-sub{font-size:12px;color:rgb(var(--muted-foreground));margin-bottom:18px}"
        ".lo-wrap .quality-banner{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px 16px;margin-bottom:20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}"
        ".lo-wrap .qb-score{font-size:26px;font-weight:800;color:#D97706}"
        ".lo-wrap .qb-label{font-size:12px;font-weight:600;color:rgb(var(--ink-alt))}"
        ".lo-wrap .qb-sub{font-size:11px;color:rgb(var(--muted-foreground));margin-top:1px}"
        ".lo-wrap .qb-grades{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}"
        ".lo-wrap .qb-grade{display:flex;flex-direction:column;align-items:center;background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:6px;padding:5px 10px;min-width:52px}"
        ".lo-wrap .qbg-label{font-size:9px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px}"
        ".lo-wrap .qbg-val{font-size:16px;font-weight:800;line-height:1}"
        ".lo-wrap .sec-title{font-size:14px;font-weight:700;color:rgb(var(--foreground));margin-bottom:2px}"
        ".lo-wrap .sec-sub{font-size:12px;color:rgb(var(--muted-foreground));margin-bottom:14px}"
        ".lo-wrap .fix-item{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid rgb(var(--muted))}"
        ".lo-wrap .fix-item:last-child{border-bottom:none}"
        ".lo-wrap .fix-num{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;margin-top:1px}"
        ".lo-wrap .fn-1{background:#FEE2E2;color:#991B1B}.lo-wrap .fn-2{background:#FEF3C7;color:#92400E}.lo-wrap .fn-3{background:#DBEAFE;color:#1E40AF}"
        ".lo-wrap .fix-text{font-size:13px;color:rgb(var(--ink-alt));line-height:1.55}"
        ".lo-wrap .fix-text strong{color:rgb(var(--foreground))}"
        ".lo-wrap .rule{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid rgb(var(--surface-alt));align-items:flex-start}"
        ".lo-wrap .rule:last-child{border-bottom:none}"
        ".lo-wrap .rdot{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;flex-shrink:0;margin-top:2px;font-weight:700}"
        ".lo-wrap .rd-r{background:#FEE2E2;color:#991B1B}.lo-wrap .rd-a{background:#FEF3C7;color:#92400E}.lo-wrap .rd-g{background:#F0FDF4;color:#166534}.lo-wrap .rd-b{background:#EFF6FF;color:#1E40AF}"
        ".lo-wrap .rtxt{font-size:13px;color:rgb(var(--ink-alt));line-height:1.6;flex:1}"
        ".lo-wrap .rtxt strong{color:rgb(var(--foreground));font-weight:600}"
        ".lo-wrap .title-box{font-family:SFMono-Regular,Consolas,monospace;border-radius:6px;padding:10px 14px;font-size:13px;margin-bottom:6px}"
        ".lo-wrap .tb-bad{background:#FFF5F5;border:1px solid #FECACA;color:#991B1B;font-weight:600}"
        ".lo-wrap .tb-good{background:#F0FDF4;border:1px solid #BBF7D0;color:#166534;font-weight:600}"
        ".lo-wrap .tb-neutral{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));color:rgb(var(--ink-alt))}"
        ".lo-wrap .mob-prev{background:#1E293B;border-radius:6px;padding:8px 12px;display:flex;align-items:center;gap:10px;margin:6px 0;font-family:SFMono-Regular,Consolas,monospace;font-size:12px}"
        ".lo-wrap .mp-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#475569;flex-shrink:0;min-width:52px}"
        ".lo-wrap .mp-v{color:#F1F5F9}.lo-wrap .mp-c{color:#334155}"
        ".lo-wrap .cbar-wrap{margin-bottom:6px}"
        ".lo-wrap .cbar{height:3px;background:rgb(var(--border));border-radius:2px;overflow:hidden}"
        ".lo-wrap .cf{height:3px;border-radius:2px;display:block}"
        ".lo-wrap .cf-g{background:#22C55E}.lo-wrap .cf-a{background:#F59E0B}.lo-wrap .cf-r{background:#EF4444}"
        ".lo-wrap .topt{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px 16px;margin-bottom:8px}"
        ".lo-wrap .topt.rec{border-color:#22C55E;background:#F0FDF4}"
        ".lo-wrap .to-lbl{font-size:10px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}"
        ".lo-wrap .to-val{font-family:SFMono-Regular,Consolas,monospace;font-size:14px;font-weight:700;color:rgb(var(--foreground));margin-bottom:6px}"
        ".lo-wrap .to-meta{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .photo-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}"
        ".lo-wrap .pc{border-radius:8px;padding:12px;border:1px solid rgb(var(--border))}"
        ".lo-wrap .pc-ok{background:rgb(var(--surface-alt))}.lo-wrap .pc-warn{background:#FFFBEB;border-color:#FDE68A}.lo-wrap .pc-bad{background:#FFF5F5;border-color:#FECACA}.lo-wrap .pc-miss{background:#F5F3FF;border-color:#DDD6FE}"
        ".lo-wrap .pc-num{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px}"
        ".lo-wrap .pc-grade{display:inline-flex;border-radius:20px;padding:2px 8px;font-size:11px;font-weight:700;margin-bottom:6px;border:1px solid}"
        ".lo-wrap .pcg-a{background:#F0FDF4;color:#166534;border-color:#BBF7D0}.lo-wrap .pcg-b{background:#EFF6FF;color:#1E40AF;border-color:#BFDBFE}.lo-wrap .pcg-c{background:#FFFBEB;color:#92400E;border-color:#FDE68A}.lo-wrap .pcg-d{background:#FEF2F2;color:#991B1B;border-color:#FECACA}.lo-wrap .pcg-m{background:#F5F3FF;color:#5B21B6;border-color:#DDD6FE}"
        ".lo-wrap .pc-txt{font-size:12px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .pc-txt strong{color:rgb(var(--foreground))}"
        ".lo-wrap .cov-wrap{margin-bottom:16px}"
        ".lo-wrap .cov-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:6px}"
        ".lo-wrap .cov-track{height:8px;background:rgb(var(--muted));border-radius:4px;overflow:hidden;display:flex;gap:2px}"
        ".lo-wrap .cov-leg{display:flex;gap:14px;margin-top:5px;flex-wrap:wrap}"
        ".lo-wrap .cov-li{display:flex;align-items:center;gap:5px;font-size:10px;color:rgb(var(--muted-foreground))}"
        ".lo-wrap .cov-dot{width:8px;height:8px;border-radius:2px}"
        ".lo-wrap .rv-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}"
        ".lo-wrap .rv-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:6px;padding:10px;text-align:center}"
        ".lo-wrap .rv-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:4px}"
        ".lo-wrap .rv-num{font-size:20px;font-weight:800;margin-bottom:4px}"
        ".lo-wrap .rv-bar{height:3px;background:rgb(var(--border));border-radius:2px;overflow:hidden;max-width:44px;margin:0 auto}"
        ".lo-wrap .rv-fill{height:3px;border-radius:2px}"
        ".lo-wrap .desc-lbl{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px}"
        ".lo-wrap .desc{font-family:SFMono-Regular,Consolas,monospace;font-size:12px;border-radius:6px;padding:12px 14px;line-height:1.8;margin-bottom:5px}"
        ".lo-wrap .desc-b{background:#FFF5F5;border:1px solid #FECACA;color:#991B1B}"
        ".lo-wrap .desc-g{background:#F0FDF4;border:1px solid #BBF7D0;color:#166534}"
        ".lo-wrap .char-ct{font-size:10px;color:rgb(var(--muted-foreground));text-align:right;margin-bottom:10px}"
        ".lo-wrap .atags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:12px}"
        ".lo-wrap .atag{padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid}"
        ".lo-wrap .at-g{background:#F0FDF4;color:#166534;border-color:#BBF7D0}.lo-wrap .at-b{background:#EFF6FF;color:#1E40AF;border-color:#BFDBFE}.lo-wrap .at-a{background:#FFFBEB;color:#92400E;border-color:#FDE68A}.lo-wrap .at-r{background:#FEF2F2;color:#991B1B;border-color:#FECACA}"
        ".lo-wrap .tier-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}"
        ".lo-wrap .tbl{width:100%;border-collapse:collapse;font-size:12px}"
        ".lo-wrap .tbl th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));font-weight:700;padding:7px 10px;background:rgb(var(--surface-alt));border-bottom:1px solid rgb(var(--border))}"
        ".lo-wrap .tbl td{padding:8px 10px;border-bottom:1px solid rgb(var(--muted));color:rgb(var(--ink-alt));vertical-align:middle}"
        ".lo-wrap .tbl tr:last-child td{border-bottom:none}"
        ".lo-wrap .tbl tr.this td{background:#F0FDF4;color:rgb(var(--foreground));font-weight:600}"
        ".lo-wrap .tbl tr.this td:first-child{border-left:3px solid #22C55E;padding-left:7px}"
        ".lo-wrap .better{color:#166534;font-weight:700}.lo-wrap .worse{color:#DC2626;font-weight:700}.lo-wrap .neut{color:rgb(var(--muted-foreground))}"
        ".lo-wrap .cl-sect{margin-bottom:16px}"
        ".lo-wrap .cl-hd{display:flex;align-items:center;gap:8px;margin-bottom:8px}"
        ".lo-wrap .cl-dot{width:10px;height:10px;border-radius:50%}"
        ".lo-wrap .cl-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}"
        ".lo-wrap .ci{display:flex;gap:10px;align-items:flex-start;padding:8px 12px;border-radius:6px;margin-bottom:4px;border:1px solid}"
        ".lo-wrap .ci-r{background:#FEF2F2;border-color:#FECACA}.lo-wrap .ci-a{background:#FFFBEB;border-color:#FDE68A}.lo-wrap .ci-g{background:#F0FDF4;border-color:#BBF7D0}"
        ".lo-wrap .checkbox{width:14px;height:14px;border-radius:3px;flex-shrink:0;margin-top:2px;border:1.5px solid}"
        ".lo-wrap .cb-r{border-color:#EF4444}.lo-wrap .cb-a{border-color:#F59E0B}.lo-wrap .cb-g{border-color:#22C55E}"
        ".lo-wrap .ci-txt{font-size:12px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .ci-txt strong{color:rgb(var(--foreground))}"
        ".lo-wrap .rev-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}"
        ".lo-wrap .rev-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px}"
        ".lo-wrap .rev-card.hl{background:#F0FDF4;border-color:#BBF7D0}"
        ".lo-wrap .rev-lbl{font-size:10px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}"
        ".lo-wrap .rev-val{font-size:20px;font-weight:800;margin-bottom:3px}"
        ".lo-wrap .rv-pos{color:#166534}"
        ".lo-wrap .rev-sub{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .roadmap{position:relative;padding-left:24px}"
        ".lo-wrap .roadmap::before{content:'';position:absolute;left:6px;top:4px;bottom:4px;width:2px;background:linear-gradient(180deg,#EF4444 0%,#F59E0B 40%,#3B82F6 100%);border-radius:2px}"
        ".lo-wrap .rm-item{position:relative;margin-bottom:16px}"
        ".lo-wrap .rm-item:last-child{margin-bottom:0}"
        ".lo-wrap .rm-dot{position:absolute;left:-20px;top:4px;width:10px;height:10px;border-radius:50%;border:2px solid rgb(var(--surface));box-shadow:0 0 0 2px currentColor}"
        ".lo-wrap .rmd-r{color:#EF4444;background:#EF4444}.lo-wrap .rmd-a{color:#F59E0B;background:#F59E0B}.lo-wrap .rmd-b{color:#3B82F6;background:#3B82F6}"
        ".lo-wrap .rm-ph{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}"
        ".lo-wrap .rmp-r{color:#DC2626}.lo-wrap .rmp-a{color:#D97706}.lo-wrap .rmp-b{color:#1D4ED8}"
        ".lo-wrap .rm-ul{list-style:none;padding:0;margin:0}"
        ".lo-wrap .rm-ul li{font-size:12px;color:rgb(var(--muted-foreground));padding:2px 0;display:flex;align-items:flex-start;gap:6px}"
        ".lo-wrap .rm-ul li::before{content:'\\2192';color:rgb(var(--muted-foreground));flex-shrink:0;font-size:11px;margin-top:1px}"
        ".lo-wrap .two-col{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}"
        ".lo-wrap .usp-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:10px}"
        ".lo-wrap .usp-name{font-size:12px;font-weight:700;margin-bottom:3px}"
        ".lo-wrap .usp-desc{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .seg-card{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px}"
        ".lo-wrap .seg-name{font-size:12px;font-weight:700;color:#1E40AF;margin-bottom:3px}"
        ".lo-wrap .seg-desc{font-size:11px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .gf-block{background:linear-gradient(135deg,#FFFBEB,#FEF9E7);border:1.5px solid #FDE68A;border-radius:8px;padding:14px 16px;margin-bottom:12px;display:flex;gap:12px;align-items:flex-start}"
        ".lo-wrap .gf-icon{font-size:24px;flex-shrink:0}"
        ".lo-wrap .gf-title{font-size:13px;font-weight:700;color:#92400E;margin-bottom:3px}"
        ".lo-wrap .gf-body{font-size:12px;color:rgb(var(--ink-alt));line-height:1.6}"
        ".lo-wrap .gf-body strong{color:rgb(var(--foreground))}"
        ".lo-wrap .consistency-note{background:#FFF5F5;border:1px solid #FECACA;border-radius:6px;padding:10px 14px;font-size:12px;color:#991B1B;margin-bottom:10px}"
        ".lo-wrap .divider{height:1px;background:rgb(var(--muted));margin:18px 0}"
        ".lo-wrap .note{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.6;margin-top:8px}"
        "@media(max-width:680px){.lo-wrap .metric-row{flex-wrap:wrap}.lo-wrap .metric-card{min-width:45%}.lo-wrap .photo-grid,.lo-wrap .two-col,.lo-wrap .rev-grid{grid-template-columns:1fr}.lo-wrap .rv-grid{grid-template-columns:1fr 1fr}.lo-wrap .qb-grades{margin-left:0;margin-top:10px}}"
    )

    # ── Platform badges ────────────────────────────────────────────────────────
    def _plat_badge(cls, dot_color, label, href):
        tag = "a" if href else "div"
        attrs = f' href="{_esc(href)}" target="_blank" rel="noopener"' if href else ""
        dim = "" if href else " plat-dim"
        return (f'<{tag} class="plat {cls}{dim}"{attrs}>'
                f'<div class="plat-dot" style="background:{dot_color}"></div>{_esc(label)}</{tag}>')

    pms_label = getattr(prop, "customization_group", "") or getattr(prop, "city", "") or "PMS"
    platform_row = (
        '<div class="platform-row">'
        + _plat_badge("plat-airbnb",  "#FF5A5F", "Airbnb",       airbnb_url)
        + _plat_badge("plat-vrbo",    "#1C5BD9", "VRBO",         vrbo_url)
        + _plat_badge("plat-booking", "#003580", "Booking.com",  booking_url)
        + _plat_badge("plat-pms",     "#7C3AED", _esc(pms_label), "")
        + '</div>'
    )

    # ── Metric cards ───────────────────────────────────────────────────────────
    rating_disp = (f"{float(primary_rating):.2f} ★" if primary_rating is not None else "N/A")
    rating_cls  = ("green" if primary_rating and float(primary_rating) >= 4.85
                   else "" if primary_rating and float(primary_rating) >= 4.7 else "amber")
    reviews_sub = f"{primary_reviews} reviews" if primary_reviews else "No reviews synced"
    photos_disp = (f"{airbnb_photos} / 20+" if airbnb_photos is not None else "N/A")
    photos_cls  = "red" if (airbnb_photos is None or int(airbnb_photos) < 10) else ("amber" if int(airbnb_photos) < 20 else "green")

    def _mc(label, value, sub, extra_cls=""):
        return (f'<div class="metric-card{" " + extra_cls if extra_cls else ""}">'
                f'<div class="mc-label">{label}</div>'
                f'<div class="mc-value {_esc(value[1])}">{_esc(value[0])}</div>'
                f'<div class="mc-sub">{_esc(sub)}</div></div>')

    metric_row = (
        '<div class="metric-row">'
        + _mc("Status",         (status_val, status_cls),   status_sub,     "status-card")
        + _mc("Overall Rating", (rating_disp, rating_cls),  reviews_sub)
        + _mc("5-Star Rate",    ("N/A", ""),                "Not synced — check host dashboard")
        + _mc("Guest Favorite", ("Active ✓" if (primary_rating and float(primary_rating) >= 4.8 and primary_reviews and int(primary_reviews) >= 5) else "Check", "green" if primary_rating and float(primary_rating) >= 4.8 else "amber"),
              "Review in Airbnb host dashboard")
        + _mc("Photos",         (photos_disp, photos_cls),  "Below benchmark" if airbnb_photos and int(airbnb_photos) < 20 else "Competitive count")
        + _mc("Issues",         (str(issues_count), issues_cls), " · ".join(issue_list[:4]) or "None flagged", "issues-card")
        + '</div>'
    )

    # ── Benchmark card ─────────────────────────────────────────────────────────
    benchmark_card = (
        f'<div class="benchmark-card">'
        f'<div class="bc-label">Vs {_esc(benchmark.get("basis", "portfolio"))} Benchmark</div>'
        f'<div class="bc-value {bench_cls}">{occ_gap:+.1f} pts occ</div>'
        f'<div class="bc-sub">{_esc(bench_sub)}</div>'
        f'</div>'
    )

    # ── Quality banner ─────────────────────────────────────────────────────────
    grades_html = "".join(
        f'<div class="qb-grade"><div class="qbg-label">{lbl}</div>'
        f'<div class="qbg-val" style="color:{_gc(g)}">{_esc(g)}</div></div>'
        for lbl, g in [("Title", title_grade), ("Images", photo_grade),
                       ("Reviews", review_grade), ("Pricing", pricing_grade),
                       ("Occ", occ_grade), ("Amenities", "B")]
    )
    quality_banner = (
        f'<div class="quality-banner">'
        f'<div><div class="qb-score">{quality_score} / 10</div>'
        f'<div class="qb-label">Evidence-Based Quality Score</div>'
        f'<div class="qb-sub">Grade {_esc(quality_letter)} · {issues_count} issue(s) flagged · AI analysis below</div></div>'
        f'<div class="qb-grades">{grades_html}</div>'
        f'</div>'
    )

    # ── Top 3 fixes ────────────────────────────────────────────────────────────
    fn_cls = ["fn-1", "fn-2", "fn-3"]
    fix_rows = "".join(
        f'<div class="fix-item"><div class="fix-num {fn_cls[i]}">{i+1}</div>'
        f'<div class="fix-text">{_esc(txt)}</div></div>'
        for i, (txt, _) in enumerate(top3)
    )
    top_fixes = (
        f'<div class="sec-title">Top Fixes</div>'
        f'<div class="sec-sub" style="margin-bottom:10px">Highest-impact actions across the entire listing</div>'
        + fix_rows
    )

    # ── Title optimization ─────────────────────────────────────────────────────
    if airbnb_title:
        tb_cls  = "tb-bad" if ab_ta["issues"] else "tb-good"
        issue_count_txt = f'{len(ab_ta["issues"])} violation{"s" if len(ab_ta["issues"]) != 1 else ""} found' if ab_ta["issues"] else "No violations found"
        mobile_current  = _esc(airbnb_title[:32])
        mobile_rest     = _esc(airbnb_title[32:]) if len(airbnb_title) > 32 else ""
        mobile_rest_html = f'<span class="mp-c">{mobile_rest}…</span>' if mobile_rest else ""
        char_pct = min(100, int(ab_ta["char_count"] / 50 * 100))
        bar_cls  = "cf-g" if ab_ta["char_count"] <= 40 else ("cf-a" if ab_ta["char_count"] <= 50 else "cf-r")
        rule_rows = ""
        for iss in ab_ta["issues"]:
            rule_rows += f'<div class="rule"><div class="rdot rd-r">✕</div><div class="rtxt"><strong>Issue:</strong> {_esc(iss)}</div></div>'
        for pas in ab_ta["passes"]:
            rule_rows += f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt">{_esc(pas)}</div></div>'
        if vrbo_title and vr_ta["issues"]:
            for iss in vr_ta["issues"]:
                rule_rows += f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>VRBO:</strong> {_esc(iss)}</div></div>'
        title_section = (
            f'<div class="sec-title">Title Optimization</div>'
            f'<div class="sec-sub">{_esc(issue_count_txt)} · Platform rules applied</div>'
            f'<div class="title-box {tb_cls}">"{_esc(airbnb_title)}" — {ab_ta["char_count"]} chars</div>'
            f'<div style="margin-bottom:14px">'
            f'<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px">Mobile truncation (32 chars visible)</div>'
            f'<div class="mob-prev"><span class="mp-lbl">Current</span><span class="mp-v">{mobile_current}</span>{mobile_rest_html}</div>'
            f'</div>'
            f'<div class="cbar-wrap"><div class="cbar"><div class="cf {bar_cls}" style="width:{char_pct}%"></div></div></div>'
            + rule_rows
            + f'<div style="margin-top:14px"><div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:8px">Recommended rewrites</div>'
            f'<div class="topt rec"><div class="to-lbl">★ Recommended — remove violations, lead with top USP</div>'
            f'<div class="to-val">{_esc(prop.property_name)}, {prop.bedrooms}BR — [your top amenity]</div>'
            f'<div class="cbar-wrap"><div class="cbar"><div class="cf cf-g" style="width:72%"></div></div></div>'
            f'<div class="to-meta">Sentence case ✓ · No redundant terms · Add your #1 Smokies amenity (hot tub, view, indoor pool, game room, theater, pet-friendly) before 32-char mobile cut</div></div>'
            f'<div class="topt"><div class="to-lbl">Option 2 — location-first</div>'
            f'<div class="to-val">{_esc(prop.property_name)}, {_esc(prop.area or "")}, {prop.bedrooms}BR</div>'
            f'<div class="to-meta">Area lead targets guests searching by location · Sentence case ✓</div></div>'
            f'</div>'
        )
    else:
        title_section = (
            f'<div class="sec-title">Title Optimization</div>'
            f'<div class="sec-sub">Title not synced</div>'
            f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt">Title not synced — check in Hostaway or the OTA host dashboard to run analysis.</div></div>'
        )

    # ── Image analysis ─────────────────────────────────────────────────────────
    def _photo_pcls(g: str) -> str:
        return {"A": "pcg-a", "B": "pcg-b", "C": "pcg-c", "D": "pcg-d"}.get(g[0] if g else "", "pcg-m")

    def _pc_block(platform, count, grade, card_cls):
        if count is None:
            return (f'<div class="pc pc-miss"><div class="pc-num">{_esc(platform)}</div>'
                    f'<span class="pc-grade pcg-m">Not synced</span>'
                    f'<div class="pc-txt">Photo count not in marketing data — update marketing_links.csv.</div></div>')
        cnt = int(count)
        note = (f"<strong>{cnt} photos</strong> — target 20+ minimum" if cnt < 20 else f"<strong>{cnt} photos</strong> — competitive count")
        return (f'<div class="pc {card_cls}"><div class="pc-num">{_esc(platform)}</div>'
                f'<span class="pc-grade {_photo_pcls(grade)}">{_esc(grade)}</span>'
                f'<div class="pc-txt">{note}</div></div>')

    ab_card_cls = "pc-bad" if photo_grade.startswith("D") else ("pc-warn" if photo_grade.startswith("C") else "pc-ok")
    vr_card_cls = "pc-bad" if vrbo_pg.startswith("D") else ("pc-warn" if vrbo_pg.startswith("C") else "pc-ok")
    total_photos = (airbnb_photos or 0)
    cov_pct = min(100, int(total_photos / 30 * 100))
    photo_section = (
        f'<div class="sec-title">Image Analysis</div>'
        f'<div class="sec-sub">{total_photos if airbnb_photos else "?"} Airbnb photos total</div>'
        f'<div class="cov-wrap">'
        f'<div class="cov-lbl">Photo count vs 30-photo benchmark</div>'
        f'<div class="cov-track"><div style="background:#3B82F6;width:{cov_pct}%;height:8px"></div>'
        f'<div style="background:rgb(var(--border));width:{100-cov_pct}%;height:8px"></div></div>'
        f'<div class="cov-leg">'
        f'<div class="cov-li"><div class="cov-dot" style="background:#3B82F6"></div>Current: {total_photos} photos ({cov_pct}%)</div>'
        f'<div class="cov-li"><div class="cov-dot" style="background:rgb(var(--border))"></div>Gap to 30-photo benchmark</div>'
        f'</div></div>'
        f'<div class="photo-grid">'
        + _pc_block("Airbnb", airbnb_photos, photo_grade, ab_card_cls)
        + _pc_block("VRBO",   vrbo_photos,   vrbo_pg,     vr_card_cls)
        + f'<div class="pc pc-miss"><div class="pc-num" style="color:#5B21B6">Manual audit — required</div>'
        f'<span class="pc-grade pcg-m">Checklist</span>'
        f'<div class="pc-txt">Cover photo sells the primary reason to book? · Hot tub/view/pool/game room documented? · Every bedroom shown individually? · Exterior shot included?</div></div>'
        f'<div class="pc pc-ok"><div class="pc-num">Photo order</div>'
        f'<span class="pc-grade pcg-b">Review</span>'
        f'<div class="pc-txt">Lead with the WOW shot. Secondary spaces (laundry, garage) should be last. Avoid 3 angles of the same room.</div></div>'
        f'</div>'
        f'<div class="rule"><div class="rdot rd-b">→</div><div class="rtxt"><strong>Fastest win:</strong> Delete any redundant angles of the same room and replace with a bedroom or amenity photo — zero cost, immediate CTR improvement.</div></div>'
    )

    # ── Description guidance ───────────────────────────────────────────────────
    desc_section = (
        f'<div class="sec-title">Description Optimization</div>'
        f'<div class="sec-sub">First 295 chars = only text visible before "show more" on mobile</div>'
        f'<div class="rule"><div class="rdot rd-r">✕</div><div class="rtxt"><strong>★★ and ✹ symbols in description body</strong> violate Airbnb content policy — may suppress search visibility. Replace with ALL CAPS plain-text headers.</div></div>'
        f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>Lead with your #1 Smokies experience amenity in the first sentence.</strong> Guests scan the first 50 words. Put the strongest USP (hot tub, mountain view, indoor pool, game room, theater, pet-friendly setup, fire pit, unique design) before the fold.</div></div>'
        f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt"><strong>Optimized 295-char preview template:</strong> "[Top amenity] at [property name] — [#2 USP]. [Bedrooms/beds] at [location context]. [#3 USP], [#4 USP], sleeps [capacity]."</div></div>'
        f'<div style="margin-top:12px">'
        f'<div class="desc-lbl">Description data not synced — review in Airbnb host dashboard</div>'
        f'<div class="note">Check: does your mobile preview lead with key amenities or start with generic phrases like "Welcome to" or "Discover comfort"? If yes, rewrite the first paragraph.</div>'
        f'</div>'
    )

    # ── Amenity audit ──────────────────────────────────────────────────────────
    beds_br = f"{prop.bedrooms}BR"
    amenity_section = (
        f'<div class="sec-title">Amenity Audit</div>'
        f'<div class="sec-sub">Review filter completeness in Airbnb host dashboard and Hostaway</div>'
        f'<div class="tier-lbl" style="color:#166534">Tier 1 — Smokies conversion drivers (verify when present)</div>'
        f'<div class="atags">'
        f'<span class="atag at-g">Hot Tub</span><span class="atag at-g">Mountain View</span>'
        f'<span class="atag at-g">Fast Wi-Fi</span><span class="atag at-g">Full Kitchen</span>'
        f'<span class="atag at-g">Free Parking</span><span class="atag at-g">Keyless Entry</span>'
        f'</div>'
        f'<div class="tier-lbl" style="color:#1E40AF">Tier 2 — Premium experience filters (verify enabled + photographed)</div>'
        f'<div class="atags">'
        f'<span class="atag at-b">Indoor Pool</span><span class="atag at-b">Game Room</span>'
        f'<span class="atag at-b">Theater Room</span><span class="atag at-b">Fire Pit</span>'
        f'<span class="atag at-b">Covered Deck + Grill</span><span class="atag at-b">Pet Friendly</span>'
        f'</div>'
        f'<div class="tier-lbl" style="color:#92400E">Premium gaps to investigate</div>'
        f'<div class="atags">'
        f'<span class="atag at-a">EV charger?</span><span class="atag at-a">Sauna / cold plunge?</span>'
        f'<span class="atag at-a">Bunk room / kid amenities?</span><span class="atag at-a">Coffee bar / standout design?</span>'
        f'<span class="atag at-r">Any high-impact amenity listed with zero photo evidence?</span>'
        f'</div>'
    )

    # ── Star rating analysis ───────────────────────────────────────────────────
    def _rv_card(label, val_str, color, pct):
        return (f'<div class="rv-card"><div class="rv-lbl">{_esc(label)}</div>'
                f'<div class="rv-num" style="color:{color}">{_esc(val_str)}</div>'
                f'<div class="rv-bar"><div class="rv-fill" style="width:{pct}%;background:{color}"></div></div>'
                f'</div>')

    rv_cards = ""
    if airbnb_rating is not None:
        r = float(airbnb_rating)
        rc = "#16A34A" if r >= 4.9 else ("#2563EB" if r >= 4.7 else "#D97706")
        rv_cards += _rv_card("Airbnb", f"{r:.2f}", rc, r / 5 * 100)
    if vrbo_rating is not None:
        vr = float(vrbo_rating)
        vc = "#16A34A" if vr >= 4.9 else ("#2563EB" if vr >= 4.7 else "#D97706")
        rv_cards += _rv_card("VRBO", f"{vr:.2f}", vc, vr / 5 * 100)
    for lbl, val in [("Cleanliness", vrbo_clean), ("Check-in", vrbo_checkin),
                     ("Comm.", vrbo_comm), ("Location", vrbo_loc)]:
        if val is not None:
            v = float(val)
            vc = "#16A34A" if v >= 4.9 else ("#2563EB" if v >= 4.7 else "#D97706")
            rv_cards += _rv_card(lbl, f"{v:.1f}", vc, v / 5 * 100)
    if not rv_cards:
        rv_cards = '<div class="rv-card" style="grid-column:span 3"><div class="rv-lbl">Ratings</div><div class="rv-num" style="color:rgb(var(--muted-foreground));font-size:14px">Not synced</div></div>'

    rating_section = (
        f'<div class="sec-title">Star Rating Analysis</div>'
        f'<div class="sec-sub">'
        + (f'{primary_reviews} reviews · {float(primary_rating):.2f} overall' if primary_rating else 'Not synced')
        + f'</div>'
        f'<div class="rv-grid">{rv_cards}</div>'
        f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>4-star gap is the primary lever.</strong> A pre-arrival message with local tips and a 2-hour post-check-in follow-up converts borderline stays into 5-star reviews.</div></div>'
        f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt">'
        + (f'<strong>Rating {float(primary_rating):.2f}★ signals rate headroom.</strong> A modest 8–12% increase on peak weekends is unlikely to move the value score negatively.' if primary_rating and float(primary_rating) >= 4.8 else '<strong>Focus on operational consistency</strong> — cleanliness and check-in are the most actionable levers for rating improvement.')
        + f'</div></div>'
    )

    # ── Guest Favorite ─────────────────────────────────────────────────────────
    gf_active = (primary_rating is not None and float(primary_rating) >= 4.8
                 and primary_reviews is not None and int(primary_reviews) >= 5)
    gf_section = (
        f'<div class="sec-title">Guest Favorite &amp; Review Status</div>'
        f'<div class="gf-block"><div class="gf-icon">★</div><div>'
        f'<div class="gf-title">{"Guest Favorites badge likely active" if gf_active else "Guest Favorites status — verify in host dashboard"}</div>'
        f'<div class="gf-body">'
        + (f'Rating {float(primary_rating):.2f}★ with {primary_reviews} reviews. <strong>Airbnb Guest Favorites badge appears in search results and improves CTR.</strong> At this rating, a single 3-star review could risk the badge — cleanliness and check-in are the most important operational levers.' if gf_active else 'Guest Favorites requires 4.8+ overall with sufficient reviews. Focus on consistent 5-star cleanliness and communication to reach the threshold.')
        + f'</div></div></div>'
    )

    # ── Consistency check ──────────────────────────────────────────────────────
    cons_note = ""
    if airbnb_photos is not None and int(airbnb_photos) < 20:
        cons_note = f'<div class="consistency-note">Only {airbnb_photos} photos — amenities listed in description or filters may not be visually verified for guests. Add photos for every key amenity you list.</div>'
    consistency_section = (
        f'<div class="sec-title">Consistency Check</div>'
        f'<div class="sec-sub">Description ↔ Amenities ↔ Photos</div>'
        + cons_note
        + f'<div style="overflow-x:auto"><table class="tbl">'
        f'<thead><tr><th>Claim</th><th>Description</th><th>Amenities</th><th>Photos</th><th>Status</th></tr></thead>'
        f'<tbody>'
        f'<tr class="this"><td>{prop.bedrooms}BR layout</td><td>—</td><td>✓</td><td>{"✓" if airbnb_photos and int(airbnb_photos) >= 20 else "✕ verify"}</td><td class="{"better" if airbnb_photos and int(airbnb_photos) >= 20 else "worse"}">{"Consistent" if airbnb_photos and int(airbnb_photos) >= 20 else "Add bedroom photos"}</td></tr>'
        f'<tr><td>Key amenities</td><td>—</td><td>✓</td><td>{"✓" if airbnb_photos and int(airbnb_photos) >= 15 else "✕ gaps likely"}</td><td class="neut" style="color:#D97706;font-weight:700">Verify each amenity has a photo</td></tr>'
        f'<tr><td>Title claims</td><td>—</td><td>—</td><td>—</td><td class="neut">Review manually in host dashboard</td></tr>'
        f'</tbody></table></div>'
    )

    # ── Positioning ────────────────────────────────────────────────────────────
    area = _esc(prop.area or "this area")
    positioning_section = (
        f'<div class="sec-title">Positioning &amp; Target Segments</div>'
        f'<div class="two-col" style="margin-top:10px">'
        f'<div class="usp-card"><div class="usp-name">Location — {area}</div><div class="usp-desc">Highlight proximity to top attractions, restaurants, or transport. Should appear in the first sentence of the description.</div></div>'
        f'<div class="usp-card"><div class="usp-name">{prop.bedrooms}BR layout</div><div class="usp-desc">Group-size signal. Should appear in the title and in the first 50 words of the description.</div></div>'
        f'<div class="usp-card"><div class="usp-name">Top Smokies experience amenity (verify)</div><div class="usp-desc">Your highest-converting amenity (hot tub, mountain view, indoor pool, game room, theater, fire pit, pet friendly, EV charger, sauna, unique design) should appear in the title, first sentence, and cover photo when present.</div></div>'
        f'<div class="usp-card"><div class="usp-name">Review strength</div><div class="usp-desc">'
        + (f'Rating {float(primary_rating):.2f}★ is a competitive advantage — mention "highly-rated" or "guest favorite" in your description.' if primary_rating and float(primary_rating) >= 4.8 else 'Build reviews through proactive guest communication before and after check-in.')
        + f'</div></div></div>'
        f'<div class="two-col">'
        f'<div class="seg-card"><div class="seg-name">Primary segment</div><div class="seg-desc">Identify and optimize for your highest-ADR guest type (couples, families, large groups, remote workers, pet owners, luxury travelers) — verify amenity filters match their searches.</div></div>'
        f'<div class="seg-card"><div class="seg-name">Seasonal premium (verify)</div><div class="seg-desc">Identify peak events in {area} (festivals, holidays, sports) and set custom pricing windows before competitors.</div></div>'
        f'</div>'
    )

    # ── Top performer comparison ───────────────────────────────────────────────
    bench_basis   = _esc(benchmark.get("basis", "portfolio"))
    bench_occ     = benchmark.get("occ_60d", 0)
    bench_price   = benchmark.get("base_price")
    bench_size    = benchmark.get("sample_size", 0)
    my_occ_pct    = f"{prop.adj_occ_60d:.0%}"
    bench_occ_pct = f"{bench_occ:.1f}%"
    occ_pos       = "better" if occ_gap >= 0 else "worse"
    price_pos     = ("better" if bench_price and prop.base_price > bench_price else
                     "worse"  if bench_price and prop.base_price < bench_price else "neut")
    comparison_section = (
        f'<div class="sec-title">Top Performer Comparison</div>'
        f'<div class="sec-sub">{bench_basis} benchmark ({bench_size} listings)</div>'
        f'<div style="overflow-x:auto;margin-top:10px"><table class="tbl">'
        f'<thead><tr><th>Signal</th><th>{_esc(prop.property_name)}</th><th>{bench_basis} Average</th><th>Position</th></tr></thead>'
        f'<tbody>'
        f'<tr class="this"><td>60-day occupancy</td><td>{my_occ_pct}</td><td>{bench_occ_pct}</td>'
        f'<td class="{occ_pos}">{"Above avg" if occ_gap >= 0 else "Below avg"}</td></tr>'
        f'<tr><td>Base price</td><td>${prop.base_price:.0f}</td>'
        f'<td>{"$" + str(int(bench_price)) if bench_price else "N/A"}</td>'
        f'<td class="{price_pos}">{"Above avg" if price_pos == "better" else "Below avg" if price_pos == "worse" else "—"}</td></tr>'
        f'<tr><td>Overall rating</td><td>{"★ " + str(float(primary_rating)) if primary_rating else "N/A"}</td>'
        f'<td>Benchmark N/A</td>'
        f'<td class="{"better" if primary_rating and float(primary_rating) >= 4.75 else "neut"}">{"Strong" if primary_rating and float(primary_rating) >= 4.75 else "Verify"}</td></tr>'
        f'<tr><td>Photo count</td><td>{airbnb_photos or "N/A"}</td><td>20–35 photos</td>'
        f'<td class="{"better" if airbnb_photos and int(airbnb_photos) >= 20 else "worse"}">{"Competitive" if airbnb_photos and int(airbnb_photos) >= 20 else "Critical gap"}</td></tr>'
        f'<tr><td>Title violations</td><td>{len(ab_ta["issues"])} violation{"s" if len(ab_ta["issues"]) != 1 else ""}</td><td>0–1 typical</td>'
        f'<td class="{"better" if not ab_ta["issues"] else "worse"}">{"Clean" if not ab_ta["issues"] else "Below avg"}</td></tr>'
        f'</tbody></table></div>'
    )

    # ── Pricing strategy ───────────────────────────────────────────────────────
    pricing_section = (
        f'<div class="sec-title">Pricing &amp; Promotion Strategy</div>'
        f'<div class="sec-sub">Urgency: {_esc(prop.urgency.upper())} · 60-day occ: {prop.adj_occ_60d:.0%} · Booked next 14d: {prop.booked_14d}</div>'
        f'<div class="rev-grid" style="margin-top:10px">'
        + (f'<div class="rev-card hl"><div class="rev-lbl">Peak / Event Windows</div><div class="rev-val rv-pos">+20–40%</div><div class="rev-sub">Identify top 3 demand spikes in {area} — set custom pricing 90 days ahead.</div></div>'
           if prop.urgency in ("ok", "overperforming") else
           f'<div class="rev-card"><div class="rev-lbl">Price Position</div><div class="rev-val" style="color:#EF4444">{_esc(prop.urgency.upper())}</div><div class="rev-sub">Check posted rate vs market booked price before discounting. Use date-level adjustments, not broad base cuts.</div></div>')
        + f'<div class="rev-card"><div class="rev-lbl">Weekly Discount (7+ nights)</div><div class="rev-val">10%</div><div class="rev-sub">Visible in Airbnb search — targets long-stay and multi-family segments.</div></div>'
        f'<div class="rev-card"><div class="rev-lbl">Early Bird (60–90 days out)</div><div class="rev-val">10–15%</div><div class="rev-sub">Lock in peak-week bookings before competitors fill those dates.</div></div>'
        f'<div class="rev-card"><div class="rev-lbl">Shoulder Season</div><div class="rev-val">15–20%</div><div class="rev-sub">3+ consecutive mid-week nights at a custom rate fills gaps without discounting weekends.</div></div>'
        f'</div>'
    )

    # ── Ranked checklist ───────────────────────────────────────────────────────
    def _ci(txt, tier):
        cls_map = {"r": ("ci-r", "cb-r"), "a": ("ci-a", "cb-a"), "g": ("ci-g", "cb-g")}
        ci_c, cb_c = cls_map.get(tier, ("ci-g", "cb-g"))
        return (f'<div class="ci {ci_c}"><div class="checkbox {cb_c}"></div>'
                f'<div class="ci-txt">{txt}</div></div>')

    checklist_section = (
        f'<div class="sec-title">Ranked Fix Checklist</div>'
        f'<div style="margin-top:12px">'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#EF4444"></div>'
        f'<div class="cl-title" style="color:#991B1B">Critical — Do Today</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "r") for t in pf_today[:4])
        + (f'<div class="ci ci-r"><div class="checkbox cb-r"></div><div class="ci-txt"><strong>Verify listing is active on all channels</strong> — check Hostaway channel manager sync status.</div></div>' if not pf_today else "")
        + f'</div>'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#F59E0B"></div>'
        f'<div class="cl-title" style="color:#92400E">High Priority — This Week</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "a") for t in pf_week[:5])
        + f'<div class="ci ci-a"><div class="checkbox cb-a"></div><div class="ci-txt"><strong>Rewrite description 295-char preview</strong> — replace generic opener with king-beds, top amenity, and location lead.</div></div>'
        + f'</div>'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#22C55E"></div>'
        f'<div class="cl-title" style="color:#166534">Optimization — This Month</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "g") for t in pf_month[:4])
        + f'</div></div>'
    )

    # ── 90-day roadmap ─────────────────────────────────────────────────────────
    rm_w1 = (["Fix Airbnb title — remove all violations"] if ab_ta["issues"] else []) + \
            (["Add missing photos — start with bedrooms, then key amenities"] if airbnb_photos and int(airbnb_photos) < 20 else []) + \
            ["Rewrite description 295-char preview with top USP lead",
             "Verify amenity filters match all listed features in Hostaway"]
    rm_w2 = ["Complete photo library to 20+ images (bedrooms, amenities, exterior)",
             "Remove ★★ and special symbols from description body",
             "Set weekly discount (10%) and early bird (10–15%) in pricing tool",
             "Add building exterior and key amenity photos to gallery"]
    rm_m2 = ["Implement pre-arrival message template with local tips",
             "Add 2-hour post-check-in follow-up message",
             "Identify peak event windows and set custom pricing",
             "Target 4.85+ overall to lock in Guest Favorites badge"]

    def _rm_li(items):
        return "".join(f"<li>{_esc(i)}</li>" for i in items)

    roadmap_section = (
        f'<div class="sec-title">90-Day Action Roadmap</div>'
        f'<div style="margin-top:14px"><div class="roadmap">'
        f'<div class="rm-item"><div class="rm-dot rmd-r"></div>'
        f'<div class="rm-ph rmp-r">Week 1 — Quick wins (no-cost, immediate impact)</div>'
        f'<ul class="rm-ul">{_rm_li(rm_w1)}</ul></div>'
        f'<div class="rm-item"><div class="rm-dot rmd-a"></div>'
        f'<div class="rm-ph rmp-a">Weeks 2–4 — Photo build-out &amp; listing polish</div>'
        f'<ul class="rm-ul">{_rm_li(rm_w2)}</ul></div>'
        f'<div class="rm-item"><div class="rm-dot rmd-b"></div>'
        f'<div class="rm-ph rmp-b">Month 2–3 — Guest experience &amp; revenue optimization</div>'
        f'<ul class="rm-ul">{_rm_li(rm_m2)}</ul></div>'
        f'</div></div>'
        f'<div style="margin-top:16px;padding:12px 14px;background:rgb(var(--surface-alt));border-radius:6px;border:1px solid rgb(var(--border));font-size:11px;color:rgb(var(--muted-foreground));line-height:1.6">'
        f'<strong style="color:rgb(var(--ink-alt))">Protecting the review score while building the photo set to 20+ images are the two highest-impact moves over the next 60 days.</strong> '
        f'Base price changes should only follow after listing quality improvements are in place.'
        f'</div>'
    )

    # ── Assemble ───────────────────────────────────────────────────────────────
    divider = '<div class="divider"></div>'
    return (
        f'<style>{css}</style>'
        f'<div class="lo-wrap">'
        + platform_row
        + metric_row
        + benchmark_card
        + f'<div class="main-card">'
        + f'<div class="main-card-title">Listing Optimizer</div>'
        + f'<div class="main-card-sub">{_esc(prop.property_name)} · {_esc(prop.area or "")} · {prop.bedrooms}BR · {_esc(prop.urgency.upper())} status</div>'
        + quality_banner
        + top_fixes
        + divider
        + title_section
        + divider
        + photo_section
        + divider
        + desc_section
        + divider
        + amenity_section
        + divider
        + rating_section
        + divider
        + gf_section
        + divider
        + consistency_section
        + divider
        + positioning_section
        + divider
        + comparison_section
        + divider
        + pricing_section
        + divider
        + checklist_section
        + divider
        + roadmap_section
        + divider
        + f'<div class="sec-title">AI Deep Analysis</div>'
        + f'<div class="sec-sub">Streaming analysis based on your full property context</div>'
        + f'<div id="lo-ai-body" class="note" style="font-size:13px;color:rgb(var(--muted-foreground))">'
        + f'<p style="color:rgb(var(--muted-foreground));font-style:italic">Loading AI analysis…</p>'
        + f'</div>'
        + f'</div>'  # /main-card
        + f'</div>'  # /lo-wrap
    )


def _group_label(prop: Property) -> str:
    parts = [
        getattr(prop, "customization_group", ""),
        getattr(prop, "customization_sub_group", ""),
    ]
    return " / ".join(part for part in parts if part) or getattr(prop, "city", "") or "Ungrouped"


def _action_context(prop: Property) -> dict:
    return {
        "property": prop.name,
        "listing_id": getattr(prop, "listing_id", ""),
        "pms_name": getattr(prop, "pms_name", ""),
        "group": getattr(prop, "customization_group", ""),
        "subgroup": getattr(prop, "customization_sub_group", ""),
        "city": getattr(prop, "city", ""),
        "group_label": _group_label(prop),
        "system": "PriceLabs",
    }


def _action_dedupe_key(action: dict) -> tuple:
    payload = action.get("pricelabs_payload") or {}
    return (
        action.get("property"),
        action.get("type"),
        action.get("target_dates"),
        payload.get("kind"),
        payload.get("start_date"),
        payload.get("end_date"),
        payload.get("adjustment_pct"),
        payload.get("suggested_rate"),
        payload.get("suggested_base_price"),
        payload.get("estimated_base_price"),
        payload.get("estimated_rate"),
    )


def _is_pace_year_action(action: dict) -> bool:
    return (
        action.get("source") == "Pace 2025 MauiP 05.17.26.xlsm"
        and str(action.get("type", "")).startswith("pace_year_")
    )


def _is_monthly_pacing_action(action: dict) -> bool:
    return action.get("source") == MONTHLY_PACING_SOURCE or str(action.get("type", "")).startswith("monthly_")


def _recently_reviewed(action: dict, now: datetime) -> bool:
    when = action.get("reviewed_at") or action.get("applied_at") or action.get("created_at")
    if not when:
        return False
    try:
        reviewed = datetime.fromisoformat(str(when))
    except ValueError:
        return False
    if reviewed.tzinfo is None:
        reviewed = reviewed.replace(tzinfo=timezone.utc)
    return now - reviewed <= timedelta(days=ACTION_REPEAT_COOLDOWN_DAYS)


def _suggest_action(prop: Property) -> dict | None:
    benchmark = _benchmark_for(prop)
    occ_gap = benchmark.get("occ_gap_pct", 0)
    owner_note = _owner_note(prop)
    stagnant = _stagnant_rate_window(prop)
    occ = prop.adj_occ_60d

    if occ >= 0.70 and prop.booked_14d == 0:
        return {
            **_action_context(prop),
            "type": "hold_rate_check_restrictions",
            "priority": "medium",
            "suggestion": "Hold pricing; check restrictions and orphan gaps",
            "adjustment": "No price decrease",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
            "proposed_value": "Hold base; audit min-stay, gaps, and blocked dates",
            "owner_note": owner_note,
            "reason": (
                f"60-day occupancy is healthy at {occ:.0%}. Zero recent pickup is not enough reason to discount; "
                "protect ADR and check whether remaining open nights are constrained."
            ),
            "implementation": "Do not push pricing. Review remaining open calendar dates, minimum stays, orphan gaps, and channel availability in PriceLabs/Hostaway.",
            "pricelabs_payload": {"kind": "manual_review"},
        }

    if 0.55 <= occ < 0.70 and prop.booked_14d == 0:
        return {
            **_action_context(prop),
            "type": "hold_rate_monitor_pickup",
            "priority": "medium",
            "suggestion": "Hold base price; monitor pickup before discounting",
            "adjustment": "No price decrease",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
            "proposed_value": "Hold base; consider date-specific action only if open high-value dates remain",
            "owner_note": owner_note,
            "reason": (
                f"60-day occupancy is near target at {occ:.0%}. With no pickup, investigate open-date quality first; "
                "do not reduce the base rate across the listing."
            ),
            "implementation": "Review only the unbooked high-value dates. If they are clean, use a narrow date-level percentage adjustment, not a base decrease.",
            "pricelabs_payload": {"kind": "manual_review"},
        }

    if prop.urgency == "overperforming":
        pct = 0.05
        new_base = _pct_rate(prop.base_price, pct)
        return {
            **_action_context(prop),
            "type": "fast_booking_price_increase",
            "priority": "high" if prop.adj_occ_60d >= 0.90 or prop.booked_14d >= 14 else "medium",
            "suggestion": f"Review base price increase of {_pct_label(pct)}",
            "adjustment": f"{_pct_label(pct)} base review",
            "target_dates": "Next 30 days",
            "current_value": f"Base {_money(prop.base_price)}" + (f"; floor {_money(prop.min_price)}" if prop.min_price else ""),
            "proposed_value": f"Base {_pct_label(pct)} (est. {_money(new_base)})",
            "owner_note": owner_note,
            "reason": (
                f"Fast booking signal: {prop.booked_14d} booked nights in 14 days; "
                f"60-day occupancy is {prop.adj_occ_60d:.0%}."
            ),
            "implementation": "Approve first, then update PriceLabs using a percentage-style base review. Do not change max price.",
            "pricelabs_payload": {"kind": "base_percentage_review", "adjustment_pct": pct, "estimated_base_price": new_base},
        }

    if stagnant and prop.urgency in {"critical", "warning"} and occ < 0.55:
        pct = stagnant["adjustment_pct"]
        hit_floor = stagnant["hit_floor"]
        return {
            **_action_context(prop),
            "type": "stagnant_rate_nudge",
            "priority": "high" if prop.urgency == "critical" else "medium",
            "suggestion": (
                f"Review floor-limited rate {_money(stagnant['suggested_rate'])} for {stagnant['label']}"
                if hit_floor
                else f"Review {_pct_label(pct)} adjustment for stagnant dates {stagnant['label']}"
            ),
            "adjustment": f"floor-limited to {_money(stagnant['suggested_rate'])}" if hit_floor else f"{_pct_label(pct)} date adjustment",
            "target_dates": stagnant["label"],
            "current_value": f"{stagnant['nights']} straight nights at {_money(stagnant['rate'])}",
            "proposed_value": (
                f"Minimum floor {_money(stagnant['suggested_rate'])}"
                if hit_floor
                else f"{_pct_label(pct)} date adjustment (est. {_money(stagnant['suggested_rate'])})"
            ),
            "owner_note": owner_note,
            "reason": (
                f"Listing is {prop.urgency}; pickup is {prop.booked_14d} nights in 14 days. "
                "Flat rates can look stale, so use a small percentage review instead of a broad promotion."
            ),
            "implementation": (
                "Approve first, then use a percentage adjustment unless the minimum floor is reached. "
                + ("Owner restriction noted: avoid promo language/discount campaigns." if prop.no_promotions else "Do not apply automatically.")
            ),
            "pricelabs_payload": {
                "kind": "custom_fixed_rate" if hit_floor else "custom_percentage_adjustment",
                "start_date": stagnant["start"].isoformat(),
                "end_date": stagnant["end"].isoformat(),
                "adjustment_pct": pct,
                "suggested_rate": stagnant["suggested_rate"] if hit_floor else None,
                "estimated_rate": stagnant["suggested_rate"],
            },
        }

    if prop.booked_14d == 0 and occ <= 0.15:
        if prop.no_promotions:
            pct = -0.03
            new_base, hit_floor = _floor_limited_rate(prop, pct)
            return {
                **_action_context(prop),
                "type": "base_price_small_nudge_no_promo",
                "priority": "high" if prop.urgency == "critical" else "medium",
                "suggestion": (
                    f"Review base price at minimum floor {_money(new_base)}"
                    if hit_floor
                    else f"Review base price decrease of {_pct_label(pct)}"
                ),
                "adjustment": f"floor-limited to {_money(new_base)}" if hit_floor else f"{_pct_label(pct)} base review",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {_money(prop.base_price)}",
                "proposed_value": f"Minimum floor {_money(new_base)}" if hit_floor else f"Base {_pct_label(pct)} (est. {_money(new_base)})",
                "owner_note": owner_note,
                "reason": "No promo owner note detected; use a small percentage review instead of discount or promotional campaign.",
                "implementation": "Approve first, then update PriceLabs base price only if owner restrictions allow.",
                "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
            }
        pct = -0.10 if prop.base_price < 250 else -0.12
        target_rate, hit_floor = _floor_limited_rate(prop, pct)
        return {
            **_action_context(prop),
            "type": "open_gap_fixed_rate_discount",
            "priority": "high" if prop.urgency == "critical" else "medium",
            "suggestion": (
                f"Review floor-limited rate {_money(target_rate)} for {_range_label(19, 5)}"
                if hit_floor
                else f"Review {_pct_label(pct)} adjustment for {_range_label(19, 5)}"
            ),
            "adjustment": f"floor-limited to {_money(target_rate)}" if hit_floor else f"{_pct_label(pct)} from base",
            "target_dates": _range_label(19, 5),
            "current_value": f"Base {_money(prop.base_price)}; 14-day pickup {prop.booked_14d}",
            "proposed_value": f"Minimum floor {_money(target_rate)}" if hit_floor else f"{_pct_label(pct)} date adjustment (est. {_money(target_rate)})",
            "owner_note": owner_note,
            "reason": (
                f"No booked nights in the next 14 days and 60-day occupancy is {prop.adj_occ_60d:.0%}. "
                f"Benchmark gap: {occ_gap:+.0f} pts vs {benchmark.get('basis', 'benchmark')}."
            ),
            "implementation": "Approve first, then use a PriceLabs percentage adjustment unless the minimum floor is reached.",
            "pricelabs_payload": {
                "kind": "custom_fixed_rate" if hit_floor else "custom_percentage_adjustment",
                "start_date": (TODAY + timedelta(days=19)).isoformat(),
                "end_date": (TODAY + timedelta(days=23)).isoformat(),
                "adjustment_pct": pct,
                "suggested_rate": target_rate if hit_floor else None,
                "estimated_rate": target_rate,
            },
        }

    if prop.booked_14d == 0:
        if occ >= 0.35:
            return {
                **_action_context(prop),
                "type": "hold_rate_monitor_pickup",
                "priority": "medium",
                "suggestion": "Hold base price; watch pickup and open-date quality",
                "adjustment": "No price decrease",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                "proposed_value": "Hold base; review only weak date pockets",
                "owner_note": owner_note,
                "reason": (
                    f"Occupancy is not low enough for a base decrease ({occ:.0%}). "
                    "A base-rate cut would discount already-booked demand and weaken ADR."
                ),
                "implementation": "Check open nights and market compression first. Use date-specific adjustments only where the calendar is exposed.",
                "pricelabs_payload": {"kind": "manual_review"},
            }
        pct = -0.03
        new_base, hit_floor = _floor_limited_rate(prop, pct)
        return {
            **_action_context(prop),
            "type": "base_price_reduction",
            "priority": "high" if prop.urgency == "critical" else "medium",
            "suggestion": (
                f"Review base price at minimum floor {_money(new_base)}"
                if hit_floor
                else f"Review base price decrease of {_pct_label(pct)}"
            ),
            "adjustment": f"floor-limited to {_money(new_base)}" if hit_floor else f"{_pct_label(pct)} base review",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}",
            "proposed_value": f"Minimum floor {_money(new_base)}" if hit_floor else f"Base {_pct_label(pct)} (est. {_money(new_base)})",
            "owner_note": owner_note,
            "reason": f"0 booked nights in the next 14 days; 60-day occupancy is {prop.adj_occ_60d:.0%}.",
            "implementation": "Approve first, then update PriceLabs base price. Recheck after 7 days. Do not increase warning listings.",
            "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
        }

    if prop.adj_occ_60d < 0.25:
        if occ <= 0.20:
            pct = -0.05
            new_base, hit_floor = _floor_limited_rate(prop, pct)
            return {
                **_action_context(prop),
                "type": "low_occupancy_base_adjustment",
                "priority": "high" if prop.urgency == "critical" else "medium",
                "suggestion": (
                    f"Review base price at minimum floor {_money(new_base)}"
                    if hit_floor
                    else f"Review base price decrease of {_pct_label(pct)}"
                ),
                "adjustment": f"floor-limited to {_money(new_base)}" if hit_floor else f"{_pct_label(pct)} base review",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                "proposed_value": f"Minimum floor {_money(new_base)}" if hit_floor else f"Base {_pct_label(pct)} (est. {_money(new_base)})",
                "owner_note": owner_note,
                "reason": (
                    f"60-day occupancy is weak at {occ:.0%}. Pickup of {prop.booked_14d} nights is not enough "
                    "to offset the pacing gap, so the base needs downward pressure."
                ),
                "implementation": "Approve first. If approved, reduce base price in PriceLabs and recheck pickup/ADR after 7 days.",
                "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
            }

        if prop.no_promotions:
            pct = -0.03
            new_base, hit_floor = _floor_limited_rate(prop, pct)
            return {
                **_action_context(prop),
                "type": "soft_pickup_base_nudge_no_promo",
                "priority": "medium",
                "suggestion": (
                    f"Review base price at minimum floor {_money(new_base)}"
                    if hit_floor
                    else f"Review base price decrease of {_pct_label(pct)}"
                ),
                "adjustment": f"floor-limited to {_money(new_base)}" if hit_floor else f"{_pct_label(pct)} base review",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {prop.adj_occ_60d:.0%}",
                "proposed_value": f"Minimum floor {_money(new_base)}" if hit_floor else f"Base {_pct_label(pct)} (est. {_money(new_base)})",
                "owner_note": owner_note,
                "reason": "Soft occupancy, but owner note says no promotions; avoid discount language and use a small percentage review.",
                "implementation": "Approve first, then update PriceLabs base price only if allowed.",
                "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
            }
        pct = -0.08
        target_rate, hit_floor = _floor_limited_rate(prop, pct)
        return {
            **_action_context(prop),
            "type": "short_window_discount",
            "priority": "medium",
            "suggestion": (
                f"Review floor-limited rate {_money(target_rate)} for {_range_label(19, 5)}"
                if hit_floor
                else f"Review {_pct_label(pct)} adjustment for {_range_label(19, 5)}"
            ),
            "adjustment": f"floor-limited to {_money(target_rate)}" if hit_floor else f"{_pct_label(pct)} for target dates",
            "target_dates": _range_label(19, 5),
            "current_value": f"Base {_money(prop.base_price)}; 60-day occupancy {prop.adj_occ_60d:.0%}",
            "proposed_value": f"Minimum floor {_money(target_rate)}" if hit_floor else f"{_pct_label(pct)} date adjustment (est. {_money(target_rate)})",
            "owner_note": owner_note,
            "reason": f"60-day adjusted occupancy is {prop.adj_occ_60d:.0%}; pickup is soft.",
            "implementation": "Approve first, then use a PriceLabs percentage adjustment unless the minimum floor is reached.",
            "pricelabs_payload": {
                "kind": "custom_fixed_rate" if hit_floor else "custom_percentage_adjustment",
                "start_date": (TODAY + timedelta(days=19)).isoformat(),
                "end_date": (TODAY + timedelta(days=23)).isoformat(),
                "adjustment_pct": pct,
                "suggested_rate": target_rate if hit_floor else None,
                "estimated_rate": target_rate,
            },
        }

    return None


def _generate_weekly_actions() -> list[dict]:
    existing = _load_actions()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    protected: list[dict] = []
    protected_keys = set()
    for action in existing:
        if _is_pace_year_action(action):
            continue
        status = action.get("status", "pending")
        key = _action_dedupe_key(action)
        if status in {"approved", "applied"}:
            protected.append(action)
            protected_keys.add(key)
        elif status in {"rejected"} and _recently_reviewed(action, now_dt):
            protected.append(action)
            protected_keys.add(key)

    created_by_key: dict[tuple, dict] = {}
    underperforming = [
        p for p in _PORTFOLIO
        if p.active and p.urgency in {"critical", "warning"} and not p.onboarding
    ][:80]
    overperforming = [
        p for p in _PORTFOLIO
        if p.active and p.urgency == "overperforming"
    ][:60]
    candidates = underperforming + overperforming
    for prop in candidates:
        action = _suggest_action(prop)
        if not action:
            continue
        key = _action_dedupe_key(action)
        if key in protected_keys:
            continue
        action.update({
            "id": str(uuid.uuid4()),
            "status": "pending",
            "created_at": now,
            "reviewed_at": None,
        })
        created_by_key[key] = action
    actions = protected + list(created_by_key.values())
    _save_actions(actions)
    return actions


def _generate_monthly_pacing_actions() -> tuple[list[dict], dict]:
    result = load_monthly_pacing(MONTHLY_PACING_PATH, _PORTFOLIO, TODAY)
    if not result.get("ok"):
        return [], result.get("summary", {})

    existing = _load_actions()
    generated = result.get("actions", [])
    generated_by_id = {a["id"]: a for a in generated}
    now_dt = datetime.now(timezone.utc)
    kept: list[dict] = []
    for action in existing:
        if _is_pace_year_action(action):
            continue
        if not _is_monthly_pacing_action(action):
            kept.append(action)
            continue
        status = action.get("status", "pending")
        action_id = action.get("id")
        if status in {"approved", "applied"}:
            kept.append(action)
            generated_by_id.pop(action_id, None)
        elif status == "rejected" and _recently_reviewed(action, now_dt):
            kept.append(action)
            generated_by_id.pop(action_id, None)

    actions = kept + list(generated_by_id.values())
    _save_actions(actions)
    monthly_actions = [a for a in actions if _is_monthly_pacing_action(a)]
    return monthly_actions, result.get("summary", {})


def _booking_promo_key(prop: Property) -> str:
    ll = lookup_links(prop.name)
    return str(getattr(ll, "booking_id", "") or prop.name).strip().lower()


def _split_ids(value: str | list | None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [
        part.strip()
        for part in str(value).replace(";", ",").replace("|", ",").split(",")
        if part.strip()
    ]


def _booking_mapping(prop: Property) -> dict:
    ll = lookup_links(prop.name)
    return {
        "booking_hotel_id": getattr(ll, "booking_hotel_id", "") if ll else "",
        "booking_room_ids": _split_ids(getattr(ll, "booking_room_ids", "") if ll else "") or _split_ids(os.environ.get("BOOKING_DEFAULT_ROOM_IDS")),
        "booking_parent_rate_ids": _split_ids(getattr(ll, "booking_parent_rate_ids", "") if ll else "") or _split_ids(os.environ.get("BOOKING_DEFAULT_PARENT_RATE_IDS")),
    }


def _booking_health(prop: Property) -> dict:
    benchmark = _benchmark_for(prop)
    score = 72
    score += min(12, max(-18, benchmark.get("occ_gap", 0) * 0.7))
    score += min(8, max(-12, benchmark.get("booked_14d_gap", 0) * 1.8))
    if prop.urgency == "critical":
        score -= 16
    elif prop.urgency == "warning":
        score -= 8
    elif prop.urgency == "overperforming":
        score += 8
    if prop.booked_14d == 0:
        score -= 8
    score = int(max(0, min(100, round(score))))
    if score < 45:
        label = "critical"
    elif score < 65:
        label = "needs lift"
    elif score > 82:
        label = "protect ADR"
    else:
        label = "stable"
    return {"score": score, "label": label, "benchmark": benchmark}


def _default_booking_promotion(prop: Property) -> dict:
    health = _booking_health(prop)
    ll = lookup_links(prop.name)
    booking_id = getattr(ll, "booking_id", None) if ll else None
    booking_url = getattr(ll, "booking_url", None) if ll else None
    mapping = _booking_mapping(prop)
    occ = prop.adj_occ_60d
    no_promos = bool(getattr(prop, "no_promotions", False))

    if no_promos or prop.urgency == "overperforming" or occ >= 0.70:
        promo_type = "none"
        discount = 0
        reason = "Protect ADR; current demand does not justify a Booking.com discount."
    elif prop.booked_14d == 0 and occ <= 0.20:
        promo_type = "limited_time_deal"
        discount = 12
        reason = "Low occupancy and no near-term pickup; use a narrow visibility lift instead of a broad rate cut."
    elif prop.urgency in {"critical", "warning"}:
        promo_type = "basic_deal"
        discount = 8
        reason = "Soft pacing suggests a modest Booking.com promotion test."
    else:
        promo_type = "mobile_rate"
        discount = 5
        reason = "Use a light audience-specific promotion only if Booking.com ranking is weak."

    start = TODAY + timedelta(days=7)
    end = start + timedelta(days=30)
    stay_start = TODAY + timedelta(days=14)
    stay_end = stay_start + timedelta(days=60)
    expected_rate = _round_to_5(prop.base_price * (1 - discount / 100)) if discount else prop.base_price
    net_adr_note = (
        f"Estimated gross ADR after discount: {_money(expected_rate)} before Booking.com commission, taxes, and stacked discounts."
        if discount else "No gross ADR discount proposed."
    )
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"booking-promo:{_booking_promo_key(prop)}")),
        "property": prop.name,
        "display_name": prop.property_name,
        "booking_id": booking_id or "",
        "booking_hotel_id": mapping["booking_hotel_id"] or booking_id or "",
        "booking_room_ids": mapping["booking_room_ids"],
        "booking_parent_rate_ids": mapping["booking_parent_rate_ids"],
        "booking_url": booking_url or "",
        "group_label": _group_label(prop),
        "health_score": health["score"],
        "health_label": health["label"],
        "promotion_type": promo_type,
        "discount_pct": discount,
        "book_start_date": start.isoformat(),
        "book_end_date": end.isoformat(),
        "stay_start_date": stay_start.isoformat(),
        "stay_end_date": stay_end.isoformat(),
        "audience": "all_travelers" if promo_type in {"basic_deal", "limited_time_deal"} else "mobile",
        "status": "draft",
        "source": "generated",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": None,
        "current_value": f"Base {_money(prop.base_price)}; occ60 {prop.adj_occ_60d:.0%}; booked14 {prop.booked_14d}",
        "expected_adr": expected_rate,
        "net_adr_note": net_adr_note,
        "reason": reason,
        "risk_flags": _booking_promo_risks(prop, discount, promo_type),
        "ai_review": "",
        "api_payload_preview": _booking_payload_preview(prop, promo_type, discount, start, end, stay_start, stay_end),
    }


def _booking_promo_risks(prop: Property, discount: int, promo_type: str) -> list[str]:
    risks = []
    mapping = _booking_mapping(prop)
    if getattr(prop, "no_promotions", False):
        risks.append("Owner note indicates no promotions; use manual approval only.")
    risks.append("Check overlapping Booking.com Genius, mobile, country, and length-of-stay discounts before applying.")
    if not lookup_links(prop.name) or not getattr(lookup_links(prop.name), "booking_url", None):
        risks.append("Booking.com listing ID or URL is missing from marketing links.")
    if not (mapping.get("booking_hotel_id") or (lookup_links(prop.name) and getattr(lookup_links(prop.name), "booking_id", None))):
        risks.append("Booking.com hotel/property ID is missing; API push cannot run.")
    if promo_type in {"basic_deal", "limited_time_deal", "last_minute_deal", "early_booker_deal"} and not mapping.get("booking_room_ids"):
        risks.append("Booking.com room type IDs are missing; API push cannot run for this promo type.")
    if promo_type != "none" and not mapping.get("booking_parent_rate_ids"):
        risks.append("Booking.com parent rate plan IDs are missing; API push cannot run.")
    if promo_type == "mobile_rate" and discount and discount < 10:
        risks.append("Booking.com mobile rates require a minimum 10% discount.")
    if prop.min_price and discount:
        expected = prop.base_price * (1 - discount / 100)
        if expected <= prop.min_price:
            risks.append("Proposed discount may push gross ADR close to the minimum price floor.")
    if promo_type == "none":
        risks.append("No promotion recommended; monitor rank and conversion before discounting.")
    return risks


def _booking_payload_preview(prop: Property, promo_type: str, discount: int, book_start: date, book_end: date, stay_start: date, stay_end: date) -> dict:
    mapping = _booking_mapping(prop)
    return {
        "provider": "Booking.com Connectivity Promotions API",
        "endpoint": "/promotions",
        "note": "Requires Booking.com token-based machine-account credentials and Promotions API permissions.",
        "hotel_id": mapping["booking_hotel_id"] or (lookup_links(prop.name).booking_id if lookup_links(prop.name) else ""),
        "room_ids": mapping["booking_room_ids"],
        "parent_rate_ids": mapping["booking_parent_rate_ids"],
        "promotion": {
            "type": promo_type,
            "discount_percentage": discount,
            "book_dates": {"from": book_start.isoformat(), "to": book_end.isoformat()},
            "stay_dates": {"from": stay_start.isoformat(), "to": stay_end.isoformat()},
        },
    }


def _generate_booking_promotions() -> list[dict]:
    existing = {item.get("id"): item for item in _load_booking_promotions()}
    generated = []
    for prop in [p for p in _PORTFOLIO if p.active][:150]:
        candidate = _default_booking_promotion(prop)
        old = existing.get(candidate["id"])
        if old:
            preserved = {**candidate, **old}
            preserved["health_score"] = candidate["health_score"]
            preserved["health_label"] = candidate["health_label"]
            preserved["current_value"] = candidate["current_value"]
            preserved["risk_flags"] = candidate["risk_flags"]
            preserved["api_payload_preview"] = candidate["api_payload_preview"]
            generated.append(preserved)
        else:
            generated.append(candidate)
    _save_booking_promotions(generated)
    return generated


def _booking_promotion_review(promo: dict) -> str:
    prop = _PORTFOLIO_INDEX.get(promo.get("property", ""))
    ctx = _property_context(prop) if prop else "Property context unavailable."
    prompt = f"""
Today is {TODAY.isoformat()}. Review this draft Booking.com promotion before a revenue manager applies it.

Property context:
{ctx}

Draft promotion:
{json.dumps(promo, indent=2)}

Return concise markdown with:
1. Verdict: approve, revise, or reject
2. Net ADR / stacking risk
3. Ranking and conversion rationale
4. Exact changes to make before applying

Rules:
- Never assume Booking.com discounts do not stack; call out stacking checks.
- Do not say the promotion was pushed or applied.
- If Booking.com ID is missing, say it cannot be pushed by API yet.
""".strip()
    try:
        client = _ai_client()
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are an STR revenue manager reviewing Booking.com promotions. Be concise, numeric, and cautious about discount stacking."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        flags = promo.get("risk_flags") or []
        verdict = "revise" if flags or int(promo.get("discount_pct") or 0) >= 10 else "approve"
        return (
            f"**Verdict: {verdict.title()}**\n\n"
            f"AI provider unavailable, so this is a rule-based review: {e}\n\n"
            f"**Checks Before Applying**\n"
            f"- Confirm Booking.com promotion stacking with Genius, mobile, country, and length-of-stay discounts.\n"
            f"- Confirm net ADR after commission/taxes and owner restrictions.\n"
            f"- Confirm Booking.com property ID is mapped.\n\n"
            f"**Risk Flags**\n" + ("\n".join(f"- {flag}" for flag in flags) if flags else "- No major rule-based flags.")
        )


def _benchmark_for(prop: Property) -> dict:
    active = [p for p in _PORTFOLIO if p.active and p.name != prop.name]
    same_segment = [
        p for p in active
        if p.area == prop.area and p.bedrooms == prop.bedrooms and p.urgency != "onboarding"
    ]
    group = same_segment
    basis = f"{prop.area} {prop.bedrooms}BR"
    if len(group) < 4:
        group = [p for p in active if p.area == prop.area and p.urgency != "onboarding"]
        basis = prop.area or "portfolio area"
    if len(group) < 4:
        group = [p for p in active if p.urgency != "onboarding"]
        basis = "portfolio"

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0

    occ = _avg([p.adj_occ_60d for p in group])
    booked_14d = _avg([p.booked_14d for p in group])
    base_price = _avg([p.base_price for p in group if p.base_price])
    return {
        "basis": basis,
        "sample_size": len(group),
        "occ_60d": round(occ * 100, 1),
        "booked_14d": round(booked_14d, 1),
        "base_price": round(base_price, 0) if base_price else None,
        "occ_gap": round((prop.adj_occ_60d - occ) * 100, 1),
        "booked_14d_gap": round(prop.booked_14d - booked_14d, 1),
    }


def _reload_portfolio(csv_path: Path | None = None):
    """Reload portfolio data from CSV — called after a hot-reload upload."""
    global _PORTFOLIO, _PORTFOLIO_INDEX, _SUMMARY
    import marketing_links as _ml
    _ml._LINKS = None          # bust marketing links cache
    with _portfolio_lock:
        _PORTFOLIO       = load_portfolio(csv_path)
        _PORTFOLIO_INDEX = {p.name: p for p in _PORTFOLIO}
        _SUMMARY         = portfolio_summary(_PORTFOLIO)
    print(f"[reload] Portfolio reloaded — {_SUMMARY['total_active']} active, {_SUMMARY['critical_count']} critical")


def _api_pct(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("%"):
        return text
    try:
        val = float(text)
    except ValueError:
        return text
    if 0 <= val <= 1:
        val *= 100
    return f"{val:.1f}%"


def _api_bool(value: object) -> str:
    return "TRUE" if bool(value) else "FALSE"


def _api_numeric(value: object) -> str:
    """Return numeric string or empty string — strips non-numeric values like 'Unavailable'."""
    text = str(value or "").strip()
    try:
        float(text)
        return text
    except (ValueError, TypeError):
        return ""


def _api_channel_tags(item: dict) -> str:
    parts = []
    for channel in item.get("channel_listing_details") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("channel_name") or "").strip()
        listing_id = str(channel.get("channel_listing_id") or "").strip()
        if name and listing_id:
            parts.append(f"{name}:{listing_id}")
    return "; ".join(parts)


def _derive_last_booked_date(item: dict, ha_last_booked: dict | None = None) -> str:
    from datetime import date, datetime, timezone, timedelta
    # 1. Hostaway reservations (most accurate)
    listing_id = str(item.get("id") or "")
    if ha_last_booked and listing_id and listing_id in ha_last_booked:
        try:
            d = date.fromisoformat(ha_last_booked[listing_id][:10])
            return d.strftime("%d %b %Y")
        except (ValueError, TypeError):
            pass
    # 2. PriceLabs API field (if ever added)
    for field in ("last_booked_date", "last_booking_date", "last_booked"):
        raw = item.get(field)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt.strftime("%d %b %Y")
            except (ValueError, TypeError):
                try:
                    d = date.fromisoformat(str(raw)[:10])
                    return d.strftime("%d %b %Y")
                except (ValueError, TypeError):
                    pass
    # 3. Estimate from booking pickup windows
    today = date.today()
    for days in (3, 7, 15):
        try:
            if int(item.get(f"booking_pickup_unique_past_{days}") or 0) > 0:
                return (today - timedelta(days=days)).strftime("%d %b %Y")
        except (ValueError, TypeError):
            pass
    return ""


def _write_pricelabs_api_portfolio(listings: list[dict], ha_last_booked: dict | None = None) -> dict:
    header = [
        "Listing ID",
        "Listing Name",
        "Listing Sync",
        "Show Listing",
        "Listing Status",
        "PMS Name",
        "Base Price",
        "Recommended Base Price",
        "Min Price",
        "Max Price",
        "Bedroom Count",
        "City",
        "Customization Group",
        "Customization Sub Group",
        "Tags",
        "Total Occupancy ( Next 60 Days )",
        "Total Occupancy ( Next 90 Days )",
        "Nights Booked ( Past 7 Days )",
        "Nights Booked ( Past 15 Days )",
        "Last Booked Date",
    ]
    rows = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        push_enabled = bool(item.get("push_enabled"))
        hidden = bool(item.get("isHidden"))
        tags = item.get("tags")
        if isinstance(tags, list):
            tag_text = "; ".join(str(t).strip() for t in tags if str(t).strip())
        else:
            tag_text = str(tags or "").strip()
        channel_tags = _api_channel_tags(item)
        if channel_tags:
            tag_text = "; ".join(v for v in [tag_text, channel_tags] if v)
        rows.append([
            item.get("id", ""),
            item.get("name", ""),
            _api_bool(push_enabled),
            _api_bool(not hidden),
            "available" if push_enabled and not hidden else "hidden",
            item.get("pms", ""),
            item.get("base", ""),
            _api_numeric(item.get("recommended_base_price", "")),
            item.get("min", ""),
            item.get("max", ""),
            item.get("no_of_bedrooms", ""),
            item.get("city_name", ""),
            item.get("group", ""),
            item.get("subgroup", ""),
            tag_text,
            _api_pct(item.get("occupancy_next_60")),
            _api_pct(item.get("occupancy_next_90")),
            item.get("booking_pickup_past_7", ""),
            item.get("booking_pickup_past_15", ""),
            _derive_last_booked_date(item, ha_last_booked or {}),
        ])

    target_path = CSV_PATH
    write_error: str | None = None
    try:
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
    except OSError as exc:
        # Vercel's bundled filesystem is read-only outside /tmp. Fall back to
        # writing the regenerated CSV to /tmp so _reload_portfolio() still has
        # fresh data; Supabase remains the durable copy.
        fallback = Path("/tmp") / CSV_PATH.name
        try:
            with fallback.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
            target_path = fallback
        except OSError as exc2:
            write_error = f"{type(exc).__name__}: {exc}; fallback failed: {type(exc2).__name__}: {exc2}"

    active_rows = [
        row for row in rows
        if str(row[2]).upper() == "TRUE" and str(row[3]).upper() == "TRUE"
    ]
    return {
        "total": len(rows),
        "active": len(active_rows),
        "path": str(target_path),
        "write_error": write_error,
    }


def _sync_hostaway_enrichment() -> dict[str, Any]:
    """Fetch Hostaway listings, reservation stats, and reviews and cache to disk."""
    try:
        client = hostaway_client_from_env()
        listings = client.listings(limit=500)
        listing_map = {str(l.get("id") or l.get("listingMapId", "")): l for l in listings if l}
        stats = client.reservation_stats_by_listing(days_back=180)
        reviews = client.reviews_by_listing(limit_per_listing=5)
        enrichment = {"listings": listing_map, "reservation_stats": stats, "reviews": reviews}
        HOSTAWAY_ENRICHMENT_PATH.write_text(json.dumps(enrichment, indent=2), encoding="utf-8")
        return enrichment
    except (HostawayAPIError, Exception):
        if HOSTAWAY_ENRICHMENT_PATH.exists():
            try:
                return json.loads(HOSTAWAY_ENRICHMENT_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


def _load_hostaway_enrichment() -> dict[str, Any]:
    if HOSTAWAY_ENRICHMENT_PATH.exists():
        try:
            return json.loads(HOSTAWAY_ENRICHMENT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _hostaway_context(prop: Property) -> str:
    """Return formatted Hostaway enrichment context for a property."""
    enrichment = _load_hostaway_enrichment()
    if not enrichment:
        return ""
    listing_map = enrichment.get("listings", {})
    stats_map = enrichment.get("reservation_stats", {})
    reviews_map = enrichment.get("reviews", {})
    lid = str(prop.listing_id or "").strip()
    ha = listing_map.get(lid) or {}
    stats = stats_map.get(lid) or {}
    reviews = reviews_map.get(lid) or []
    if not ha and not stats:
        return ""
    lines = ["Hostaway Listing Data:"]
    if ha.get("personCapacity"):
        lines.append(f"  Guest Capacity: {ha['personCapacity']}")
    if ha.get("bathroomsNumber"):
        lines.append(f"  Bathrooms: {ha['bathroomsNumber']}")
    if ha.get("checkInTimeStart") or ha.get("checkOutTime"):
        lines.append(f"  Check-in: {ha.get('checkInTimeStart', 'unknown')}  |  Check-out: {ha.get('checkOutTime', 'unknown')}")
    # Cleaning fee from PriceLabs snapshot
    pl_data = next((l for l in _load_pricelabs_snapshot() if str(l.get("id","")) == lid), {})
    if pl_data.get("cleaning_fees"):
        lines.append(f"  Cleaning Fee: ${pl_data['cleaning_fees']:.0f}")
    # OTA channels and service fees
    channels = ha.get("channelListingDetails") or pl_data.get("channel_listing_details") or []
    if channels:
        ota_fees = {"airbnb": "3% host fee", "vrbo": "5% host fee", "booking.com": "15% commission", "bookingcom": "15% commission"}
        ch_lines = []
        for ch in channels:
            name = str(ch.get("channel_name") or ch.get("channelName") or "").lower()
            ch_id = ch.get("channel_listing_id") or ch.get("channelListingId") or ""
            fee = ota_fees.get(name, "fee unknown")
            ch_lines.append(f"{name} (ID: {ch_id}, {fee})")
        lines.append(f"  OTA Channels: {'; '.join(ch_lines)}")
    amenities = ha.get("amenities") or []
    if isinstance(amenities, list) and amenities:
        lines.append(f"  Amenities ({len(amenities)} total): {', '.join(str(a) for a in amenities[:20])}")
    desc = str(ha.get("description") or "").strip()
    if desc:
        lines.append(f"  Description (first 300 chars): {desc[:300]}")
    rules = str(ha.get("houseRules") or "").strip()
    if rules:
        lines.append(f"  House Rules (first 200 chars): {rules[:200]}")
    if stats:
        lines.append("Hostaway Reservation Stats (last 180 days):")
        if stats.get("avg_lead_days") is not None:
            lines.append(f"  Avg booking lead time: {stats['avg_lead_days']} days")
        if stats.get("avg_los") is not None:
            lines.append(f"  Avg length of stay: {stats['avg_los']} nights")
        if stats.get("total_nights"):
            lines.append(f"  Total nights booked: {stats['total_nights']}")
        sources = stats.get("sources") or {}
        if sources:
            top = sorted(sources.items(), key=lambda x: x[1], reverse=True)[:5]
            lines.append(f"  Booking sources: {', '.join(f'{s}={n}' for s, n in top)}")
    if reviews:
        lines.append(f"Recent Guest Reviews ({len(reviews)} most recent):")
        for r in reviews:
            rating = f"{r['rating']}/5" if r.get("rating") else "no rating"
            comment = r.get("comment") or "no comment"
            lines.append(f"  [{r.get('date','')} {r.get('channel','')} {rating}] {comment}")
    return "\n".join(lines)


def _load_pricelabs_snapshot() -> list[dict]:
    try:
        data = json.loads(PRICELABS_API_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return data.get("listings", data) if isinstance(data, dict) else data
    except Exception:
        return []


def _sync_pricelabs_api() -> dict:
    response = client_from_env().request("GET", "/listings")
    listings = response.get("listings") if isinstance(response, dict) else response
    if not isinstance(listings, list):
        raise PriceLabsAPIError("PriceLabs /listings did not return a listings array.")
    PRICELABS_API_SNAPSHOT_PATH.write_text(json.dumps(response, indent=2), encoding="utf-8")
    try:
        ha_client = hostaway_client_from_env()
        ha_last_booked = ha_client.last_booked_by_listing()
    except (HostawayAPIError, Exception):
        ha_last_booked = {}
    written = _write_pricelabs_api_portfolio(listings, ha_last_booked)
    _reload_portfolio()
    _sync_hostaway_enrichment()
    return {
        "source": "PriceLabs Customer API /listings",
        "listings_total": written["total"],
        "listings_active": written["active"],
        "snapshot": str(PRICELABS_API_SNAPSHOT_PATH),
        "portfolio_csv": written.get("path"),
        "csv_write_error": written.get("write_error"),
    }


def _parse_pricelabs_hook(raw_hook: str) -> tuple[str, str]:
    parts = raw_hook.strip().split(None, 1)
    if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "PUT", "PATCH"}:
        return parts[0].upper(), parts[1].strip()
    return "POST", raw_hook.strip()


def _pricelabs_post_apply_payload(apply_result: dict) -> dict:
    return {
        "listing_id": apply_result.get("listing_id"),
        "pms": apply_result.get("pms"),
        "endpoint": apply_result.get("endpoint"),
        "confirmed_base": apply_result.get("confirmed_base"),
        "confirmed_dates": apply_result.get("confirmed_dates"),
        "adjusted_start_date": apply_result.get("adjusted_start_date"),
        "source": "Haven Dashboard post-apply refresh",
    }


def _run_pricelabs_post_apply_hooks(apply_result: dict) -> dict:
    """Run optional PriceLabs Save/Refresh/Sync endpoints after a successful push.

    PriceLabs documents Save & Refresh/Sync primarily as UI actions. If the
    account has API endpoints for those actions, configure them with
    PRICELABS_POST_APPLY_ENDPOINTS, for example:
      POST /listings/{listing_id}/refresh, POST /listings/{listing_id}/sync
    """
    hooks_text = (
        os.environ.get("PRICELABS_POST_APPLY_ENDPOINTS")
        or os.environ.get("PRICELABS_SAVE_REFRESH_ENDPOINTS")
        or ""
    ).strip()
    if not hooks_text:
        return {
            "configured": False,
            "attempted": False,
            "message": "No PriceLabs Save/Refresh/Sync API hook configured.",
        }

    client = client_from_env()
    listing_id = str(apply_result.get("listing_id") or "")
    pms = str(apply_result.get("pms") or "")
    payload = _pricelabs_post_apply_payload(apply_result)
    calls = []
    for raw_hook in hooks_text.split(","):
        raw_hook = raw_hook.strip()
        if not raw_hook:
            continue
        method, endpoint = _parse_pricelabs_hook(raw_hook)
        endpoint = endpoint.format(listing_id=listing_id, pms=pms)
        response = client.request(method, endpoint, None if method == "GET" else payload)
        calls.append({
            "method": method,
            "endpoint": endpoint,
            "response": response,
        })

    return {
        "configured": True,
        "attempted": bool(calls),
        "calls": calls,
        "message": f"Ran {len(calls)} configured PriceLabs Save/Refresh/Sync hook(s).",
    }


def _verify_pricelabs_push_from_sync(apply_result: dict) -> dict:
    sync_result = _sync_pricelabs_api()
    listing_id = str(apply_result.get("listing_id") or "")
    confirmed_base = apply_result.get("confirmed_base")
    verification = {
        "synced": True,
        "sync": sync_result,
        "verified": None,
        "message": "Dashboard re-synced from PriceLabs Customer API.",
    }
    if confirmed_base is None:
        verification["message"] = (
            "Dashboard re-synced from PriceLabs Customer API; date override read-back "
            "is not available from the current snapshot."
        )
        return verification

    try:
        snapshot = json.loads(PRICELABS_API_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the PriceLabs snapshot could not be read for verification."
        return verification

    listings = snapshot.get("listings") if isinstance(snapshot, dict) else snapshot
    match = next(
        (
            item for item in listings or []
            if isinstance(item, dict) and str(item.get("id")) == listing_id
        ),
        None,
    )
    if not match:
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the updated listing was not found in the PriceLabs snapshot."
        return verification

    try:
        synced_base = int(round(float(match.get("base"))))
    except (TypeError, ValueError):
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the listing base price was not readable."
        return verification

    verification["synced_base"] = synced_base
    verification["verified"] = synced_base == int(confirmed_base)
    if verification["verified"]:
        verification["message"] = f"Dashboard re-synced and verified PriceLabs base price at ${synced_base}."
    else:
        verification["message"] = (
            f"Dashboard re-synced, but PriceLabs snapshot still shows base ${synced_base} "
            f"instead of ${confirmed_base}."
        )
    return verification


def _finalize_pricelabs_push(apply_result: dict) -> dict:
    post_apply = {"verified_sync": None, "refresh": None}
    try:
        post_apply["refresh"] = _run_pricelabs_post_apply_hooks(apply_result)
        post_apply["verified_sync"] = _verify_pricelabs_push_from_sync(apply_result)
    except PriceLabsAPIError as e:
        raise PricingApplyError(f"PriceLabs push succeeded, but post-push refresh/sync failed: {e}") from e
    apply_result["post_apply"] = post_apply
    return apply_result

# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _property_links_context(prop: Property) -> str:
    ll = lookup_links(prop.name)
    if not ll:
        return "OTA Links: not found in marketing data"
    lines = []
    if ll.airbnb_url:
        lines.append(f"  Airbnb: {ll.airbnb_url}  (headline: \"{ll.airbnb_headline}\")")
        if ll.airbnb_rating is not None:
            lines.append(f"    Rating: {_format_rating(ll.airbnb_rating, ll.airbnb_reviews)} — status: {ll.airbnb_rating_status}")
        if ll.airbnb_photos is not None:
            lines.append(f"    Photos: {_format_count(ll.airbnb_photos)} — grade: {ll.airbnb_photo_grade}")
    else:
        lines.append("  Airbnb: NOT LISTED")
    if ll.vrbo_url:
        lines.append(f"  VRBO:   {ll.vrbo_url}  (headline: \"{ll.vrbo_headline}\")")
        if ll.vrbo_rating is not None:
            lines.append(f"    Rating: {_format_rating(ll.vrbo_rating, ll.vrbo_reviews)} — status: {ll.vrbo_rating_status}")
        if ll.vrbo_photos is not None:
            lines.append(f"    Photos: {_format_count(ll.vrbo_photos)} — grade: {ll.vrbo_photo_grade}")
    else:
        lines.append("  VRBO:   NOT LISTED")
    if getattr(ll, "booking_url", None):
        lines.append(f"  Booking.com: {ll.booking_url}")
    else:
        lines.append("  Booking.com: NOT LISTED")
    lines.append(f"  PMS ID: {ll.streamline_id or 'unknown'}")
    return "OTA Links:\n" + "\n".join(lines)


def _format_rating(rating: float | None, reviews: int | None) -> str:
    if rating is None:
        return "unknown"
    review_text = f" from {reviews} reviews" if reviews is not None else ""
    scale = 10 if rating > 5 else 5
    return f"{rating:.1f}/{scale}{review_text}"


def _format_count(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _property_context(prop: Property) -> str:
    occ_60 = f"{prop.adj_occ_60d:.0%}" if prop.adj_occ_60d else "0%"
    occ_90 = f"{prop.adj_occ_90d:.0%}" if prop.adj_occ_90d else "0%"
    last_bkd = f"{prop.last_booked_days} days ago" if prop.last_booked_days is not None else "no booking on record"
    min_p = f"${prop.min_price:.0f}" if prop.min_price else "unknown"
    min_stay = f"{prop.min_stay} nights" if prop.min_stay else "unknown"
    demand = f"{prop.demand_sensitivity}%" if prop.demand_sensitivity else "unknown"
    safety_minimum = f"{prop.historical_anchoring}%" if prop.historical_anchoring else "not synced"
    links_ctx = _property_links_context(prop)
    benchmark = _benchmark_for(prop)
    return f"""
Property: {prop.name}
Tags/Area: {prop.tags}
Bedrooms: {prop.bedrooms}
Urgency Status: {prop.urgency.upper()} (score {prop.urgency_score}/100)
Known Issues: {"; ".join(prop.issues) if prop.issues else "none flagged"}
Owner Notes / Restrictions: {_owner_note(prop) or "none detected"}

PriceLabs Settings:
  Base Price: ${prop.base_price:.0f}
  Min Price: {min_p}
  Min Stay: {min_stay}
  Last Minute: {prop.last_minute}  |  Far Future: {prop.far_future}
  Day of Week: {prop.day_of_week}  |  Seasonality: {prop.seasonality}
  Demand Sensitivity: {demand}  |  Safety Minimum / Historical Anchoring: {safety_minimum}
  Long-term Pricing: {prop.long_term_pricing}
  Occupancy Pacing: {prop.occupancy_pacing}
  Gaps & Adjacencies: {prop.gaps_adjacencies}
  Events & Seasons: Enabled

{links_ctx}

Tag-Based Neighborhood Benchmark:
  Benchmark Group: {benchmark['basis']} ({benchmark['sample_size']} listings)
  Listing Occupancy 60-day: {prop.adj_occ_60d:.0%}
  Benchmark Occupancy 60-day: {benchmark['occ_60d']}%
  Occupancy Gap vs Benchmark: {benchmark['occ_gap']} percentage points
  Listing Booked Nights 14-day: {prop.booked_14d}
  Benchmark Avg Booked Nights 14-day: {benchmark['booked_14d']}
  Base Price vs Benchmark Avg: ${prop.base_price:.0f} vs ${benchmark['base_price'] if benchmark['base_price'] is not None else 'unknown'}

Occupancy & Booking Data:
  Adjusted Occupancy 60-day: {occ_60}
  Adjusted Occupancy 90-day: {occ_90}
  Booked nights next 7 days: {prop.booked_7d}
  Booked nights next 14 days: {prop.booked_14d}
  Last booked: {last_bkd}
  Min price hit rate 60-day: {prop.min_price_occ_60d:.0%}
  Min price hit rate 90-day: {prop.min_price_occ_90d:.0%}

Important data quality note:
If a setting says "unknown", do not infer or invent it. Treat PriceLabs as the pricing source.

{_hostaway_context(prop)}
""".strip()


def _apply_weekly_action(action: dict) -> dict:
    payload = action.get("pricelabs_payload") or {}
    kind = payload.get("kind")
    if not kind:
        raise PricingApplyError("This action has no PriceLabs payload. Regenerate weekly suggestions.")
    if kind == "manual_review":
        raise PricingApplyError("This is a manual review item and should not be pushed to PriceLabs.")

    listing_id = str(action.get("listing_id") or "").strip()
    pms_name = str(action.get("pms_name") or "").strip()
    if not listing_id or not pms_name:
        raise PricingApplyError("This action is missing the PriceLabs listing ID or PMS name. Regenerate weekly suggestions from the current PriceLabs export.")

    start_text = payload.get("start_date") or payload.get("override_start")
    end_text = payload.get("end_date") or payload.get("override_end") or start_text
    if not start_text:
        if kind not in {"base_percentage_review", "base_price_review"}:
            raise PricingApplyError(f"Unsupported PriceLabs action kind: {kind}.")
        new_base = payload.get("suggested_base_price") or payload.get("estimated_base_price")
        if not new_base:
            raise PricingApplyError("This base-price action is missing the target base price.")
        new_base_value = int(round(float(new_base)))
        prop = _resolve_property(action.get("property", ""))
        if prop and prop.min_price and new_base_value <= int(round(float(prop.min_price))):
            raise PricingApplyError(
                f"Not pushed: target base ${new_base_value} is at or below the current PriceLabs minimum "
                f"price ${int(round(float(prop.min_price)))}. The calendar would stay constrained by the "
                "minimum price floor. Review the floor separately before pushing this base-price decrease."
            )
        listing_update = {
            "id": listing_id,
            "pms": pms_name,
            "base": new_base_value,
        }
        request_payload = {"listings": [listing_update]}
        try:
            response = client_from_env().request("POST", "/listings", request_payload)
        except PriceLabsAPIError as e:
            raise PricingApplyError(str(e)) from e

        returned_listings = response.get("listings") if isinstance(response, dict) else None
        if returned_listings is not None:
            confirmed = [
                item for item in returned_listings
                if isinstance(item, dict)
                and str(item.get("id")) == listing_id
                and int(float(item.get("base", -1))) == listing_update["base"]
            ]
            if not confirmed:
                raise PricingApplyError(
                    "PriceLabs responded, but did not confirm the requested base price update."
                )

        return {
            "provider": "PriceLabs",
            "listing_id": listing_id,
            "pms": pms_name,
            "endpoint": "/listings",
            "sent": request_payload,
            "response": response,
            "confirmed_base": listing_update["base"],
        }

    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (TypeError, ValueError) as e:
        raise PricingApplyError("This action has invalid PriceLabs override dates.") from e

    adjusted_start = max(start, EARLIEST_RATE_EDIT_DATE)
    if end < adjusted_start:
        raise PricingApplyError("This action only affects same-day or past dates, so it was not pushed to PriceLabs.")

    override_items = []
    day = adjusted_start
    while day <= end:
        item = {"date": day.isoformat()}
        if kind in {"custom_percentage_adjustment", "monthly_date_override"}:
            pct = payload.get("adjustment_pct")
            if pct is None:
                raise PricingApplyError("This percentage action is missing adjustment_pct.")
            item["price"] = str(round(float(pct) * 100, 2)).rstrip("0").rstrip(".")
            item["price_type"] = "percent"
        elif kind == "kcity_dso_min_price":
            min_price = payload.get("min_price")
            if not min_price:
                raise PricingApplyError("This DSO action is missing min_price.")
            item["minPrice"] = int(round(float(min_price)))
        elif kind == "custom_fixed_rate":
            rate = payload.get("suggested_rate")
            if not rate:
                raise PricingApplyError("This fixed-rate action is missing suggested_rate.")
            item["price"] = str(int(round(float(rate))))
            item["price_type"] = "fixed"
            item["currency"] = payload.get("currency") or "USD"
        else:
            raise PricingApplyError(
                f"PriceLabs push is only enabled for date-level overrides right now. Unsupported action kind: {kind}."
            )
        override_items.append(item)
        day += timedelta(days=1)

    request_payload = {"pms": pms_name, "overrides": override_items}
    try:
        response = client_from_env().request("POST", f"/listings/{listing_id}/overrides", request_payload)
    except PriceLabsAPIError as e:
        raise PricingApplyError(str(e)) from e

    returned_overrides = response.get("overrides") if isinstance(response, dict) else None
    returned_dates = {
        str(item.get("date"))
        for item in returned_overrides or []
        if isinstance(item, dict) and item.get("date")
    }
    requested_dates = {item["date"] for item in override_items}
    if returned_overrides is not None and not requested_dates.issubset(returned_dates):
        missing = ", ".join(sorted(requested_dates - returned_dates))
        raise PricingApplyError(
            "PriceLabs responded, but did not confirm every requested override. "
            f"Unconfirmed date(s): {missing or 'unknown'}."
        )

    return {
        "provider": "PriceLabs",
        "listing_id": listing_id,
        "pms": pms_name,
        "endpoint": f"/listings/{listing_id}/overrides",
        "sent": request_payload,
        "response": response,
        "confirmed_dates": sorted(returned_dates) if returned_dates else sorted(requested_dates),
        "adjusted_start_date": adjusted_start.isoformat() if adjusted_start != start else None,
    }


def _build_property_prompt(report_type: str, prop: Property) -> str:
    ctx = _property_context(prop)
    today = TODAY.isoformat()
    is_fast = prop.urgency == "overperforming"

    base = f"""
Today is {today}. Analyze this HVR Smokies / Tennessee STR property from the PriceLabs portfolio export:

{ctx}

Pricing recommendation guardrails:
- If status is CRITICAL or WARNING, do not recommend increasing base price, minimum price, or nightly rates.
- Use percentage adjustments for pricing requests, such as -3%, -8%, -10%, or +5%.
- Only recommend a fixed dollar rate when the percentage adjustment would hit or cross the minimum price floor.
- If owner tags include Fixed Min Rate or No Promotions, call that out and avoid promo/discount campaign language.
- Do not recommend matching a benchmark average blindly; use benchmark only as context.
- Never recommend or apply same-day rate edits. Same-day bookings are not allowed.
- Never edit Last Minute adjustment settings. Today's available rate is intentionally protected by a 999% last-minute increase.
- Do not include sections where all inputs are unknown or not synced. Say what data is missing in one sentence only when it affects the recommendation.
- Do not use Maui, Hawaii, beach, or island seasonality. Use Tennessee Smokies demand logic: cabin/leisure drive-to demand, weekends, summer family travel, fall foliage, holidays, events, and city-level context from the listing data.

""".strip()

    why_not_booking_prompt = f"""
{base}

Explain what is working for this fast-paced property and where we may be leaving ADR on the table.

1. **What Is Working** — Why is this property pacing fast? Use occupancy, pickup, benchmark gap, OTA links, city, bedroom count, and group as evidence.
2. **ADR Protection** — Identify whether the base/min setup looks too low, reasonable, or needs only selective future-date review.
3. **Demand & Timing** — Use Tennessee Smokies logic for {prop.city or prop.area}: weekend compression, summer/fall demand, holidays, and drive-to leisure demand.
4. **Opportunity Check** — Give specific opportunities to protect revenue without hurting conversion: selected date increases, restriction review, content/OTA visibility, and channel mix.
5. **Risks To Watch** — Note any risk from underpricing, minimum floors, overly generous discounts, or missing OTA/listing data.
6. **Specific Next Moves** — Give 3-5 concrete actions. Do not recommend broad discounts for a fast-paced listing.
""".strip() if is_fast else f"""
{base}

Diagnose why this property has {prop.booked_7d} bookings in next 7 days and {prop.booked_14d} in next 14 days.

1. **Pricing Diagnosis** — Min-price occurrence is {prop.min_price_occ_60d:.0%}. If current min price is unknown, say the floor must be checked in PriceLabs before recommending changes.
2. **Demand & Timing** — Use Tennessee Smokies logic for {prop.city or prop.area}: weekday/weekend pattern, summer travel, fall foliage, holidays, and local drive-to demand.
3. **Booking Blockers** — Identify only blockers supported by synced data: low pickup, low occupancy, missing OTA link, Airbnb link export issue, weak title/photo/review signal, or channel visibility.
4. **Min Stay / Restrictions Check** — Only analyze minimum stay if the current setting is explicitly known. Otherwise list it as a Hostaway/PriceLabs check, not a finding.
5. **Last-Minute Strategy** — Never edit same-day protection. Recommend only future-date percentage actions where demand and pickup support it.
6. **Specific Fixes** — Give 3-5 concrete fixes tied to this listing's actual data. Avoid generic advice.
""".strip()

    sections = {
        "overview": f"""
{base}

Provide a full revenue management assessment:

1. **Urgency Assessment** — Why is this property at {prop.urgency.upper()} status? What's the revenue impact?
2. **Booking Pace Analysis** — {prop.booked_7d} booked nights next 7 days, {prop.booked_14d} next 14 days. What does this tell us?
3. **Pricing Analysis** — Is the base price of ${prop.base_price:.0f} appropriate for a {prop.bedrooms}BR property in {prop.area}?
4. **Action Plan** — Top 5 specific actions to take this week in PriceLabs, Hostaway, or OTA only where supported by synced data
5. **Revenue Opportunity** — Estimate the 90-day revenue uplift if occupancy and pickup recover
""".strip(),

        "why_not_booking": why_not_booking_prompt,

        "revenue": f"""
{base}

Run a full revenue and pricing analysis:

1. **KPI Status** — Occupancy 60d: {prop.adj_occ_60d:.0%} vs target ~75%. How far off and what's the dollar gap?
2. **Booking Insights / Monthly Performance** — Use PriceLabs Booking Insights, Report Builder, and portfolio pacing data as the source of truth. If OTA reservation source is not present in the synced PriceLabs data, say that PriceLabs needs to expose or export the booking-source columns.
3. **PriceLabs Settings Audit** — Discuss Demand Sensitivity only if synced. Treat Historical Anchoring as PriceLabs Safety Minimum; do not call it a separate setting.
4. **Specific Rate Changes** — Give exact percentage-based pricing actions for this week. Use fixed dollar rates only when the minimum floor is actually reached; do not recommend maximum rates or price ceilings.
5. **Channel Revenue Risk** — Use only known OTA links/channel data. If booking source is not synced, do not estimate channel mix.
6. **90-Day Projection** — If all pricing fixes are implemented, project the revenue uplift
""".strip(),

        "weekly_actions": _build_weekly_actions_prompt(prop),
    }

    if report_type == "listing":
        return _build_listing_quality_prompt(prop)

    return sections.get(report_type, sections["overview"])


def _build_weekly_actions_prompt(prop: Property) -> str:
    return f"""
Today is {TODAY.isoformat()}. Create a weekly revenue-management review for this property using only the data below.

{_property_context(prop)}

Return:
1. Current health summary
2. Top 3 recommended actions
3. Risk level for each action
4. What should be checked before approving
5. Exact proposed action text for an approval queue

Rules:
- Do not claim an action has been applied.
- Do not recommend changing unknown settings.
- If the property is CRITICAL or WARNING, do not recommend increasing base price, minimum floor, or rates.
- Use percentage adjustments for pricing requests, such as -3%, -8%, -10%, or +5%.
- Only switch to a fixed dollar rate when the calculated percentage rate would hit or cross the minimum price floor.
- If owner tags include Fixed Min Rate or No Promotions, call that out and avoid discount/promo language.
- Every proposed action must be approve/decline friendly and include the exact field/date/percentage to review.
- Same-day bookings are not allowed: do not propose or apply rate edits for today.
- Never edit Last Minute adjustment settings; the 999% same-day protection must stay locked.
""".strip()


def _local_property_report(report_type: str, prop: Property, error: Exception | None = None) -> str:
    if report_type == "listing":
        return _local_listing_optimizer_report(prop, error)

    benchmark = _benchmark_for(prop)
    occ_gap = benchmark.get("occ_gap", 0)
    occ = prop.adj_occ_60d
    pickup = prop.booked_14d
    status = prop.urgency.upper()
    issue_text = "; ".join(prop.issues) if prop.issues else "No major PriceLabs issue flags."
    owner_text = _owner_note(prop) or "No owner promo restriction detected."

    if prop.urgency in {"critical", "warning"} and occ < 0.35 and pickup == 0:
        verdict = "Demand is soft. Review date-level price position before using broad promotions."
        action = "Check exposed open dates, compare posted rate to market booked and last-year booked price, then use a narrow date adjustment only where our posted rate is high."
    elif prop.urgency == "overperforming":
        verdict = "Booking pace is strong. Protect ADR and review whether future dates are underpriced."
        action = "Avoid discounts. Review selective increases for high-demand forward windows."
    elif pickup == 0:
        verdict = "No recent pickup, but occupancy is not weak enough for an automatic discount."
        action = "Hold base price and inspect restrictions, availability, channel visibility, and orphan gaps."
    else:
        verdict = "No emergency price move from current synced metrics."
        action = "Monitor pickup and use the forward-pacing price-position check for date-level decisions."

    ai_note = f"\n\n_AI provider unavailable: {error}_" if error else ""
    return f"""
## Local Revenue Review

**Verdict:** {verdict}

**Current Signal**
- Status: {status}
- Base price: {_money(prop.base_price)}
- Minimum price: {_money(prop.min_price) if prop.min_price else "unknown"}
- 60-day adjusted occupancy: {occ:.0%}
- Booked nights, last 15-day pickup window: {pickup}
- Benchmark: {benchmark.get("basis", "portfolio")} ({benchmark.get("sample_size", 0)} listings)
- Occupancy gap vs benchmark: {occ_gap:+.1f} pts

**Issues**
{issue_text}

**Owner / Promo Constraint**
{owner_text}

**Recommended Next Action**
{action}

**Forward Pacing Rule**
Do not discount just because pacing is behind. Compare:
- our posted future price and posted percentile
- market booked price
- last year's booked price
- market posted price

If we are behind pace but already below market booked and last-year booked price, hold price and check conversion, restrictions, visibility, availability, and Booking.com promotion stacking.
{ai_note}
""".strip()


def _local_listing_optimizer_report(prop: Property, error: Exception | None = None) -> str:
    ll = lookup_links(prop.name)
    airbnb_title = ll.airbnb_headline if ll and ll.airbnb_headline else "Not synced"
    vrbo_title = ll.vrbo_headline if ll and ll.vrbo_headline else "Not synced"
    airbnb_rating = _format_rating(ll.airbnb_rating, ll.airbnb_reviews) if ll else "unknown"
    vrbo_rating = _format_rating(ll.vrbo_rating, ll.vrbo_reviews) if ll else "unknown"
    airbnb_photos = _format_count(ll.airbnb_photos) if ll else "unknown"
    vrbo_photos = _format_count(ll.vrbo_photos) if ll else "unknown"
    ha_ctx = _hostaway_context(prop)
    # Pull enrichment data directly for structured display
    enrichment = _load_hostaway_enrichment()
    lid = str(prop.listing_id or "").strip()
    ha = (enrichment.get("listings") or {}).get(lid) or {}
    ha_reviews = (enrichment.get("reviews") or {}).get(lid) or []
    pl_listings = _load_pricelabs_snapshot()
    pl_data = next((l for l in pl_listings if str(l.get("id","")) == lid), {})
    cleaning_fee = pl_data.get("cleaning_fees")
    channels = ha.get("channelListingDetails") or pl_data.get("channel_listing_details") or []
    ota_fee_map = {"airbnb": "3% host fee", "vrbo": "5% host fee", "booking.com": "15% commission", "bookingcom": "15% commission"}
    evidence = []
    if ll and (ll.airbnb_headline or ll.vrbo_headline):
        evidence.append("titles")
    if ll and (ll.airbnb_photos is not None or ll.vrbo_photos is not None):
        evidence.append("photo counts")
    if ll and (ll.airbnb_rating is not None or ll.vrbo_rating is not None):
        evidence.append("ratings/reviews")
    if ha_reviews:
        evidence.append("hostaway reviews")
    score_note = "provisional" if len(evidence) < 3 else "evidence-based"
    ai_note = f"\n\n_AI provider unavailable: {error}_" if error else ""

    # OTA channels section
    ota_lines = []
    for ch in channels:
        name = str(ch.get("channel_name") or ch.get("channelName") or "").lower()
        ch_id = ch.get("channel_listing_id") or ch.get("channelListingId") or "unknown"
        fee = ota_fee_map.get(name, "fee unknown")
        ota_lines.append(f"- {name.title()}: ID {ch_id} — {fee}")
    ota_section = "\n".join(ota_lines) if ota_lines else "- No OTA channel data synced"

    # Fees section
    fees_section = f"- Cleaning Fee: ${cleaning_fee:.0f}" if cleaning_fee else "- Cleaning fee: not synced"

    # Reviews section
    if ha_reviews:
        review_lines = []
        for r in ha_reviews:
            rating = f"⭐ {r['rating']}/5" if r.get("rating") else ""
            ch = r.get("channel", "").title()
            dt = r.get("date", "")
            comment = r.get("comment") or "No comment"
            review_lines.append(f"**{ch} {dt} {rating}**\n  _{comment}_")
        reviews_section = "\n\n".join(review_lines)
    else:
        reviews_section = "_No reviews synced from Hostaway yet. Run Sync to pull recent reviews._"

    return f"""
## Listing Optimizer

**Evidence-Based Quality Score:** {score_note}

## Title Optimization

**Current**
- Airbnb: {airbnb_title}
- VRBO: {vrbo_title}

**Recommended**
- Airbnb: Keep under 50 characters; lead with location or the clearest guest-facing hook.
- VRBO: Use a slightly fuller title under 70 characters with bedroom count and primary amenity.

## Photo And Visual Check
- Airbnb photos: {airbnb_photos} ({ll.airbnb_photo_grade if ll else "not synced"})
- VRBO photos: {vrbo_photos} ({ll.vrbo_photo_grade if ll else "not synced"})

Manual check: cover image, first 8 photo order, bedroom/bath coverage, hot tub/view/game-room proof, and thumbnail crop.

## Reviews And Trust
- Airbnb: {airbnb_rating}
- VRBO: {vrbo_rating}

### Recent Guest Reviews (from Hostaway)
{reviews_section}

## OTA Channels & Fees
{ota_section}

### Property Fees
{fees_section}

## Positioning
- Group: {_group_label(prop)}
- Bedrooms: {prop.bedrooms}
- Guest Capacity: {ha.get('personCapacity', 'not synced')}
- Bathrooms: {ha.get('bathroomsNumber', 'not synced')}
- Inferred segment: {"families / groups" if prop.bedrooms >= 3 else "couples / small groups"}

{f"## Hostaway Listing Details{chr(10)}{ha_ctx}" if ha_ctx else ""}

## Action Checklist
1. Open Airbnb, VRBO, and Booking.com links from the dashboard.
2. Confirm the OTA title matches the intended positioning.
3. Check whether the first photo sells the main reason to book.
4. Verify amenity filters that affect search conversion.
5. Confirm bedroom/bath count and sleeping setup consistency.
6. Review recent guest feedback above and address recurring complaints.
{ai_note}
""".strip()


def _has_listing_quality_data(prop: Property) -> bool:
    ll = lookup_links(prop.name)
    if not ll:
        return False
    has_real_title = any(
        title and title.strip().lower() != prop.property_name.strip().lower()
        for title in (ll.airbnb_headline, ll.vrbo_headline)
    )
    has_reviews = ll.airbnb_rating is not None or ll.vrbo_rating is not None
    has_photos = ll.airbnb_photos is not None or ll.vrbo_photos is not None
    return has_real_title or has_reviews or has_photos


def _build_listing_quality_prompt(prop: Property) -> str:
    quality_rules = _listing_quality_rules()
    ll = lookup_links(prop.name)
    airbnb_title = ll.airbnb_headline if ll and ll.airbnb_headline else "Not available"
    vrbo_title   = ll.vrbo_headline   if ll and ll.vrbo_headline   else "Not available"
    airbnb_url   = ll.airbnb_url      if ll and ll.airbnb_url      else "Not listed"
    vrbo_url     = ll.vrbo_url        if ll and ll.vrbo_url        else "Not listed"
    pms_id = ll.streamline_id  if ll else "Unknown"
    airbnb_rating = _format_rating(ll.airbnb_rating, ll.airbnb_reviews) if ll else "unknown"
    airbnb_rating_status = ll.airbnb_rating_status if ll else "unknown"
    vrbo_rating = _format_rating(ll.vrbo_rating, ll.vrbo_reviews) if ll else "unknown"
    vrbo_rating_status = ll.vrbo_rating_status if ll else "unknown"
    airbnb_photos = _format_count(ll.airbnb_photos) if ll else "unknown"
    vrbo_photos = _format_count(ll.vrbo_photos) if ll else "unknown"
    airbnb_photo_grade = ll.airbnb_photo_grade if ll else "unknown"
    vrbo_photo_grade = ll.vrbo_photo_grade if ll else "unknown"
    min_price = f"${prop.min_price:.0f}" if prop.min_price else "unknown"
    min_stay = f"{prop.min_stay} nights" if prop.min_stay else "unknown"
    review_lines = []
    if ll and ll.airbnb_rating is not None:
        review_lines.append(f"  Airbnb Rating: {airbnb_rating} — status: {airbnb_rating_status}")
    if ll and ll.vrbo_rating is not None:
        review_lines.append(f"  VRBO Rating:   {vrbo_rating} — status: {vrbo_rating_status}")
    if ll and ll.vrbo_review_label:
        review_lines.append(f"  VRBO Review Label: {ll.vrbo_review_label}")
    if ll and any(v is not None for v in (ll.vrbo_cleanliness, ll.vrbo_checkin, ll.vrbo_communication, ll.vrbo_location)):
        review_lines.append(
            f"  VRBO Category Scores: Cleanliness {ll.vrbo_cleanliness if ll.vrbo_cleanliness is not None else 'n/a'}, "
            f"Check-in {ll.vrbo_checkin if ll.vrbo_checkin is not None else 'n/a'}, "
            f"Communication {ll.vrbo_communication if ll.vrbo_communication is not None else 'n/a'}, "
            f"Location {ll.vrbo_location if ll.vrbo_location is not None else 'n/a'}"
        )
    photo_lines = []
    if ll and ll.airbnb_photos is not None:
        photo_lines.append(f"  Airbnb Photos: {airbnb_photos} — {airbnb_photo_grade}")
    if ll and ll.vrbo_photos is not None:
        photo_lines.append(f"  VRBO Photos:   {vrbo_photos} — {vrbo_photo_grade}")
    review_photo_context = "\n".join(review_lines + photo_lines) if review_lines or photo_lines else "  No reliable review/rating/photo data synced yet."

    # Infer property type from name
    name_lower = prop.name.lower()
    if "studio" in name_lower or prop.bedrooms == 0:
        prop_type = "Studio"
    elif prop.bedrooms == 1:
        prop_type = "1-Bedroom Condo"
    elif prop.bedrooms == 2:
        prop_type = "2-Bedroom Condo"
    elif prop.bedrooms >= 3:
        prop_type = f"{prop.bedrooms}-Bedroom Home"
    else:
        prop_type = "Condo"

    area_desc = _group_label(prop) or prop.area or prop.city or "HVR Smokies"

    return f"""
You are a listing optimization analyst for HVR Smokies short-term rentals, similar to PriceLabs Listing Optimizer.
Analyze this listing and produce a practical listing-quality report for the dashboard. Today is {TODAY.isoformat()}.

Use the LISTING QUALITY RULEBOOK below as the source of truth. If the rulebook conflicts with a generic instinct,
follow the rulebook. Do not give vague advice. If source data is missing, keep it out of the score and put it under
Manual Review Needed instead of creating long NA sections.

Hard constraints:
- Do not generate a low score just because data is unsynced.
- Do not write repeated NA rows or sections.
- Score only synced evidence: titles, photo counts, thumbnails, ratings, reviews, VRBO category scores, links, and visible metadata.
- Put missing descriptions, amenities, photo order, cover image quality, and guest-favorite status under Manual Review Needed.
- For Smokies listings, prioritize experience amenities and OTA filters: hot tub, mountain views, indoor pool, game room, theater room, pet friendly, fire pit/outdoor lounge, EV charger, sauna/cold plunge, fast Wi-Fi/workstation, kid amenities, covered deck/grill, coffee bar/standout design, and wedding/event-friendly spaces.
- Treat indoor pool, exceptional mountain view, luxury outdoor space, theater + arcade combo, sauna/cold plunge, and unique design as the largest potential pricing-premium signals when they are supported by title/photo/amenity data.
- Treat pet friendly, hot tub, flexible cancellation, competitive cleaning fee, fast Wi-Fi, and game room as occupancy-driver checks. Do not claim they exist unless synced or visible.
- For cover-photo recommendations, favor hot tub with mountain view, indoor pool, sunset deck, dramatic cabin/A-frame exterior, or theater/game room lighting when the listing actually has that feature.
- If fewer than three evidence categories are synced, write "Not scored - insufficient synced listing data" instead of a numeric score.
- Do not recommend adding photos when photo count is unknown. Say "sync or manually check photo count and order" instead.
- Do not recommend rewriting a full description when the description is unsynced. Give a manual-review checklist instead.
- Do not use placeholder text like [Inferred], [NA], or bracketed template labels.
- Do not upgrade claims: "mountain view" is not "panoramic mountain view"; "pool access" is not "private indoor pool"; "near Gatlinburg" is not "walk to downtown".
- Count title characters accurately before criticizing title length.
- Do not claim promotional language unless the title actually contains sale, discount, special, deal, limited time, or excessive punctuation/symbols.
- For target segments, use real segment names such as Couples, Families, Remote Workers, Large Groups, or Leisure Travelers, and label them as inferred when needed.

LISTING QUALITY RULEBOOK:
{quality_rules if quality_rules else "No external rulebook file found; use the explicit rules in this prompt."}

PROPERTY DATA:
  Name: {prop.name}
  Type: {prop_type}
  Location: {area_desc}
  Bedrooms: {prop.bedrooms}
  Base Price: ${prop.base_price:.0f}/night  |  Min Price: {min_price}
  Min Stay: {min_stay}
  PMS ID: {pms_id}

OTA LISTING TITLES:
  Airbnb Title: "{airbnb_title}" ({len(airbnb_title)} characters)
  VRBO Title:   "{vrbo_title}" ({len(vrbo_title)} characters)
  Airbnb URL: {airbnb_url}
  VRBO URL:   {vrbo_url}

REVIEW / PHOTO DATA:
{review_photo_context}

PriceLabs SETTINGS (pricing context):
  Last Minute Discounting: {prop.last_minute}
  Long-term Pricing: {prop.long_term_pricing}
  Occupancy Pacing: {prop.occupancy_pacing}
  Gaps & Adjacencies: {prop.gaps_adjacencies}
  Demand Sensitivity: {prop.demand_sensitivity}%

BOOKING PERFORMANCE:
  Adj. Occupancy 60-day: {prop.adj_occ_60d:.0%}
  Booked nights next 7d / 14d: {prop.booked_7d} / {prop.booked_14d}
  Last booked: {f"{prop.last_booked_days} days ago" if prop.last_booked_days is not None else "no record"}
  Min-price hit rate 60d: {prop.min_price_occ_60d:.0%}

---

Produce this exact concise structure:

## Listing Optimizer

**Evidence-Based Quality Score:** X.X/10 or "Not scored - insufficient synced listing data"
Score only synced evidence. If fewer than three evidence categories are synced, do not give a numeric score.

### Top Fixes
Give 3-5 prioritized fixes. Each fix must be actionable and based on synced data or clearly marked "manual review". Do not call missing data a defect.

## Title Optimization

**Current**
- Airbnb: "{airbnb_title}"
- VRBO: "{vrbo_title}"

**Recommended**
- Airbnb: one improved title, 50 characters or fewer, with character count
- VRBO: one improved title, 70 characters or fewer, with character count

**Why**
Explain what the current title does well and exactly what to change.

## Photo And Visual Check
Use only synced photo counts and thumbnail availability.
- Airbnb photos: {airbnb_photos} ({airbnb_photo_grade})
- VRBO photos: {vrbo_photos} ({vrbo_photo_grade})

Say what the cover image and first 8 photos should be manually checked for. Do not invent photo quality.

## Reviews And Trust
Use only the explicit ratings, review counts, and VRBO category scores provided.
- Airbnb: {airbnb_rating} - {airbnb_rating_status}
- VRBO: {vrbo_rating} - {vrbo_rating_status}

Explain if review score/count is likely hurting conversion, or say it looks healthy if supported.

## Positioning
List likely guest segments and selling points supported by the listing name, bedroom count, group, ratings, or synced titles. Mark inferred items as inferred. For Smokies, explicitly check whether the listing is positioned for couples, families, large groups, pet owners, remote workers, luxury travelers, or event/wedding groups.

## Manual Review Needed
List only missing items that would materially improve the optimizer:
- full description
- amenities
- cover image quality
- photo order
- guest favorite / badge status
- platform consistency
- high-impact Smokies amenity filters and photo proof: hot tub, mountain view, indoor pool, game room, theater room, pet friendly, fire pit, covered deck/grill, EV charger, sauna/cold plunge, fast Wi-Fi/workstation, kid amenities, coffee bar/design feature

## Action Checklist
Return 5-8 checklist items the team can complete in Hostaway/OTA/PriceLabs Listing Optimizer.
""".strip()


def _build_portfolio_prompt(report_type: str) -> str:
    today = TODAY.isoformat()
    active = [p for p in _PORTFOLIO if p.active]
    critical = [p for p in active if p.urgency == "critical"]
    warning = [p for p in active if p.urgency == "warning"]

    # Keep critical list short — top 12 only, condensed format
    critical_summary = "\n".join(
        f"  {p.property_name} | {p.bedrooms}BR | occ60={p.adj_occ_60d:.0%} | bkd14={p.booked_14d} | base=${p.base_price:.0f} | {p.issues[0] if p.issues else ''}"
        for p in critical[:12]
    )

    if report_type == "portfolio":
        return f"""
You are an STR revenue manager. Today is {today}. Portfolio: {_SUMMARY['total_active']} active HVR Smokies / Tennessee vacation rentals.

STATS: Critical={_SUMMARY['critical_count']} | Warning={_SUMMARY['warning_count']} | OK={_SUMMARY['ok_count']}
Zero bookings next 14d: {_SUMMARY['zero_bookings_14d']} properties | Avg occ 60d: {_SUMMARY['avg_occ_60d']}%
LT pricing disabled: {_SUMMARY['long_term_pricing_disabled']}/{_SUMMARY['total_active']} | Occ pacing disabled: {_SUMMARY['occupancy_pacing_disabled']}/{_SUMMARY['total_active']}

TOP CRITICAL PROPERTIES:
{critical_summary}

Provide:
1. **Portfolio Health Score** (0–100) + one-line verdict
2. **Systemic Issues** — problems hitting 10+ properties, portfolio-wide fix for each
3. **Top 5 Critical Properties** — why critical, the single most important fix, revenue at stake
4. **3 Quick Wins** — changes in PriceLabs or Hostaway this week, broadest impact
5. **Revenue Projection** — if critical properties hit 70% occupancy, estimated monthly uplift

Rules:
- This is Tennessee / Smokies inventory, not Maui or Hawaii.
- Do not include a Settings Gaps section when settings are unknown or not synced.
""".strip()

    return _build_portfolio_prompt("portfolio")


def _clean_channel(value: object) -> str:
    text = str(value or "").strip()
    low = text.lower()
    if "airbnb" in low:
        return "Airbnb"
    if "vrbo" in low or "homeaway" in low:
        return "VRBO"
    if "booking" in low:
        return "Booking.com"
    if "direct" in low or "website" in low:
        return "Direct"
    return text or "Unknown"


def _extract_calendar_day(item: dict, fallback_date: date) -> dict:
    day_text = (
        item.get("date")
        or item.get("startDate")
        or item.get("calendarDate")
        or item.get("day")
        or fallback_date.isoformat()
    )
    status = str(item.get("status") or item.get("availability") or "").strip().lower()
    reservation = item.get("reservation") if isinstance(item.get("reservation"), dict) else {}
    reservation_id = (
        item.get("reservationId")
        or item.get("reservation_id")
        or reservation.get("id")
        or item.get("bookingId")
    )
    channel = _clean_channel(
        item.get("channelName")
        or item.get("channel")
        or item.get("source")
        or reservation.get("channelName")
        or reservation.get("channel")
    )
    rate = item.get("price") or item.get("rate") or item.get("nightlyRate") or item.get("amount")
    try:
        rate = round(float(str(rate).replace("$", "").replace(",", "")), 2)
    except (TypeError, ValueError):
        rate = None
    is_booked = bool(reservation_id) or status in {"reserved", "booked", "unavailable"}
    return {
        "date": str(day_text)[:10],
        "status": "booked" if is_booked else "open",
        "channel": channel if is_booked else "",
        "reservation_id": str(reservation_id or ""),
        "rate": rate,
        "guest": reservation.get("guestName") or item.get("guestName") or "",
    }


def _revenue_calendar(prop: Property, days: int = 30) -> dict:
    start = TODAY
    rate_by_date = {}
    for day_text, rate in prop.calendar_rates:
        rate_by_date[day_text] = rate
    fallback_days = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        rate = rate_by_date.get(day.isoformat())
        fallback_days.append({
            "date": day.isoformat(),
            "status": "unknown" if rate is None else "rate_only",
            "channel": "",
            "reservation_id": "",
            "rate": rate,
            "guest": "",
        })
    return {
        "ok": True,
        "source": "PriceLabs calendar export",
        "property": prop.name,
        "days": fallback_days,
        "message": "This data is available in PriceLabs Booking Insights. Export the PriceLabs CSV from this Booking Insights or detailed report view and upload it here so the dashboard can track monthly revenue, occupancy, ADR, and OTA/channel detail when included.",
    }


def _fmt_money_api(value: object) -> str:
    try:
        if value is None:
            return "-"
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct_api(value: object, signed: bool = False) -> str:
    try:
        if value is None:
            return "-"
        val = float(value)
        prefix = "+" if signed and val > 0 else ""
        return f"{prefix}{val * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _match_action_to_property(action: dict, prop: Property) -> bool:
    def key(value: object) -> str:
        text = str(value or "").split(" -- ")[0].split(": Default")[0]
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    action_keys = [key(action.get("property")), key(action.get("display_name"))]
    prop_keys = [key(prop.name), key(prop.property_name)]
    return any(a and p and (a == p or a in p or p in a) for a in action_keys for p in prop_keys)


def _revenue_insights(prop: Property) -> dict:
    actions = [a for a in _load_actions() if _is_monthly_pacing_action(a)]
    if not actions and MONTHLY_PACING_PATH.exists():
        actions, _summary = _generate_monthly_pacing_actions()

    matched = [a for a in actions if _match_action_to_property(a, prop)]
    primary = matched[0] if matched else None
    metrics = primary.get("monthly_metrics", {}) if primary else {}
    revenue = metrics.get("rental_revenue")
    revenue_stly = metrics.get("rental_revenue_stly")
    revenue_yoy = metrics.get("rental_revenue_yoy")
    occ = metrics.get("paid_occupancy")
    occ_stly = metrics.get("paid_occupancy_stly")
    occ_gap = metrics.get("paid_occupancy_gap")
    revpar = metrics.get("revpar")
    market_revpar = metrics.get("market_revpar")
    adr = prop.base_price or None

    if primary:
        signal = primary.get("monthly_signal", "monitor")
        recommendation = primary.get("suggestion") or primary.get("proposed_value") or "Monitor monthly pacing"
        reason = primary.get("reason", "")
        target_dates = primary.get("target_dates") or "Report Builder range"
    elif prop.urgency == "overperforming":
        signal = "overperforming"
        recommendation = "Fast pace: protect ADR and review selective future-date increases"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy and {prop.booked_14d} booked nights in the next 14 days."
        target_dates = "PriceLabs portfolio export"
    elif prop.urgency in {"critical", "warning"}:
        signal = "behind_occupancy"
        recommendation = "Behind occupancy: inspect open dates, OTA visibility, and price position"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy and {prop.booked_14d} booked nights in the next 14 days."
        target_dates = "PriceLabs portfolio export"
    else:
        signal = "monitor"
        recommendation = "Monitor next refresh"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy."
        target_dates = "PriceLabs portfolio export"

    rows = []
    if primary:
        rows.append({
            "period": target_dates,
            "revenue": _fmt_money_api(revenue),
            "revenue_delta": _fmt_pct_api(revenue_yoy, True),
            "occupancy": _fmt_pct_api(occ),
            "occupancy_delta": _fmt_pct_api(occ_gap, True),
            "adr": _fmt_money_api(adr),
            "revpar": _fmt_money_api(revpar),
            "market_revpar": _fmt_money_api(market_revpar),
            "signal": signal,
        })

    return {
        "ok": True,
        "property": prop.name,
        "source": "PriceLabs Report Builder" if primary else "PriceLabs portfolio export",
        "has_report_builder": bool(primary),
        "signal": signal,
        "recommendation": recommendation,
        "reason": reason,
        "target_dates": target_dates,
        "kpis": {
            "revenue": _fmt_money_api(revenue),
            "revenue_stly": _fmt_money_api(revenue_stly),
            "revenue_yoy": _fmt_pct_api(revenue_yoy, True),
            "occupancy": _fmt_pct_api(occ if occ is not None else prop.adj_occ_60d),
            "occupancy_stly": _fmt_pct_api(occ_stly),
            "occupancy_gap": _fmt_pct_api(occ_gap, True),
            "adr": _fmt_money_api(adr),
            "revpar": _fmt_money_api(revpar),
            "market_revpar": _fmt_money_api(market_revpar),
        },
        "rows": rows,
        "message": "" if primary else "PriceLabs Booking Insights is available in the PriceLabs UI, but its CSV has not been loaded for this listing yet. Showing portfolio pacing signals until the PriceLabs export is uploaded.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", summary=_SUMMARY, today=TODAY.isoformat())


def _persist_upload(file_storage, local_path: Path, remote_name: str) -> None:
    """Save an uploaded file locally (best-effort) and mirror to Supabase Storage.

    The local write is wrapped in try/except so the read-only Vercel filesystem
    does not break the request when Supabase Storage is the durable target.
    """
    body = file_storage.read()
    try:
        local_path.write_bytes(body)
    except OSError:
        pass
    if supabase_store.is_enabled():
        supabase_store.upload_csv(remote_name, body)


@app.route("/api/reload", methods=["POST"])
def reload_data():
    """
    Accept uploaded CSV files and hot-reload portfolio data without restart.
    Form fields:  pricelabs_csv (file, optional)
                  marketing_csv  (file, optional)
                  report_builder_csv (file, optional)
    """
    updated = []
    if "pricelabs_csv" in request.files or "wheelhouse_csv" in request.files:
        f = request.files.get("pricelabs_csv") or request.files["wheelhouse_csv"]
        if f.filename:
            _persist_upload(f, CSV_PATH, "pricelabs_portfolio.csv")
            updated.append("pricelabs")

    if "marketing_csv" in request.files:
        f = request.files["marketing_csv"]
        if f.filename:
            _persist_upload(f, MARKETING_PATH, "marketing_links.csv")
            updated.append("marketing")

    if "report_builder_csv" in request.files:
        f = request.files["report_builder_csv"]
        if f.filename:
            _persist_upload(f, MONTHLY_PACING_PATH, "pricelabs_report_builder_monthly.csv")
            updated.append("report_builder")

    if updated:
        _reload_portfolio()
        return jsonify({
            "ok": True,
            "updated": updated,
            "summary": _SUMMARY,
        })
    return jsonify({"ok": False, "error": "No files uploaded"}), 400


@app.route("/api/summary")
def get_summary():
    """Return current portfolio summary stats (for after a reload)."""
    return jsonify(_SUMMARY)


@app.route("/api/actions")
def get_actions():
    actions = _load_actions()
    if request.args.get("generate") == "1":
        actions = _generate_monthly_pacing_actions()[0] if request.args.get("source") == "monthly_pacing" else _generate_weekly_actions()
    source = request.args.get("source")
    if source == "weekly":
        actions = [
            a for a in actions
            if not _is_pace_year_action(a) and not _is_monthly_pacing_action(a)
        ]
    elif source == "monthly_pacing":
        actions = [a for a in actions if _is_monthly_pacing_action(a)]
    elif source == "pace_year":
        actions = []
    status = request.args.get("status")
    if status:
        actions = [a for a in actions if a.get("status") == status]
    return jsonify({"ok": True, "actions": actions})


@app.route("/api/monthly-pacing")
def monthly_pacing():
    has_csv = MONTHLY_PACING_PATH.exists()
    try:
        if request.args.get("generate") == "1":
            actions, summary = _generate_monthly_pacing_actions()
            return jsonify({"ok": True, "actions": actions, "summary": summary, "has_csv": has_csv})

        result = load_monthly_pacing(MONTHLY_PACING_PATH, _PORTFOLIO, TODAY)
        actions = [a for a in _load_actions() if _is_monthly_pacing_action(a)]
        if not actions and has_csv:
            actions, summary = _generate_monthly_pacing_actions()
            result["summary"] = summary
        if actions:
            result["actions"] = actions
        result["has_csv"] = has_csv
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "has_csv": has_csv, "actions": [], "summary": {}}), 500


@app.route("/api/kcity-surge")
def kcity_surge():
    try:
        result = generate_dso_tasks(_PORTFOLIO, TODAY)
        existing = _load_actions()
        existing_ids = {a["id"] for a in existing if a.get("source") == KCITY_DSO_SOURCE}
        for action in result["actions"]:
            matched = next((a for a in existing if a["id"] == action["id"]), None)
            if matched:
                action["status"] = matched.get("status", "pending")
                action["reviewed_at"] = matched.get("reviewed_at")
                action["apply_result"] = matched.get("apply_result")
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "actions": [], "summary": {}}), 500


@app.route("/api/actions/<action_id>", methods=["POST"])
def update_action(action_id: str):
    payload = request.get_json(silent=True) or {}
    new_status = payload.get("status")
    if new_status not in {"approved", "rejected", "pending", "applied"}:
        return jsonify({"ok": False, "error": "status must be approved, rejected, pending, or applied"}), 400
    apply_now = bool(payload.get("apply"))

    actions = _load_actions()
    for action in actions:
        if action.get("id") == action_id:
            action["status"] = new_status
            action["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            if new_status == "applied" and not apply_now:
                action["applied_at"] = datetime.now(timezone.utc).isoformat()
            if new_status == "approved" and apply_now:
                try:
                    result = _finalize_pricelabs_push(_apply_weekly_action(action))
                    action["status"] = "applied"
                    action["applied_at"] = datetime.now(timezone.utc).isoformat()
                    action["apply_result"] = result
                    adjusted_start = result.get("adjusted_start_date") if isinstance(result, dict) else None
                    if adjusted_start:
                        wp = action.get("pricelabs_payload") or {}
                        old_start = wp.get("start_date")
                        wp["start_date"] = adjusted_start
                        action["pricelabs_payload"] = wp
                        action["target_dates"] = action.get("target_dates", "").replace(str(old_start), adjusted_start)
                        post_apply = result.get("post_apply") if isinstance(result, dict) else {}
                        verified_sync = (post_apply or {}).get("verified_sync") or {}
                        refresh = (post_apply or {}).get("refresh") or {}
                        sync_message = verified_sync.get("message") or "Dashboard re-sync status unknown."
                        refresh_message = refresh.get("message") or ""
                        action["apply_note"] = (
                            f"Start date adjusted from {old_start} to {adjusted_start} "
                            f"to preserve same-day booking protection. {sync_message} {refresh_message}"
                        ).strip()
                    else:
                        confirmed_base = result.get("confirmed_base") if isinstance(result, dict) else None
                        confirmed = result.get("confirmed_dates") if isinstance(result, dict) else None
                        post_apply = result.get("post_apply") if isinstance(result, dict) else {}
                        verified_sync = (post_apply or {}).get("verified_sync") or {}
                        refresh = (post_apply or {}).get("refresh") or {}
                        sync_message = verified_sync.get("message") or "Dashboard re-sync status unknown."
                        refresh_message = refresh.get("message") or ""
                        if confirmed_base:
                            action["apply_note"] = f"PriceLabs confirmed base price update to ${confirmed_base}. {sync_message} {refresh_message}".strip()
                        elif confirmed:
                            action["apply_note"] = f"PriceLabs confirmed {len(confirmed)} date override(s): {', '.join(confirmed[:3])}{'...' if len(confirmed) > 3 else ''}. {sync_message} {refresh_message}".strip()
                        else:
                            action["apply_note"] = f"PriceLabs accepted the override request. {sync_message} {refresh_message}".strip()
                except PricingApplyError as e:
                    action["status"] = "approved"
                    action["apply_error"] = str(e)
                    _save_actions(actions)
                    return jsonify({"ok": False, "error": str(e), "action": action}), 400
            _save_actions(actions)
            return jsonify({"ok": True, "action": action})
    return jsonify({"ok": False, "error": "Action not found"}), 404


@app.route("/api/revenue-calendar")
def revenue_calendar():
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found", "days": []}), 404
    return jsonify(_revenue_calendar(prop))


@app.route("/api/revenue-insights")
def revenue_insights():
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found"}), 404
    return jsonify(_revenue_insights(prop))


@app.route("/api/booking-promotions")
def get_booking_promotions():
    promotions = _generate_booking_promotions() if request.args.get("generate") == "1" else _load_booking_promotions()
    if not promotions:
        promotions = _generate_booking_promotions()
    property_name = request.args.get("property", "")
    if property_name:
        promotions = [p for p in promotions if p.get("property") == property_name]
    status = request.args.get("status", "")
    if status and status != "all":
        promotions = [p for p in promotions if p.get("status", "draft") == status]
    return jsonify({"ok": True, "promotions": promotions})


@app.route("/api/booking-promotions/<promotion_id>", methods=["POST"])
def update_booking_promotion(promotion_id: str):
    payload = request.get_json(silent=True) or {}
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    editable = {
        "promotion_type",
        "discount_pct",
        "booking_hotel_id",
        "booking_room_ids",
        "booking_parent_rate_ids",
        "book_start_date",
        "book_end_date",
        "stay_start_date",
        "stay_end_date",
        "audience",
        "status",
    }
    for promo in promotions:
        if promo.get("id") != promotion_id:
            continue
        for key in editable:
            if key in payload:
                promo[key] = payload[key]
        if "discount_pct" in payload:
            try:
                promo["discount_pct"] = max(0, min(50, int(float(payload["discount_pct"]))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "discount_pct must be numeric"}), 400
        promo["booking_room_ids"] = _split_ids(promo.get("booking_room_ids"))
        promo["booking_parent_rate_ids"] = _split_ids(promo.get("booking_parent_rate_ids"))
        if promo.get("status") not in {"draft", "needs_review", "approved", "rejected", "applied"}:
            return jsonify({"ok": False, "error": "Invalid promotion status"}), 400
        promo["updated_at"] = datetime.now(timezone.utc).isoformat()
        prop = _PORTFOLIO_INDEX.get(promo.get("property", ""))
        if prop:
            discount = int(promo.get("discount_pct") or 0)
            promo["expected_adr"] = _round_to_5(prop.base_price * (1 - discount / 100)) if discount else prop.base_price
            promo["risk_flags"] = _booking_promo_risks(prop, discount, promo.get("promotion_type") or "none")
            try:
                promo["api_payload_preview"] = _booking_payload_preview(
                    prop,
                    promo.get("promotion_type") or "none",
                    discount,
                    date.fromisoformat(promo["book_start_date"]),
                    date.fromisoformat(promo["book_end_date"]),
                    date.fromisoformat(promo["stay_start_date"]),
                    date.fromisoformat(promo["stay_end_date"]),
                )
            except (KeyError, ValueError):
                return jsonify({"ok": False, "error": "Promotion dates must use YYYY-MM-DD"}), 400
        _save_booking_promotions(promotions)
        return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/booking-promotions/<promotion_id>/review", methods=["POST"])
def review_booking_promotion(promotion_id: str):
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    for promo in promotions:
        if promo.get("id") == promotion_id:
            promo["ai_review"] = _booking_promotion_review(promo)
            promo["status"] = "needs_review" if promo.get("status") == "draft" else promo.get("status", "needs_review")
            promo["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            _save_booking_promotions(promotions)
            return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/booking-promotions/<promotion_id>/push", methods=["POST"])
def push_booking_promotion(promotion_id: str):
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    for promo in promotions:
        if promo.get("id") != promotion_id:
            continue
        if promo.get("status") != "approved":
            return jsonify({"ok": False, "error": "Promotion must be approved before pushing to Booking.com."}), 400
        try:
            xml_body = build_promotion_xml(promo)
            result = booking_client_from_env().create_promotion(xml_body)
        except BookingAPIError as e:
            promo["push_error"] = str(e)
            promo["pushed_at"] = None
            promo["api_xml_preview"] = None
            try:
                promo["api_xml_preview"] = build_promotion_xml(promo)
            except BookingAPIError:
                pass
            _save_booking_promotions(promotions)
            return jsonify({"ok": False, "error": str(e), "promotion": promo}), 400
        promo["status"] = "applied"
        promo["pushed_at"] = datetime.now(timezone.utc).isoformat()
        promo["booking_push_result"] = result
        promo["booking_promotion_ids"] = result.get("promotion_ids", [])
        promo["push_error"] = ""
        promo["api_xml_sent"] = xml_body
        _save_booking_promotions(promotions)
        return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    """Bulk-refresh dashboard data from PriceLabs Customer API."""
    if not os.environ.get("PRICELABS_API_KEY"):
        return jsonify({"ok": False, "error": "PRICELABS_API_KEY is not set. Add it to your .env file and restart the app.", "summary": _SUMMARY}), 400
    try:
        result = _sync_pricelabs_api()
        return jsonify({
            "ok": True,
            "output": (
                f"Synced {result['listings_total']} PriceLabs listings from the Customer API; "
                f"{result['listings_active']} are active/syncing and loaded into the dashboard."
            ),
            "sync": result,
            "summary": _SUMMARY,
        })
    except PriceLabsAPIError as e:
        return jsonify({"ok": False, "error": str(e), "summary": _SUMMARY}), 400
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, flush=True)
        return jsonify({"ok": False, "error": f"Sync failed: {e}", "traceback": tb, "summary": _SUMMARY}), 500


@app.route("/api/hostaway/revenue")
def hostaway_revenue():
    """Return reservations, monthly totals, and YTD stats for a listing from Hostaway."""
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found"}), 404
    lid = str(prop.listing_id or "").strip()
    if not lid:
        return jsonify({"ok": False, "error": "No listing ID for this property"}), 400
    try:
        client = hostaway_client_from_env()
        reservations = client.reservations_for_listing(lid, limit=200)
    except (HostawayAPIError, Exception) as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    today = date.today()
    current_year = today.year

    # Build reservation rows
    rows = []
    monthly: dict[str, dict] = {}
    ytd_revenue = 0.0
    ytd_nights = 0

    for r in reservations:
        arrival = (r.get("arrivalDate") or r.get("checkIn") or "")[:10]
        departure = (r.get("departureDate") or r.get("checkOut") or "")[:10]
        booked = (r.get("createdAt") or r.get("bookingDate") or "")[:10]
        if not arrival:
            continue

        # Financial data - try multiple field names Hostaway uses
        money = r.get("money") or {}
        rental_rev = float(money.get("rentalRevenue") or r.get("rentalRevenue") or r.get("totalPrice") or r.get("totalAmount") or 0)
        total_rev = float(money.get("totalPrice") or money.get("totalAmount") or r.get("totalPrice") or r.get("totalAmount") or rental_rev)
        cleaning = float(money.get("cleaningFee") or r.get("cleaningFee") or 0)
        channel_fee = float(money.get("channelFee") or money.get("channelCommission") or r.get("channelFee") or 0)

        los = 0
        if arrival and departure:
            try:
                los = (date.fromisoformat(departure) - date.fromisoformat(arrival)).days
            except (ValueError, TypeError):
                pass
        adr = round(rental_rev / los, 2) if los > 0 and rental_rev > 0 else 0

        source = str(r.get("channelName") or r.get("source") or r.get("channel") or "direct").strip()
        guest_count = r.get("guestCount") or r.get("numberOfGuests") or 1

        rows.append({
            "booked_date": booked,
            "check_in": arrival,
            "check_out": departure,
            "los": los,
            "rental_revenue": rental_rev,
            "total_revenue": total_rev,
            "cleaning_fee": cleaning,
            "channel_fee": channel_fee,
            "adr": adr,
            "source": source,
            "guest_count": guest_count,
            "reservation_id": r.get("id") or r.get("reservationId") or "",
        })

        # Monthly aggregation (by check-in month of current year)
        if arrival.startswith(str(current_year)):
            month_key = arrival[:7]  # YYYY-MM
            if month_key not in monthly:
                monthly[month_key] = {"revenue": 0.0, "nights": 0, "reservations": 0}
            monthly[month_key]["revenue"] += rental_rev
            monthly[month_key]["nights"] += los
            monthly[month_key]["reservations"] += 1

        # YTD (current year arrivals up to today)
        if arrival.startswith(str(current_year)) and arrival <= today.isoformat():
            ytd_revenue += rental_rev
            ytd_nights += los

    # Build monthly table for current year
    monthly_rows = []
    for month_num in range(1, 13):
        key = f"{current_year}-{month_num:02d}"
        m = monthly.get(key, {})
        nights = m.get("nights", 0)
        rev = m.get("revenue", 0.0)
        days_in_month = [31,28,29,31,30,31,30,31,31,30,31,30,31][month_num] if (current_year % 4 == 0 and month_num == 2) else [31,28,31,30,31,30,31,31,30,31,30,31][month_num - 1]
        occ = round(nights / days_in_month * 100) if nights else 0
        adr = round(rev / nights, 2) if nights else 0
        monthly_rows.append({
            "month": date(current_year, month_num, 1).strftime("%b %Y"),
            "revenue": round(rev, 2),
            "nights": nights,
            "reservations": m.get("reservations", 0),
            "occupancy": occ,
            "adr": adr,
            "is_current": month_num == today.month,
        })

    # Calendar: next 3 months
    calendar_months = []
    for offset in range(3):
        m = (today.month - 1 + offset) % 12 + 1
        y = current_year + (today.month - 1 + offset) // 12
        import calendar as cal_mod
        cal_days = cal_mod.monthcalendar(y, m)
        booked_dates = {r["check_in"] for r in rows if r["check_in"] and r["check_in"] >= today.isoformat()}
        calendar_months.append({
            "year": y, "month": m,
            "name": date(y, m, 1).strftime("%B %Y"),
            "weeks": cal_days,
            "booked": list(booked_dates),
        })

    return jsonify({
        "ok": True,
        "property": prop.name,
        "listing_id": lid,
        "reservations": rows[:50],
        "monthly": monthly_rows,
        "ytd_revenue": round(ytd_revenue, 2),
        "ytd_nights": ytd_nights,
        "calendar_months": calendar_months,
        "year": current_year,
    })


@app.route("/api/portfolio")
def get_portfolio():
    urgency_filter = request.args.get("urgency", "all")
    props = [p for p in _PORTFOLIO if p.active]

    if urgency_filter == "critical":
        props = [p for p in props if p.urgency == "critical"]
    elif urgency_filter == "warning":
        props = [p for p in props if p.urgency == "warning"]
    elif urgency_filter == "overperforming":
        props = [p for p in props if p.urgency == "overperforming"]
    elif urgency_filter == "onboarding":
        props = [p for p in props if p.urgency == "onboarding"]
    elif urgency_filter == "ok":
        props = [p for p in props if p.urgency == "ok"]

    def _prop_dict(p: Property) -> dict:
        ll = lookup_links(p.name)
        benchmark = _benchmark_for(p)
        return {
            "name": p.name,
            "display_name": p.property_name,
            "area": p.area,
            "side": p.side,
            "city": getattr(p, "city", ""),
            "group": getattr(p, "customization_group", ""),
            "subgroup": getattr(p, "customization_sub_group", ""),
            "group_label": _group_label(p),
            "bedrooms": p.bedrooms,
            "base_price": p.base_price,
            "min_price": p.min_price,
            "booked_7d": p.booked_7d,
            "booked_14d": p.booked_14d,
            "adj_occ_60d": round(p.adj_occ_60d * 100, 1),
            "last_booked_days": p.last_booked_days,
            "urgency": p.urgency,
            "urgency_score": p.urgency_score,
            "issues": p.issues,
            "owner_restrictions": p.owner_restrictions,
            "benchmark": benchmark,
            # OTA links
            "airbnb_url":      ll.airbnb_url if ll else None,
            "vrbo_url":        ll.vrbo_url if ll else None,
            "streamline_id":   ll.streamline_id if ll else None,
            "airbnb_headline": ll.airbnb_headline if ll else None,
            "airbnb_id_issue": ll.airbnb_id_issue if ll else "",
            "vrbo_headline":   ll.vrbo_headline if ll else None,
            "has_airbnb":      bool(ll and ll.has_airbnb),
            "has_vrbo":        bool(ll and ll.has_vrbo),
            "booking_url":     ll.booking_url if ll else None,
            "has_booking":     bool(ll and ll.has_booking),
            "airbnb_rating":   ll.airbnb_rating if ll else None,
            "airbnb_reviews":  ll.airbnb_reviews if ll else None,
            "airbnb_rating_status": ll.airbnb_rating_status if ll else "unknown",
            "vrbo_rating":     ll.vrbo_rating if ll else None,
            "vrbo_reviews":    ll.vrbo_reviews if ll else None,
            "vrbo_rating_status": ll.vrbo_rating_status if ll else "unknown",
            "vrbo_review_label": ll.vrbo_review_label if ll else "",
            "vrbo_cleanliness": ll.vrbo_cleanliness if ll else None,
            "vrbo_checkin": ll.vrbo_checkin if ll else None,
            "vrbo_communication": ll.vrbo_communication if ll else None,
            "vrbo_location": ll.vrbo_location if ll else None,
            "airbnb_photos":   ll.airbnb_photos if ll else None,
            "airbnb_thumb_url": ll.airbnb_thumb_url if ll else "",
            "airbnb_photo_grade": ll.airbnb_photo_grade if ll else "unknown",
            "vrbo_photos":     ll.vrbo_photos if ll else None,
            "vrbo_thumb_url":  ll.vrbo_thumb_url if ll else "",
            "vrbo_photo_grade": ll.vrbo_photo_grade if ll else "unknown",
        }

    return jsonify({
        "summary": _SUMMARY,
        "properties": [_prop_dict(p) for p in props],
    })


@app.route("/api/report")
def stream_report():
    report_type = request.args.get("type", "portfolio")
    property_name = request.args.get("property", "")
    resolved_prop = _resolve_property(property_name)

    # Resolve property and build prompt
    if resolved_prop:
        prop = resolved_prop
        prompt = _build_property_prompt(report_type, prop)
    else:
        prompt = _build_portfolio_prompt(report_type)

    def generate():
        try:
            client = _ai_client()

            SHORT_SYSTEM = (
                "You are an expert STR revenue manager and listing optimizer for HVR Smokies vacation rentals. "
                "Be specific, data-driven, and actionable. Use the exact data provided. "
                "Do not call tools or functions. Format responses in clean markdown. "
                "Do not recommend maximum rates, max prices, price caps, or price ceilings. "
                "Do not recommend same-day rate edits, and never recommend editing Last Minute adjustment settings. "
                "Never use Maui or Hawaii market assumptions; this portfolio is Tennessee Smokies and nearby cities. "
                "Skip sections where synced data is unknown instead of filling them with unknowns. "
                "Keep the answer concise: focus on the highest-impact issues and actions."
            )

            messages: list[dict] = [
                {"role": "system", "content": SHORT_SYSTEM},
                {"role": "user",   "content": prompt},
            ]

            def call_with_fallback(msgs):
                """Try each model in the cascade until one succeeds."""
                models = AI_MODEL_FALLBACKS
                for model in models:
                    try:
                        kwargs = dict(
                            model=model,
                            messages=msgs,
                            max_tokens=2200 if report_type == "listing" else 1200,
                            temperature=0.2 if report_type == "listing" else 0.4,
                        )
                        return client.chat.completions.create(**kwargs), model
                    except AIAPIStatusError as e:
                        if e.status_code in (429, 413):
                            continue   # rate limit or too large — try next model
                        raise
                raise RuntimeError("All models hit rate limit — try again in a few minutes")

            response, used_model = call_with_fallback(messages)
            text = response.choices[0].message.content or ""
            if resolved_prop and resolved_prop.urgency in {"critical", "warning"}:
                risky = re.search(r"\bincrease\b.*\b(base|min(?:imum)?|rate|price)\b", text, flags=re.I | re.S)
                if risky:
                    text += (
                        "\n\n## Guardrail Correction\n\n"
                        f"This listing is {resolved_prop.urgency.upper()}, so do not increase base price, "
                        "minimum price, or nightly rates from this report. Treat any earlier increase language "
                        "as rejected. Use forward price-position checks first: posted percentile, market booked "
                        "price, last-year booked price, market posted price, restrictions, and channel visibility."
                    )
            if resolved_prop and re.search(r"\b(decrease|increase|change|adjust|reduce|raise)\b.*\b(minimum price|min price|price floor|floor)\b", text, flags=re.I | re.S):
                text += (
                    "\n\n## Minimum Price Guardrail\n\n"
                    "Do not change minimum price or floor from this report. Floor changes require a separate "
                    "owner/portfolio review. For revenue recovery, use date-level forward pacing: compare posted "
                    "price percentile, market booked price, last-year booked price, and market posted price before "
                    "choosing hold, narrow rate adjustment, Booking.com promo, or channel visibility work."
                )
            chunk = 80
            if report_type == "listing" and resolved_prop:
                listing_html = _listing_optimizer_html(resolved_prop)
                yield f"data: {json.dumps({'type': 'html', 'html': listing_html})}\n\n"
                for i in range(0, len(text), chunk):
                    yield f"data: {json.dumps({'type': 'ai_text', 'text': text[i:i+chunk]})}\n\n"
            else:
                for i in range(0, len(text), chunk):
                    yield f"data: {json.dumps({'type': 'text', 'text': text[i:i+chunk]})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            if resolved_prop:
                fallback = _local_property_report(report_type, resolved_prop, e)
                chunk = 80
                if report_type == "listing":
                    listing_html = _listing_optimizer_html(resolved_prop)
                    yield f"data: {json.dumps({'type': 'html', 'html': listing_html})}\n\n"
                    for i in range(0, len(fallback), chunk):
                        yield f"data: {json.dumps({'type': 'ai_text', 'text': fallback[i:i+chunk]})}\n\n"
                else:
                    for i in range(0, len(fallback), chunk):
                        yield f"data: {json.dumps({'type': 'text', 'text': fallback[i:i+chunk]})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    if not _get_ai_api_key():
        print(
            f"WARNING: none of {', '.join(AI_API_KEY_ENV_VARS)} are set. Dashboard will run, but AI reports use fallback/error handling.",
            file=sys.stderr,
        )
    active_count = _SUMMARY["total_active"]
    critical_count = _SUMMARY["critical_count"]
    print(f"Starting STR Portfolio Dashboard at http://localhost:8080")
    print(f"Portfolio: {active_count} active properties · {critical_count} critical")
    app.run(debug=False, port=8080, threaded=True, use_reloader=False)
