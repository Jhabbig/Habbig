#!/usr/bin/env python3
"""
Economic calendar — upcoming macro events with crypto-impact tagging.

Why we need this on top of macro.py:
  macro.py tracks the *price series* (DXY, US10Y, VIX, M2, gold). This
  module tracks the *event schedule* — FOMC dates, CPI release dates,
  NFP, PCE. Knowing the price doesn't tell you when the next shock lands.

Data source:
  FRED's /fred/releases/dates endpoint returns the publication schedule
  for every economic release the Fed tracks (~300 releases). We classify
  each by name pattern into high/medium/low impact + a per-event
  crypto-impact score derived from historical correlation studies.

Why not Trading Economics / Forex Factory / Investing.com:
  Trading Economics free tier rate-limits to ~20 calls/day. FF and IC
  don't have official APIs. FRED is free, RFC-2616-friendly, and already
  in our integration footprint (FRED_API_KEY env var).

Notable events the FRED endpoint doesn't surface cleanly:
  - FOMC meeting *start* (day 1) — only the decision day (day 2) is a
    release. We don't track day 1 since the market doesn't react until
    the decision drops.
  - Treasury auctions — separate Treasury Direct feed, future work.
  - Fed governor speeches — not in FRED's release calendar. Future work
    via a Fed RSS scrape.

Per-release time-of-day is hardcoded by event type since FRED only
returns dates. CPI / NFP / PCE / GDP all drop at 8:30 AM ET (12:30 UTC).
FOMC decisions at 2:00 PM ET (18:00 UTC).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import database as db

log = logging.getLogger("crypto.econ_calendar")

FRED_BASE = "https://api.stlouisfed.org/fred"


# ─── Impact classification ──────────────────────────────────────────────────

# Names of releases we consider HIGH impact for crypto. Matched
# case-insensitively against the FRED release_name field, with word
# boundaries to avoid false positives ("CPI" alone vs "CPI for Tokyo").
HIGH_IMPACT_PATTERNS = [
    r"\bFOMC\b",
    r"\bCPI\b",                              # Consumer Price Index
    r"\bConsumer Price Index\b",
    r"\bEmployment Situation\b",             # Non-Farm Payrolls
    r"\bPersonal Consumption Expenditures\b",  # PCE inflation — Fed's preferred gauge
    r"\bPCE\b",
    r"\bGross Domestic Product\b",
    r"\bNon-?Farm Payroll",
    r"\bJobless Claims\b",
    r"\bFederal Open Market Committee\b",
    r"\bH\.4\.1",                             # Fed balance sheet (weekly)
]

MEDIUM_IMPACT_PATTERNS = [
    r"\bIndustrial Production\b",
    r"\bRetail Trade\b",
    r"\bRetail Sales\b",
    r"\bProducer Price Index\b",
    r"\bPPI\b",
    r"\bHousing Starts\b",
    r"\bDurable Goods\b",
    r"\bConsumer Sentiment\b",
    r"\bBeige Book\b",
    r"\bTrade Balance\b",
    r"\bISM\b",
    r"\bPMI\b",
]

# Crypto-impact weight (0..1). Drawn from historical move-on-release
# studies: FOMC decisions and CPI prints move BTC ~3% on average; NFP
# and PCE ~1.5%; everything else < 1%. The weight is a coarse heuristic
# the UI uses to colour-rank events.
CRYPTO_IMPACT_WEIGHTS = [
    (r"\bFOMC\b|\bFederal Open Market Committee\b", 1.00),
    (r"\bConsumer Price Index\b|\bCPI\b",            0.90),
    (r"\bEmployment Situation\b|\bNon-?Farm",        0.75),
    (r"\bPersonal Consumption Expenditures\b|\bPCE\b", 0.70),
    (r"\bGross Domestic Product\b|\bGDP\b",          0.55),
    (r"\bJobless Claims\b",                          0.40),
    (r"\bH\.4\.1",                                   0.40),  # Fed balance sheet
    (r"\bProducer Price Index\b|\bPPI\b",            0.35),
    (r"\bRetail Trade\b|\bRetail Sales\b",           0.30),
    (r"\bIndustrial Production\b",                   0.25),
    (r"\bHousing Starts\b|\bDurable Goods\b",        0.20),
    (r"\bConsumer Sentiment\b",                      0.20),
    (r"\bISM\b|\bPMI\b",                             0.30),
    (r"\bBeige Book\b",                              0.30),
]

# Time-of-day map: most US macro releases drop at 8:30 AM ET (12:30 UTC);
# FOMC decisions at 2:00 PM ET (18:00 UTC); H.4.1 at 4:30 PM ET (20:30
# UTC) on Thursdays. We add the time component so the UI can render an
# accurate countdown.
RELEASE_TIME_UTC = {
    "fomc":      (18, 0),
    "cpi":       (12, 30),
    "ppi":       (12, 30),
    "nfp":       (12, 30),
    "pce":       (12, 30),
    "gdp":       (12, 30),
    "retail":    (12, 30),
    "industrial": (13, 15),
    "claims":    (12, 30),
    "fed_h41":   (20, 30),
    "default":   (12, 30),
}


def _impact_for(name: str) -> str:
    if not name:
        return "low"
    for pat in HIGH_IMPACT_PATTERNS:
        if re.search(pat, name, re.IGNORECASE):
            return "high"
    for pat in MEDIUM_IMPACT_PATTERNS:
        if re.search(pat, name, re.IGNORECASE):
            return "medium"
    return "low"


def _crypto_impact_for(name: str) -> float:
    if not name:
        return 0.0
    for pat, weight in CRYPTO_IMPACT_WEIGHTS:
        if re.search(pat, name, re.IGNORECASE):
            return weight
    return 0.0


def _time_bucket_for(name: str) -> str:
    n = (name or "").lower()
    if "fomc" in n or "federal open market" in n:
        return "fomc"
    if "consumer price" in n or " cpi " in f" {n} ":
        return "cpi"
    if "producer price" in n or " ppi " in f" {n} ":
        return "ppi"
    if "employment situation" in n or "non-farm" in n or "nonfarm" in n:
        return "nfp"
    if "personal consumption" in n or " pce " in f" {n} ":
        return "pce"
    if "gross domestic product" in n or " gdp " in f" {n} ":
        return "gdp"
    if "retail trade" in n or "retail sales" in n:
        return "retail"
    if "industrial production" in n:
        return "industrial"
    if "jobless claims" in n:
        return "claims"
    if "h.4.1" in n or "h.41" in n:
        return "fed_h41"
    return "default"


def _category_for(name: str) -> str:
    """A coarser bucket than impact for filtering: rates / inflation /
    growth / labor / other. The UI groups events by this for legibility."""
    n = (name or "").lower()
    # Use word-boundary regex on the short acronyms so e.g. 'pce' inside a
    # longer word doesn't falsely match.
    def _w(pat): return re.search(r"\b" + pat + r"\b", n) is not None
    if "fomc" in n or "federal open market" in n or "h.4.1" in n or "h.41" in n:
        return "rates"
    if ("price index" in n or "consumption" in n
            or _w("cpi") or _w("ppi") or _w("pce")):
        return "inflation"
    if (_w("gdp") or "gross domestic" in n or "industrial" in n
            or "retail" in n or "trade" in n):
        return "growth"
    if ("employment" in n or "non-farm" in n or "nonfarm" in n
            or _w("nfp") or "jobless" in n or "unemployment" in n):
        return "labor"
    return "other"


# Aliases for `next_event` — let users type "cpi" and find "Consumer Price Index".
_NEXT_EVENT_ALIASES = {
    "cpi":  "consumer price index",
    "ppi":  "producer price index",
    "nfp":  "employment situation",
    "pce":  "personal consumption expenditures",
    "gdp":  "gross domestic product",
    "fomc": "fomc",
    "fed":  "fomc",
}


# ─── FRED fetch ─────────────────────────────────────────────────────────────

def _has_fred_key() -> bool:
    return bool(os.environ.get("FRED_API_KEY", "").strip())


def fetch_fred_release_dates(start: str, end: str) -> list[dict]:
    """Pull all release dates in the window. FRED paginates; we walk it."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        return []
    out: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        try:
            r = requests.get(
                f"{FRED_BASE}/releases/dates",
                params={
                    "api_key": key, "file_type": "json",
                    "realtime_start": start, "realtime_end": end,
                    "include_release_dates_with_no_data": "false",
                    "order_by": "release_date", "sort_order": "asc",
                    "limit": limit, "offset": offset,
                },
                timeout=20,
            )
            if r.status_code >= 400:
                log.warning("FRED /releases/dates failed: HTTP %d", r.status_code)
                break
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("FRED fetch error: %s", e)
            break
        rows = payload.get("release_dates", [])
        out.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
        if offset > 5000:
            # Safety cap: FRED's calendar shouldn't ever exceed a few thousand
            # events in a single window.
            break
    return out


