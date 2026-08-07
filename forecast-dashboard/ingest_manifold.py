"""Manifold Markets ingestion (venue='manifold') via the public v0 API (no auth).

Contract (same as the other ingest modules): async fetch_horizon(horizon_days)
returns a list of normalized market dicts:
  {venue: 'manifold', venue_id: <market id>, event_ticker: None,
   question, category, url, end_date (ISO Z),
   yes_bid: None, yes_ask: None,          # Manifold exposes no order book
   last_price: <displayed probability 0..1>,
   liquidity: <total liquidity / subsidy pool>,
   volume_24h,
   yes_token_id: None, no_token_id: None}

category maps from Manifold group slugs onto the shared taxonomy
(consistent with the kalshi and metaculus maps): politics / science / tech /
sports / finance / economics / weather / fed / other.

GET /v0/markets pages newest-created first with before=<id> as the cursor, so
close-time filtering happens client-side and the page/row caps bound how deep
the created-time scan goes — a full-horizon sweep is not guaranteed, only the
most recently created slice of it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.manifold.markets/v0/markets"
PAGE_LIMIT = 1000
MAX_PAGES = 5
MAX_ROWS = 1500
TIMEOUT = 20.0
_PAGE_PAUSE = 0.25
_RETRY_BACKOFF = 1.0
_UA = "forecast-dashboard/0.1"

# Group slugs observed on manifold.markets; first match wins. Crypto lands in
# finance because the shared taxonomy has no crypto bucket; fed/weather match
# the buckets the kalshi series-prefix map emits (KXFED, KXHIGH/KXLOW) so the
# board's category filter groups the same concept across venues.
_CATEGORY_BY_SLUG = (
    ("fed", {"fed", "federal-reserve", "interest-rates"}),
    ("weather", {"weather", "hurricanes"}),
    (
        "politics",
        {
            "politics",
            "us-politics",
            "uk-politics",
            "world-politics",
            "elections",
            "us-elections",
            "2026-midterms",
            "geopolitics",
            "ukraine-russia-war",
            "israel-hamas-conflict",
            "supreme-court",
            "donald-trump",
            "trump-administration",
        },
    ),
    ("economics", {"economics", "economy", "macroeconomics", "inflation", "recession", "economics-default"}),
    ("finance", {"finance", "stocks", "stock-market", "investing", "sp-500", "crypto", "bitcoin", "ethereum", "crypto-prices", "cryptocurrencies"}),
    (
        "sports",
        {"sports", "sports-default", "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "tennis", "golf", "chess", "olympics", "formula-1", "premier-league", "world-cup"},
    ),
    ("science", {"science", "science-default", "biology", "physics", "chemistry", "space", "spacex", "nasa", "climate", "climate-change", "health", "medicine", "public-health", "covid", "biotech"}),
    ("tech", {"technology", "technology-default", "tech", "ai", "artificial-intelligence", "openai", "anthropic", "google-deepmind", "programming", "software", "hardware", "self-driving-cars"}),
)


def _to_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def _close_iso(close_ms: float) -> str:
    return datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _category(raw: dict) -> str:
    slugs = raw.get("groupSlugs")
    labels = set()
    if isinstance(slugs, list):
        for s in slugs:
            if isinstance(s, str) and s.strip():
                labels.add(s.strip().lower())
    for canonical, aliases in _CATEGORY_BY_SLUG:
        if aliases & labels:
            return canonical
    return "other"


def _normalize_market(raw: dict, now_ms: int, horizon_ms: int) -> dict | None:
    """Normalize one /v0/markets row; None when filtered out (non-binary,
    resolved, or closeTime outside (now, horizon])."""
    market_id = raw.get("id")
    if not market_id or not isinstance(market_id, str):
        return None
    if raw.get("outcomeType") != "BINARY":
        return None
    if raw.get("isResolved"):
        return None
    close_ms = _to_float(raw.get("closeTime"))
    if close_ms is None or close_ms <= now_ms or close_ms > horizon_ms:
        return None
    prob = _to_float(raw.get("probability"))
    if prob is not None and not (0.0 <= prob <= 1.0):
        prob = None
    question = (raw.get("question") or "").strip()
    return {
        "venue": "manifold",
        "venue_id": market_id,
        "event_ticker": None,
        "question": question or market_id,
        "category": _category(raw),
        "url": raw.get("url") or None,
        "end_date": _close_iso(close_ms),
        "yes_bid": None,
        "yes_ask": None,
        "last_price": prob,
        "liquidity": _to_float(raw.get("totalLiquidity")),
        "volume_24h": _to_float(raw.get("volume24Hours")),
        "yes_token_id": None,
        "no_token_id": None,
    }


async def _get_page(client: httpx.AsyncClient, params: dict) -> list | None:
    for attempt in (0, 1):
        try:
            resp = await client.get(API_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list):
                return payload
            return None
        except Exception as exc:
            if attempt:
                log.warning("Manifold GET %s failed after retry: %s", params, exc)
            else:
                await asyncio.sleep(_RETRY_BACKOFF)
    return None


async def fetch_horizon(horizon_days: int) -> list[dict]:
    """Fetch Manifold binary markets closing within `horizon_days`.

    Returns a list of normalized market dicts (see module docstring).
    Never raises — on any failure, returns whatever was collected so far.
    """
    out: list[dict] = []
    try:
        try:
            days = max(1, int(horizon_days))
        except (TypeError, ValueError):
            days = 30
        now_ms = int(time.time() * 1000)
        horizon_ms = now_ms + days * 86400 * 1000
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        ) as client:
            before: str | None = None
            for page_no in range(MAX_PAGES):
                params: dict = {"limit": PAGE_LIMIT}
                if before:
                    params["before"] = before
                page = await _get_page(client, params)
                if page is None:
                    break
                capped = False
                for raw in page:
                    if not isinstance(raw, dict):
                        continue
                    row = _normalize_market(raw, now_ms, horizon_ms)
                    if row is None or row["venue_id"] in seen:
                        continue
                    seen.add(row["venue_id"])
                    out.append(row)
                    if len(out) >= MAX_ROWS:
                        capped = True
                        break
                if capped:
                    log.warning("manifold: truncating at row cap %d (page %d)", MAX_ROWS, page_no + 1)
                    break
                last_id = page[-1].get("id") if page and isinstance(page[-1], dict) else None
                if len(page) < PAGE_LIMIT or not last_id:
                    break
                if page_no == MAX_PAGES - 1:
                    log.warning("manifold: truncating scan at page cap %d (%d rows kept)", MAX_PAGES, len(out))
                    break
                before = last_id
                await asyncio.sleep(_PAGE_PAUSE)
    except Exception as exc:
        log.error("Manifold ingest failed: %s", exc)
    return out
