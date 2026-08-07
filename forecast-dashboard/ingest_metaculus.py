"""Metaculus ingestion (venue='metaculus') via the questions API.

Contract (same as the other ingest modules): async fetch_horizon(horizon_days)
returns a list of normalized market dicts:
  {venue: 'metaculus', venue_id: <question id>, event_ticker: None,
   question, category, url, end_date (ISO Z),
   yes_bid: None, yes_ask: None,          # Metaculus has no order book
   last_price: <displayed community probability 0..1>,
   liquidity: <forecaster count>,          # proxy for depth/attention
   volume_24h: None,
   yes_token_id: None, no_token_id: None}

Auth: verified live 2026-08-08 — anonymous API reads return 403 at the app
layer ("The API is only available to authenticated users") on both /api2/ and
/api/ paths, and the OpenAPI spec static sits behind a Cloudflare JS
challenge. Reads therefore need a (free) Metaculus API token, sent as
'Authorization: Token <t>' from the METACULUS_API_TOKEN env var. Without a
token the 403 is logged and fetch_horizon returns [] (never raises).

Because the live payload could not be inspected anonymously, parsing is
defensive across both documented shapes: legacy api2 rows
(possibilities.type, close_time, community_prediction.full.q2,
number_of_forecasters) and the post-2024 rewrite (question.type,
scheduled_close_time, aggregations.recency_weighted.latest.{centers,
forecast_values, means, forecaster_count}, nr_forecasters). Questions
without a public community prediction are skipped.

category maps from Metaculus category/tag/project labels onto the shared
taxonomy (consistent with the kalshi and manifold maps): politics / science /
tech / sports / finance / economics / weather / fed / other.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

API_URL = "https://www.metaculus.com/api2/questions/"
QUESTION_PAGE_URL = "https://www.metaculus.com/questions/{qid}/"
PAGE_LIMIT = 100
TIMEOUT = 15.0
_MAX_PAGES = 20
_PAGE_PAUSE = 0.25
_RETRY_BACKOFF = 1.0
_UA = "forecast-dashboard/0.1 (+https://narve.ai)"

_TOKEN_CATEGORIES = {
    "fed": "fed",
    "fomc": "fed",
    "weather": "weather",
    "hurricane": "weather",
    "ai": "tech",
    "ml": "tech",
    "tech": "tech",
    "technology": "tech",
    "politics": "politics",
    "geopolitics": "politics",
    "election": "politics",
    "elections": "politics",
    "law": "politics",
    "crypto": "finance",
    "cryptocurrency": "finance",
    "cryptocurrencies": "finance",
    "finance": "finance",
    "markets": "finance",
    "stocks": "finance",
    "economy": "economics",
    "economics": "economics",
    "business": "economics",
    "sport": "sports",
    "sports": "sports",
    "space": "science",
    "health": "science",
    "medicine": "science",
    "climate": "science",
    "environment": "science",
}

# Stems in priority order; the token map above wins over stems so that e.g.
# "Sports & Entertainment" lands on sports before any stem can fire.
_STEM_CATEGORIES = (
    ("fed", ("federal reserve", "interest rate")),
    ("politics", ("politic", "election", "geopolit", "governance")),
    ("economics", ("econom", "business")),
    ("finance", ("financ", "stock", "currenc", "crypto", "market")),
    ("tech", ("technolog", "comput", "artificial intelligence", "software", "robot")),
    ("science", ("scien", "physic", "biolog", "chemi", "astro", "space", "health", "medic", "pandemic", "climate", "environment")),
    ("sports", ("sport",)),
)


def api_headers() -> dict:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    token = (os.environ.get("METACULUS_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Token {token}"
    return headers


def map_category(labels: list[str]) -> str:
    for label in labels:
        for tok in re.split(r"[^a-z0-9]+", label.lower()):
            cat = _TOKEN_CATEGORIES.get(tok)
            if cat:
                return cat
    for cat, stems in _STEM_CATEGORIES:
        for label in labels:
            low = label.lower()
            if any(s in low for s in stems):
                return cat
    return "other"


def _labels(raw: dict, q: dict) -> list[str]:
    out: list[str] = []

    def _collect(item) -> None:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k in ("name", "slug"):
                v = item.get(k)
                if isinstance(v, str):
                    out.append(v)

    projects = raw.get("projects")
    if isinstance(projects, dict):
        for group in projects.values():
            for item in group if isinstance(group, list) else [group]:
                _collect(item)
    for source in (raw, q):
        for key in ("categories", "tags"):
            group = source.get(key)
            if isinstance(group, list):
                for item in group:
                    _collect(item)
    return out


def _as_prob(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if 0.0 <= f <= 1.0 else None


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prob_from_latest(latest) -> float | None:
    if not isinstance(latest, dict):
        return None
    centers = latest.get("centers")
    if isinstance(centers, (list, tuple)) and centers:
        p = _as_prob(centers[0])
        if p is not None:
            return p
    fv = latest.get("forecast_values")
    # binary forecast_values are [p_no, p_yes]
    if isinstance(fv, (list, tuple)) and len(fv) == 2:
        p = _as_prob(fv[1])
        if p is not None:
            return p
    means = latest.get("means")
    if isinstance(means, (list, tuple)) and means:
        return _as_prob(means[0])
    return None


def _recency_weighted_latest(raw: dict, q: dict) -> dict | None:
    for source in (q, raw):
        aggs = source.get("aggregations")
        if isinstance(aggs, dict):
            rw = aggs.get("recency_weighted")
            if isinstance(rw, dict) and isinstance(rw.get("latest"), dict):
                return rw["latest"]
    return None


def _community_prob(raw: dict, q: dict) -> float | None:
    p = _prob_from_latest(_recency_weighted_latest(raw, q))
    if p is not None:
        return p
    for source in (raw, q):
        cp = source.get("community_prediction")
        if isinstance(cp, dict):
            full = cp.get("full")
            if isinstance(full, dict):
                p = _as_prob(full.get("q2"))
                if p is not None:
                    return p
            p = _as_prob(cp.get("q2"))
            if p is not None:
                return p
        else:
            p = _as_prob(cp)
            if p is not None:
                return p
    return None


def _forecaster_count(raw: dict, q: dict) -> float | None:
    latest = _recency_weighted_latest(raw, q)
    if latest is not None:
        n = _to_float(latest.get("forecaster_count"))
        if n is not None:
            return n
    for source in (raw, q):
        for key in ("nr_forecasters", "number_of_forecasters", "forecasts_count"):
            n = _to_float(source.get(key))
            if n is not None:
                return n
    return None


def _parse_iso(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _close_time(raw: dict, q: dict) -> datetime | None:
    for source, key in ((q, "scheduled_close_time"), (raw, "scheduled_close_time"), (q, "close_time"), (raw, "close_time")):
        dt = _parse_iso(source.get(key))
        if dt is not None:
            return dt
    return None


def _normalize_question(raw: dict, now: datetime, cutoff: datetime) -> dict | None:
    qid = raw.get("id")
    q = raw.get("question") if isinstance(raw.get("question"), dict) else {}
    if qid is None:
        qid = q.get("id")
    if qid is None:
        return None
    poss = q.get("possibilities") or raw.get("possibilities")
    qtype = q.get("type") or (poss.get("type") if isinstance(poss, dict) else None)
    if qtype != "binary":
        return None
    title = ((raw.get("title") or q.get("title")) or "").strip()
    if not title:
        return None
    close_dt = _close_time(raw, q)
    if close_dt is None or close_dt <= now or close_dt > cutoff:
        return None
    prob = _community_prob(raw, q)
    if prob is None:
        return None
    return {
        "venue": "metaculus",
        "venue_id": str(qid),
        "event_ticker": None,
        "question": title,
        "category": map_category(_labels(raw, q)),
        "url": QUESTION_PAGE_URL.format(qid=qid),
        "end_date": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "yes_bid": None,
        "yes_ask": None,
        "last_price": prob,
        "liquidity": _forecaster_count(raw, q),
        "volume_24h": None,
        "yes_token_id": None,
        "no_token_id": None,
    }


async def _get_page(client: httpx.AsyncClient, params: dict) -> dict | None:
    for attempt in (0, 1):
        try:
            resp = await client.get(API_URL, params=params)
            if resp.status_code in (401, 403):
                log.warning("Metaculus API rejected the request (HTTP %s): anonymous access is disabled — set METACULUS_API_TOKEN", resp.status_code)
                return None
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            if attempt:
                log.warning("Metaculus GET %s failed after retry: %s", params, exc)
            else:
                await asyncio.sleep(_RETRY_BACKOFF)
    return None


async def fetch_horizon(horizon_days: int) -> list[dict]:
    """Fetch Metaculus binary questions closing within `horizon_days`.

    Returns a list of normalized market dicts (see module docstring).
    Never raises — on any failure, returns whatever was collected so far.
    """
    out: list[dict] = []
    try:
        try:
            days = max(1, int(horizon_days))
        except (TypeError, ValueError):
            days = 30
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=api_headers()) as client:
            seen: set[str] = set()
            for page in range(_MAX_PAGES):
                params = {
                    "status": "open",
                    "forecast_type": "binary",
                    "limit": PAGE_LIMIT,
                    "offset": page * PAGE_LIMIT,
                    # ignored by the legacy api2; the rewrite needs it for
                    # community predictions to appear on list rows
                    "with_cp": "true",
                }
                payload = await _get_page(client, params)
                if payload is None:
                    break
                results = payload.get("results")
                if not isinstance(results, list) or not results:
                    break
                for raw in results:
                    if not isinstance(raw, dict):
                        continue
                    row = _normalize_question(raw, now, cutoff)
                    if row is not None and row["venue_id"] not in seen:
                        seen.add(row["venue_id"])
                        out.append(row)
                if len(results) < PAGE_LIMIT:
                    break
                if "next" in payload and payload.get("next") is None:
                    break
                await asyncio.sleep(_PAGE_PAUSE)
    except Exception as exc:
        log.error("Metaculus ingest failed: %s", exc)
    return out