def _to_datetime_utc(date_str: str, name: str) -> datetime:
    """Combine FRED's date with our hardcoded release-time-of-day."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    bucket = _time_bucket_for(name)
    hh, mm = RELEASE_TIME_UTC.get(bucket, RELEASE_TIME_UTC["default"])
    return d.replace(hour=hh, minute=mm)


# ─── Refresh job ────────────────────────────────────────────────────────────

def refresh() -> dict:
    """Pull next ~120 days of events into the DB. Idempotent — dedup is
    on (source, external_id, datetime_utc)."""
    started = time.time()
    if not _has_fred_key():
        return {"error": "FRED_API_KEY not configured", "inserted": 0}
    start = datetime.now(timezone.utc).date().isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=120)).date().isoformat()
    raw = fetch_fred_release_dates(start, end)

    rows = []
    for r in raw:
        try:
            name = (r.get("release_name") or "").strip()
            release_id = r.get("release_id")
            date = r.get("date")
            if not name or not date or release_id is None:
                continue
            impact = _impact_for(name)
            crypto = _crypto_impact_for(name)
            # Drop low-impact entries to keep the table tight — we can
            # always pull them again on demand. The UI doesn't show "low"
            # anyway.
            if impact == "low" and crypto < 0.15:
                continue
            dt_utc = _to_datetime_utc(date, name)
            external_id = f"fred-{release_id}-{date}"
            rows.append((
                "fred", external_id, name, "US", dt_utc.isoformat(),
                impact, _category_for(name), crypto,
            ))
        except (ValueError, TypeError, KeyError) as e:
            log.warning("malformed FRED row %r: %s", r, e)
            continue
    inserted = db.upsert_econ_events(rows) if rows else {"new": 0}
    return {
        "fetched": len(raw), "kept": len(rows),
        "new": inserted.get("new", 0),
        "elapsed_s": round(time.time() - started, 2),
    }


# ─── Read API ───────────────────────────────────────────────────────────────

def list_upcoming(days: int = 14, min_impact: str = "medium",
                  category: str | None = None) -> list[dict]:
    """Events occurring in the next N days, ordered earliest first."""
    impact_order = {"low": 0, "medium": 1, "high": 2}
    min_level = impact_order.get(min_impact, 1)
    allowed = [k for k, v in impact_order.items() if v >= min_level]
    start = datetime.now(timezone.utc).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    rows = db.list_econ_events(
        since=start, until=end, impact_in=allowed, category=category,
    )
    out = []
    for r in rows:
        d = dict(r)
        # Useful pre-computed field for the UI.
        try:
            dt = datetime.fromisoformat(d["datetime_utc"])
            delta = dt - datetime.now(timezone.utc)
            d["seconds_until"] = int(delta.total_seconds())
        except (ValueError, TypeError):
            d["seconds_until"] = None
        out.append(d)
    return out


def next_event(name_pattern: str) -> Optional[dict]:
    """Find the next upcoming event whose name matches `name_pattern`
    (case-insensitive substring). Acronyms (CPI, NFP, FOMC, ...) are
    expanded via `_NEXT_EVENT_ALIASES` so users can type the short form."""
    upcoming = list_upcoming(days=120, min_impact="low")
    pat = (name_pattern or "").lower().strip()
    expanded = _NEXT_EVENT_ALIASES.get(pat, pat)
    for ev in upcoming:
        if expanded in (ev.get("name") or "").lower():
            return ev
    return None


# ─── Push integration ───────────────────────────────────────────────────────

def fire_pre_event_alerts() -> dict:
    """Cron entry — for every event that's < 60 min away and not yet
    alerted, fire a push to every user who has push subscriptions. Idempotent
    via the `alerted_at` flag on the event row."""
    cutoff_iso = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    events = db.get_econ_events_due_for_alert(now_iso, cutoff_iso)
    if not events:
        return {"checked": 0, "alerted": 0}
    push_mod = None
    alerted = 0
    for ev in events:
        if ev["impact"] != "high":
            continue
        try:
            dt = datetime.fromisoformat(ev["datetime_utc"])
            mins = int((dt - datetime.now(timezone.utc)).total_seconds() / 60)
        except (ValueError, TypeError):
            mins = 0
        if push_mod is None:
            import push as _p
            push_mod = _p
        # Fan out to every user with active push subscriptions. We don't
        # filter by user preferences here yet — this is a "high-impact macro
        # event" notification that everyone in a HODL product wants.
        user_ids = db.get_users_with_push_subscriptions()
        for uid in user_ids:
            try:
                push_mod.notify_user(
                    uid,
                    title=f"📅 {ev['name'][:60]} in ~{mins}m",
                    body=f"High-impact macro event drops at "
                         f"{dt.strftime('%H:%M UTC')}.",
                    url="/long-term#news",
                    tag=f"econ-{ev['id']}",
                )
            except Exception as e:
                log.warning("econ pre-event push failed for %s: %s", uid, e)
        db.mark_econ_event_alerted(ev["id"])
        alerted += 1
    return {"checked": len(events), "alerted": alerted}
