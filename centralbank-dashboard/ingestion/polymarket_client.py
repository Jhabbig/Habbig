"""Polymarket FOMC-market matcher.

Pulls active markets that close in the window around the next FOMC, applies a
rule-based filter to keep only Fed-rate-decision markets, and classifies each
market's question into the same bucket vocabulary v0.2 produces
(`cut25` / `cut50` / `hold` / `hike25` / `hike50` / ...).

Source: Polymarket Gamma API (`gamma-api.polymarket.com/markets`). Public, no
key. Cache 5 min — Polymarket prices move continuously, but more often than
that just hammers their API.

Matching is intentionally rule-based (per project policy):
  - title must mention Fed/FOMC/Federal Reserve/Federal Funds AND a rate-action term
  - bucket extraction via regex over verb + bps + direction
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from threading import Lock

from . import outcome_classifier

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
_UA = "centralbank-dashboard/0.1"

_FED_RX = re.compile(r"\b(fed|fomc|federal reserve|federal funds)\b", re.I)
_RATE_RX = re.compile(r"\b(rate|decision|cut|hike|hold|unchanged|no change|bps|bp|basis points?)\b", re.I)

_CACHE: dict = {"data": None, "fetched_at": 0.0, "key": None}
_CACHE_TTL = 5 * 60  # 5 min
_lock = Lock()


def classify_outcome(question: str) -> str | None:
    """Backwards-compatible delta-only classifier. New code should call
    :func:`outcome_classifier.classify` directly so it can pass the current
    rate and pick up Kalshi-style level questions."""
    return outcome_classifier.classify_delta(question or "")


def _is_fed_market(question: str) -> bool:
    return bool(_FED_RX.search(question or "") and _RATE_RX.search(question or ""))


def fetch_markets_in_window(end_min: date, end_max: date, limit: int = 200) -> list[dict]:
    """Fetch markets whose end-date falls in [end_min, end_max]. Empty on failure."""
    params = {
        "closed": "false",
        "active": "true",
        "limit": str(limit),
        "end_date_min": end_min.isoformat() + "T00:00:00Z",
        "end_date_max": end_max.isoformat() + "T23:59:59Z",
    }
    url = f"{GAMMA}/markets?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Polymarket fetch failed (%s..%s): %s", end_min, end_max, exc)
        return []


def _parse_yes_price(market: dict) -> float | None:
    """Extract the YES outcome price. Polymarket returns these as JSON-encoded
    strings inside the JSON response, which is awkward but consistent."""
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if not prices:
            return None
        return float(prices[0])  # YES is index 0 by convention on Polymarket
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _parse_volume(market: dict) -> float:
    """Pick the best available volume number. Field names vary across versions."""
    for k in ("volume24hr", "volumeNum", "volume24Hr", "volume"):
        v = market.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (ValueError, TypeError):
            continue
    return 0.0


def match_fomc_markets(meeting_date: date, window_days: int = 7) -> list[dict]:
    """Return matched markets with bucket, price, volume, URL — sorted by volume desc."""
    raw = fetch_markets_in_window(meeting_date, meeting_date + timedelta(days=window_days))
    out: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()  # (bucket, slug) — dedupe
    for m in raw:
        q = m.get("question") or ""
        if not _is_fed_market(q):
            continue
        bucket = classify_outcome(q)
        if not bucket:
            continue
        price = _parse_yes_price(m)
        if price is None:
            continue
        slug = m.get("slug") or str(m.get("id") or "")
        key = (bucket, slug)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append({
            "outcome_bucket": bucket,
            "question": q,
            "polymarket_price": round(price, 4),
            "volume_24h": _parse_volume(m),
            "url": f"https://polymarket.com/market/{slug}" if slug else None,
            "end_date": m.get("endDate"),
            "id": m.get("id"),
        })
    out.sort(key=lambda x: x["volume_24h"], reverse=True)
    return out


def get_cached_for_meeting(meeting_date: date, force: bool = False) -> list[dict]:
    now = time.time()
    key = meeting_date.isoformat()
    with _lock:
        fresh = (
            _CACHE["data"] is not None
            and _CACHE["key"] == key
            and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        )
        if fresh and not force:
            return _CACHE["data"]
    data = match_fomc_markets(meeting_date)
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
        _CACHE["key"] = key
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Self-test the classifier
    cases = [
        "Will the Fed cut rates by 25 bps in April 2026?",
        "Fed rate decision: 50 bp hike in June?",
        "Will the FOMC hold rates steady in May?",
        "Federal Reserve raises rates by 25 basis points",
        "Federal funds rate: no change in April 2026",
        "Bitcoin to $200k by year-end",  # negative
        "Trump approval rating",          # negative
    ]
    for q in cases:
        print(f"  {classify_outcome(q)!r:12s}  fed_match={_is_fed_market(q)}  | {q}")
