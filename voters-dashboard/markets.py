"""
Prediction-market integration for the Voters Dashboard.

Fetches relevant markets from:
  - Polymarket (https://gamma-api.polymarket.com/markets) — public read
  - Kalshi    (https://api.elections.kalshi.com/trade-api/v2/markets) — public read

For each country we know about, we apply a curated keyword filter
(loaded from data/election_keywords.yaml) to pick election-relevant
markets. Results are cached aggressively (markets don't move on minute
timescales for elections months away).

Public surface:
    await fetch_markets_for_country(iso, name) -> {polymarket: [...], kalshi: [...]}

If a sibling API is down or slow, we return an empty list with a
"_status" hint instead of failing the whole country page.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger("voters.markets")

KEYWORDS_PATH = Path(__file__).parent / "data" / "election_keywords.yaml"

POLYMARKET_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"

REQUEST_TIMEOUT = 6.0   # seconds — strict; we'd rather show stale cache than hang the page
CACHE_TTL = 300          # seconds — 5 min, election markets don't move that fast
NEGATIVE_CACHE_TTL = 60  # seconds — cache misses for less time so transient failures recover

# How many candidate markets to pull from each provider before keyword filtering.
# Polymarket gamma supports `limit` up to ~500. We pull a wide net once per TTL
# and filter in-process for all 25 countries off the same payload.
POLYMARKET_LIMIT = 500
KALSHI_LIMIT = 500

# Kalshi's bare /markets endpoint is dominated by sports parlays. We instead
# pull from /events with category filters that map to election-relevant chains.
#
# Note on prices: Kalshi's public/unauthenticated API does not return
# bid/ask/last/volume on /markets or /events?with_nested_markets — those
# fields are populated only for authenticated sessions. We surface the
# question + click-through to kalshi.com and let the UI render gracefully
# when prices are absent. (Polymarket gamma exposes prices publicly, so
# their YES% appears as expected.)
KALSHI_EVENT_CATEGORIES = ("Elections", "Politics", "World", "Economics")


# ──────────────────────────────────────────────────────────────────────────────
# Keyword loading
# ──────────────────────────────────────────────────────────────────────────────

_keywords_cache: dict[str, Any] | None = None
_keywords_loaded_at: float = 0.0


def _load_keywords() -> dict[str, dict]:
    global _keywords_cache, _keywords_loaded_at
    now = time.time()
    if _keywords_cache and (now - _keywords_loaded_at) < 300:
        return _keywords_cache
    if not KEYWORDS_PATH.exists():
        _keywords_cache = {}
        _keywords_loaded_at = now
        return {}
    try:
        with KEYWORDS_PATH.open("r", encoding="utf-8") as f:
            _keywords_cache = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("election_keywords.yaml unreadable: %s", e)
        _keywords_cache = {}
    _keywords_loaded_at = now
    return _keywords_cache


# ──────────────────────────────────────────────────────────────────────────────
# Provider fetchers — return list of normalised market dicts
# ──────────────────────────────────────────────────────────────────────────────

# Each market is normalised to:
#   {
#     "provider": "polymarket" | "kalshi",
#     "id": str,
#     "question": str,
#     "url": str,
#     "search_blob": str (lowercase, used for keyword match),
#     "yes_price": float | None,    # 0..1
#     "volume": float | None,       # USD or contracts, provider-dependent
#     "end_date": str | None,
#     "active": bool,
#   }


_global_lock = asyncio.Lock()
_polymarket_cache: dict[str, Any] = {"data": [], "fetched_at": 0.0, "ok": False}
_kalshi_cache: dict[str, Any] = {"data": [], "fetched_at": 0.0, "ok": False}


def _normalize_polymarket(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    qid = str(raw.get("id") or raw.get("conditionId") or "")
    question = (raw.get("question") or "").strip()
    if not qid or not question:
        return None
    slug = (raw.get("slug") or "").lower()
    tags = " ".join(str(t.get("label") or t.get("slug") or "") for t in (raw.get("tags") or []) if isinstance(t, dict)).lower()
    blob = " ".join([question.lower(), slug, tags])

    # Best-effort YES price extraction. Polymarket gamma exposes outcomes
    # & outcomePrices in different shapes across endpoint versions.
    yes_price = None
    try:
        prices = raw.get("outcomePrices")
        if isinstance(prices, list) and prices:
            # ['0.62', '0.38'] -> first is YES
            yes_price = float(prices[0])
        elif isinstance(prices, str):
            # JSON string fallback
            import json as _json
            arr = _json.loads(prices)
            if isinstance(arr, list) and arr:
                yes_price = float(arr[0])
    except Exception:
        yes_price = None

    volume = None
    try:
        v = raw.get("volume") or raw.get("volume24hr")
        volume = float(v) if v is not None else None
    except (TypeError, ValueError):
        volume = None

    return {
        "provider": "polymarket",
        "id": qid,
        "question": question,
        "url": f"https://polymarket.com/event/{slug}" if slug else f"https://polymarket.com/markets/{qid}",
        "search_blob": blob,
        "yes_price": yes_price,
        "volume": volume,
        "end_date": raw.get("endDate"),
        "active": bool(raw.get("active", True)) and not bool(raw.get("closed", False)),
    }


def _normalize_kalshi(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ticker = raw.get("ticker") or ""
    title = (raw.get("title") or raw.get("subtitle") or "").strip()
    if not ticker or not title:
        return None
    blob = " ".join([
        title.lower(),
        (raw.get("subtitle") or "").lower(),
        ticker.lower(),
        (raw.get("category") or "").lower(),
    ])
    yes_price = None
    try:
        # Kalshi prices are in cents (0..100)
        if raw.get("yes_bid") is not None and raw.get("yes_ask") is not None:
            yes_price = (float(raw["yes_bid"]) + float(raw["yes_ask"])) / 2.0 / 100.0
        elif raw.get("last_price") is not None:
            yes_price = float(raw["last_price"]) / 100.0
    except (TypeError, ValueError):
        yes_price = None
    volume = None
    try:
        v = raw.get("volume") or raw.get("volume_24h")
        volume = float(v) if v is not None else None
    except (TypeError, ValueError):
        volume = None
    return {
        "provider": "kalshi",
        "id": ticker,
        "question": title,
        "url": f"https://kalshi.com/markets/{ticker}",
        "search_blob": blob,
        "yes_price": yes_price,
        "volume": volume,
        "end_date": raw.get("close_time") or raw.get("expiration_time"),
        "active": (raw.get("status") or "").lower() == "active",
    }


async def _fetch_polymarket() -> list[dict]:
    """Pull a wide slice of active Polymarket markets, normalise, return."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": POLYMARKET_LIMIT,
        "order": "volume",
        "ascending": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(POLYMARKET_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("polymarket fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for raw in data:
        n = _normalize_polymarket(raw)
        if n and n["active"]:
            out.append(n)
    return out


async def _fetch_kalshi_category(client: httpx.AsyncClient, category: str) -> list[dict]:
    """Fetch one category's events with nested markets, return flat market list."""
    params = {
        "limit": 200,
        "status": "open",
        "category": category,
        "with_nested_markets": "true",
    }
    try:
        r = await client.get(KALSHI_EVENTS_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("kalshi %s fetch failed: %s", category, e)
        return []
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    out: list[dict] = []
    for ev in events:
        ev_title = (ev.get("title") or "").strip()
        ev_subtitle = (ev.get("sub_title") or ev.get("subtitle") or "").strip()
        for raw in (ev.get("markets") or []):
            # Inject event-level title/subtitle so the search blob is meaningful
            # (per-market titles are often just "Yes"/option labels).
            enriched = dict(raw)
            enriched.setdefault("title", ev_title or raw.get("title"))
            enriched.setdefault("subtitle", ev_subtitle or raw.get("subtitle"))
            enriched["category"] = category
            n = _normalize_kalshi(enriched)
            if n and n["active"]:
                out.append(n)
    return out


async def _fetch_kalshi() -> list[dict]:
    """Pull Kalshi events from political categories, flatten to markets."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            tasks = [_fetch_kalshi_category(client, c) for c in KALSHI_EVENT_CATEGORIES]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.warning("kalshi fetch failed: %s", e)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for res in results:
        if not isinstance(res, list):
            continue
        for n in res:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            out.append(n)
    return out


async def _refresh_cache_if_stale() -> None:
    """Refresh both provider caches in parallel, respecting TTL."""
    now = time.time()
    polymarket_stale = (now - _polymarket_cache["fetched_at"]) > CACHE_TTL
    kalshi_stale = (now - _kalshi_cache["fetched_at"]) > CACHE_TTL
    if not polymarket_stale and not kalshi_stale:
        return

    async with _global_lock:
        # Re-check after acquiring lock — another task may have refreshed.
        now = time.time()
        polymarket_stale = (now - _polymarket_cache["fetched_at"]) > CACHE_TTL
        kalshi_stale = (now - _kalshi_cache["fetched_at"]) > CACHE_TTL
        tasks = []
        keys = []
        if polymarket_stale:
            tasks.append(_fetch_polymarket()); keys.append("polymarket")
        if kalshi_stale:
            tasks.append(_fetch_kalshi()); keys.append("kalshi")
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key, res in zip(keys, results):
            cache = _polymarket_cache if key == "polymarket" else _kalshi_cache
            if isinstance(res, list):
                cache["data"] = res
                cache["fetched_at"] = time.time()
                cache["ok"] = bool(res)
            else:
                # Negative cache: set fetched_at slightly in the past so we
                # retry sooner than a full TTL but don't hammer the API.
                cache["fetched_at"] = time.time() - (CACHE_TTL - NEGATIVE_CACHE_TTL)
                cache["ok"] = False


# ──────────────────────────────────────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────────────────────────────────────

def _score(blob: str, kw: dict) -> tuple[bool, int]:
    """Return (is_match, score).

    Match if blob contains any `must_any`. Excluded if any `must_not` matches.
    Score = base 1 + count of `boost` phrases present.
    """
    must_any = kw.get("must_any") or []
    must_not = kw.get("must_not") or []
    boost = kw.get("boost") or []
    if not any(p.lower() in blob for p in must_any):
        return False, 0
    if any(p.lower() in blob for p in must_not):
        return False, 0
    score = 1 + sum(1 for p in boost if p.lower() in blob)
    return True, score


def _filter_for_country(markets: list[dict], iso: str) -> list[dict]:
    kw = (_load_keywords() or {}).get(iso)
    if not kw:
        return []
    matched: list[tuple[int, dict]] = []
    for m in markets:
        ok, score = _score(m["search_blob"], kw)
        if ok:
            matched.append((score, m))
    # Sort by score desc, then volume desc
    matched.sort(key=lambda t: (-t[0], -(t[1].get("volume") or 0)))
    return [m for _, m in matched]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_markets_for_country(iso: str, name: str = "") -> dict[str, Any]:
    """Return matched markets for a country, with provider-level status."""
    await _refresh_cache_if_stale()
    poly_filtered = _filter_for_country(_polymarket_cache["data"], iso)[:8]
    kal_filtered = _filter_for_country(_kalshi_cache["data"], iso)[:8]

    # Strip the search blob from results (internal use only).
    def _clean(items: list[dict]) -> list[dict]:
        return [{k: v for k, v in m.items() if k != "search_blob"} for m in items]

    return {
        "polymarket": _clean(poly_filtered),
        "kalshi": _clean(kal_filtered),
        "_status": {
            "polymarket": {
                "ok": _polymarket_cache["ok"],
                "fetched_at": int(_polymarket_cache["fetched_at"]),
                "candidate_count": len(_polymarket_cache["data"]),
            },
            "kalshi": {
                "ok": _kalshi_cache["ok"],
                "fetched_at": int(_kalshi_cache["fetched_at"]),
                "candidate_count": len(_kalshi_cache["data"]),
            },
        },
    }


async def warmup() -> None:
    """Best-effort prefetch on server boot. Safe to fail."""
    try:
        await _refresh_cache_if_stale()
    except Exception as e:
        log.warning("markets warmup failed: %s", e)
