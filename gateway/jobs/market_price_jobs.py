"""Live midterm market price polling (snapshot job).

Polls the Polymarket Gamma API for ACTIVE US-politics / midterm markets
and writes one ``market_snapshots`` row per market every ~5 minutes. The
snapshots feed two things:

  1. the historical-odds chart on the single main page, and
  2. the backtest's ``market_price_at_prediction`` lookup
     (``queries/markets.py:get_market_snapshot_at``), which joins
     predictions to the nearest prior snapshot by ``market_slug``.

Scope is MIDTERM ONLY (per the 2026-06-09 founder meeting: one vertical,
US politics). We deliberately do NOT snapshot every Polymarket market —
that would balloon the table and the upstream call budget.

Reuse, not duplication
----------------------
This job reuses ``portfolio/polymarket.py`` (``_gamma_base`` for the host
and the same httpx pattern). It does NOT construct a second Polymarket
client. ``fetch_market_state`` in that module is an *id-keyed* batch
lookup, which is the wrong shape for "list active markets", so we hit the
Gamma ``GET /markets?active=true&closed=false`` list endpoint directly —
the same endpoint ``backend/markets/polymarket_client.py:get_markets``
uses — but anchored on the shared ``_gamma_base()`` host helper.

Filtering
---------
Gamma's tag filtering is inconsistent across market vintages, so we fetch
active markets and keep the ones whose slug/question/category match a
politics keyword set (senate, house, election, midterm, governor, …).
Cheap, robust, and good enough for the single-vertical MVP.

Kalshi
------
``portfolio/kalshi.py`` only exposes auth'd, per-user calls (login +
``/portfolio/positions``) — there is no public market-list helper to
reuse, so Kalshi polling is left as a follow-up rather than wiring a new
client here.

Resilience
----------
Every network call is wrapped so a poll failure logs and returns a
summary instead of propagating into the scheduler. One bad Gamma
response never crashes the cron tick.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.market_price")


# Keyword set for the MIDTERM / US-politics vertical. Matched
# case-insensitively against a market's slug, question, and category.
# Kept intentionally broad-but-political so we catch the long tail of
# race-specific markets (e.g. "ohio-senate-2026") without dragging in
# sports/crypto/celebrity markets.
_MIDTERM_KEYWORDS = (
    "midterm",
    "senate",
    "congress",
    "congressional",
    "governor",
    "gubernatorial",
    "election",
    "republican",
    "democrat",
    "gop",
    "swing state",
    "ballot",
    "primary",
)

# "house" is matched separately because the bare word collides with
# "White House", "housing", etc. We only treat a market as House-related
# when "house" appears alongside a political qualifier. Both haystack and
# qualifiers are lowercased before the check.
_HOUSE_QUALIFIERS = (
    "control",
    "majority",
    "seat",
    "race",
    "flip",
    "win",
    "party",
    "representatives",
    "2026",
    "midterm",
)

# How many active markets to scan per poll. Gamma returns up to 100 per
# page; two pages keeps us well inside one tick while covering the active
# politics set comfortably. Bumping max_pages is the lever if coverage
# looks thin once real data lands.
_PAGE_SIZE = 100
_MAX_PAGES = 5

# Hard ceiling on snapshots written per run — a defensive cap so a Gamma
# response that suddenly tags everything "election" can't write thousands
# of rows in one tick.
_MAX_SNAPSHOTS_PER_RUN = 400

_HTTP_TIMEOUT = httpx.Timeout(15.0)


def _is_midterm_market(market: dict) -> bool:
    """True if *market* looks like a US-politics / midterm market.

    Matches keywords against slug, question/title, category, and
    groupItemTitle (Gamma's per-outcome label, e.g. the candidate name).
    """
    haystack = " ".join(
        str(market.get(k) or "")
        for k in ("slug", "question", "title", "category", "groupItemTitle")
    ).lower()
    if not haystack.strip():
        return False
    if any(kw in haystack for kw in _MIDTERM_KEYWORDS):
        return True
    # House markets: require a political qualifier near the word "house"
    # so we don't match "White House" press markets or housing/economy
    # markets.
    if "house" in haystack and any(q in haystack for q in _HOUSE_QUALIFIERS):
        return True
    return False


def _extract_yes_price(market: dict) -> Optional[float]:
    """Pull the YES price out of a Gamma market payload.

    Gamma returns ``outcomePrices`` as a JSON-encoded string like
    ``"[\"0.65\", \"0.35\"]"`` (index 0 = YES) on most markets, and
    occasionally as a real list. We mirror the parsing in
    ``backend/markets/unified_markets.py`` and ``jobs/resolution_jobs.py``
    — json.loads only, never eval. Returns None when no usable price is
    present so the caller can skip the market rather than write a bogus
    0.5 placeholder into the snapshot history.
    """
    raw = market.get("outcomePrices")
    prices: Any = None
    if isinstance(raw, str) and raw.strip():
        try:
            prices = json.loads(raw)
        except (ValueError, TypeError):
            prices = None
    elif isinstance(raw, list):
        prices = raw

    if isinstance(prices, list) and prices:
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            pass

    # Fallbacks for payload shapes that omit outcomePrices but carry a
    # single last-trade / best-bid price.
    for key in ("lastTradePrice", "bestBid", "price"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _extract_no_price(market: dict, yes_price: Optional[float]) -> Optional[float]:
    raw = market.get("outcomePrices")
    prices: Any = None
    if isinstance(raw, str) and raw.strip():
        try:
            prices = json.loads(raw)
        except (ValueError, TypeError):
            prices = None
    elif isinstance(raw, list):
        prices = raw
    if isinstance(prices, list) and len(prices) >= 2:
        try:
            return float(prices[1])
        except (TypeError, ValueError):
            pass
    if yes_price is not None:
        return round(1.0 - yes_price, 6)
    return None


def _extract_volume(market: dict) -> Optional[float]:
    for key in ("volume24hr", "volume", "volumeNum"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


async def _fetch_active_markets() -> list[dict]:
    """Page through Gamma's active markets via the shared host helper.

    Reuses ``portfolio.polymarket._gamma_base()`` so we honour the same
    ``POLYMARKET_GAMMA_BASE`` override the rest of the gateway uses, and
    never hard-code the host. Returns a flat list of raw market dicts;
    an empty list on any failure (logged).
    """
    from portfolio.polymarket import _gamma_base

    base = f"{_gamma_base()}/markets"
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for page in range(_MAX_PAGES):
            params = {
                "active": "true",
                "closed": "false",
                "limit": _PAGE_SIZE,
                "offset": page * _PAGE_SIZE,
            }
            try:
                resp = await client.get(base, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning(
                    "gamma active-markets fetch failed (page %d): %s",
                    page, exc,
                )
                break
            if isinstance(data, dict):
                data = data.get("data") or data.get("markets") or []
            if not isinstance(data, list) or not data:
                break
            out.extend(d for d in data if isinstance(d, dict))
            if len(data) < _PAGE_SIZE:
                break
    return out


@register_job("snapshot_midterm_market_prices")
async def snapshot_midterm_market_prices() -> dict[str, Any]:
    """Poll live midterm market prices and write market_snapshots rows.

    Returns a summary dict ``{scanned, matched, snapshotted, skipped,
    errors}`` for the job log. Never raises — a failed poll degrades to a
    zero-write tick so the scheduler keeps running.
    """
    from queries import markets as market_queries

    started = time.monotonic()
    now_ts = int(time.time())

    try:
        active = await _fetch_active_markets()
    except Exception as exc:  # pragma: no cover — defensive
        log.exception("snapshot poll aborted before fetch: %s", exc)
        return {
            "scanned": 0, "matched": 0, "snapshotted": 0,
            "skipped": 0, "errors": 1,
        }

    scanned = len(active)
    matched = 0
    snapshotted = 0
    skipped = 0
    errors = 0

    for market in active:
        if not _is_midterm_market(market):
            continue
        matched += 1

        if snapshotted >= _MAX_SNAPSHOTS_PER_RUN:
            skipped += 1
            continue

        slug = (market.get("slug") or market.get("conditionId") or "").strip()
        if not slug:
            skipped += 1
            continue

        yes_price = _extract_yes_price(market)
        if yes_price is None:
            # No usable price (e.g. brand-new market with no trades) —
            # skip rather than poison the history with a placeholder.
            skipped += 1
            continue

        no_price = _extract_no_price(market, yes_price)
        volume = _extract_volume(market)
        question = market.get("question") or market.get("title")

        try:
            market_queries.insert_market_snapshot(
                market_slug=slug,
                yes_price=yes_price,
                snapshotted_at=now_ts,
                market_question=question,
                category=market.get("category") or "politics",
                no_price=no_price,
                volume=volume,
                source_platform="polymarket",
            )
            snapshotted += 1
        except Exception as exc:
            log.warning("snapshot insert failed for %s: %s", slug, exc)
            errors += 1

    duration = round(time.monotonic() - started, 2)
    log.info(
        "midterm snapshot: scanned=%d matched=%d snapshotted=%d "
        "skipped=%d errors=%d in %.2fs",
        scanned, matched, snapshotted, skipped, errors, duration,
    )
    return {
        "scanned": scanned,
        "matched": matched,
        "snapshotted": snapshotted,
        "skipped": skipped,
        "errors": errors,
        "duration_seconds": duration,
    }


# 5-minute cadence. ``register_cron`` takes a single integer ``minute``
# (semantics match arq.cron — it does NOT accept a ``"*/5"`` string), so
# we register every fifth minute explicitly, mirroring the multi-minute
# registration pattern in jobs/sync_portfolios.py.
for _m in range(0, 60, 5):
    register_cron("snapshot_midterm_market_prices", minute=_m)
