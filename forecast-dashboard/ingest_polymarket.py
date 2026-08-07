"""Polymarket ingestion via the Gamma API, with optional CLOB book refinement.

Market dict contract (shared with ingest_kalshi.fetch_horizon):
    {
        "venue": "polymarket",
        "venue_id": str,          # condition id / market id, stable per market
        "event_ticker": str | None,
        "question": str,
        "category": str | None,   # e.g. "weather", "fed", "politics"
        "url": str | None,
        "end_date": str | None,   # ISO 8601, UTC
        "yes_bid": float | None,  # 0..1
        "yes_ask": float | None,  # 0..1
        "last_price": float | None,
        "liquidity": float | None,
        "volume_24h": float | None,
        "yes_token_id": str | None,
        "no_token_id": str | None,
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
PAGE_LIMIT = 100
MAX_PAGES = 40
SUPPLEMENT_MAX_PAGES = 20
SUPPLEMENT_MIN_LIQUIDITY = 10000
FINAL_MAX_PAGES = 10
FINAL_MIN_LIQUIDITY = 50000
REQUEST_TIMEOUT = 20.0
BOOK_TIMEOUT = 10.0
CLOB_REFINE_TOP_N = 50
BOOK_CONCURRENCY = 10

_GENERIC_TAGS = {"all", "trending", "new", "recurring", "hide from new", "weekly", "daily", "monthly"}

_CANONICAL_TAGS = [
    ("weather", {"weather", "climate", "temperature", "hurricane", "climate & weather"}),
    ("fed", {"fomc", "fed rates", "federal reserve", "interest rates", "fed"}),
    ("finance", {"finance", "stocks", "stock market", "crypto", "crypto prices", "bitcoin", "ethereum", "up or down"}),
    ("sports", {"sports", "esports", "games"}),
    ("politics", {"politics", "elections", "geopolitics", "global elections"}),
    ("economics", {"economics", "economy", "economic policy", "inflation", "macro indicators"}),
]


def _f(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_ids(raw) -> tuple[str | None, str | None]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        yes, no = raw[0], raw[1]
        return (str(yes) if yes else None, str(no) if no else None)
    return None, None


def _canonical(labels: set[str]) -> str | None:
    for canonical, aliases in _CANONICAL_TAGS:
        if aliases & labels:
            return canonical
    return None


def _category(m: dict, event: dict) -> str | None:
    # The venue's own category field stays authoritative, but is run through
    # the canonical map so e.g. "Economy"/"Crypto" land on the same buckets
    # the kalshi/manifold/metaculus maps emit.
    cat = m.get("category")
    if isinstance(cat, str) and cat.strip():
        low = cat.strip().lower()
        return _canonical({low}) or low
    labels = []
    for tags in (m.get("tags"), event.get("tags")):
        if not isinstance(tags, list):
            continue
        for t in tags:
            label = t.get("label") if isinstance(t, dict) else t
            if isinstance(label, str) and label.strip():
                labels.append(label.strip().lower())
    hit = _canonical(set(labels))
    if hit:
        return hit
    for label in labels:
        if label not in _GENERIC_TAGS:
            return label
    return None


def _normalize(m: dict, end_dt: datetime) -> dict | None:
    condition_id = m.get("conditionId")
    if not condition_id or not isinstance(condition_id, str):
        return None
    events = m.get("events")
    event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else {}
    slug = event.get("slug") or m.get("slug")
    yes_token, no_token = _token_ids(m.get("clobTokenIds"))
    liquidity = _f(m.get("liquidityNum"))
    if liquidity is None:
        liquidity = _f(m.get("liquidity"))
    return {
        "venue": "polymarket",
        "venue_id": condition_id,
        "event_ticker": event.get("ticker") or None,
        "question": m.get("question") or event.get("title") or "",
        "category": _category(m, event),
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "end_date": _iso_z(end_dt),
        "yes_bid": _f(m.get("bestBid")),
        "yes_ask": _f(m.get("bestAsk")),
        "last_price": _f(m.get("lastTradePrice")),
        "liquidity": liquidity,
        "volume_24h": _f(m.get("volume24hr")),
        "yes_token_id": yes_token,
        "no_token_id": no_token,
    }


_OFFSET_CAP = object()


async def _get_page(client: httpx.AsyncClient, params: dict):
    for attempt in (1, 2):
        try:
            resp = await client.get(GAMMA_MARKETS_URL, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 422:
                return _OFFSET_CAP
            logger.warning("polymarket GET %s -> HTTP %s (attempt %d)", GAMMA_MARKETS_URL, resp.status_code, attempt)
        except Exception as exc:
            logger.warning("polymarket GET %s failed (attempt %d): %s", GAMMA_MARKETS_URL, attempt, exc)
    return None


async def _book_bid_ask(client: httpx.AsyncClient, token_id: str) -> tuple[float | None, float | None] | None:
    try:
        resp = await client.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=BOOK_TIMEOUT)
        if resp.status_code != 200:
            return None
        book = resp.json()
    except Exception as exc:
        logger.debug("clob book %s failed: %s", token_id, exc)
        return None
    if not isinstance(book, dict):
        return None
    bids = [p for level in book.get("bids") or [] if isinstance(level, dict) and (p := _f(level.get("price"))) is not None]
    asks = [p for level in book.get("asks") or [] if isinstance(level, dict) and (p := _f(level.get("price"))) is not None]
    bid = max(bids) if bids else None
    ask = min(asks) if asks else None
    if bid is None and ask is None:
        return None
    return bid, ask


async def _refine_top_books(client: httpx.AsyncClient, rows: list[dict]) -> None:
    candidates = sorted(
        (r for r in rows if r.get("yes_token_id")),
        key=lambda r: r.get("liquidity") or 0.0,
        reverse=True,
    )[:CLOB_REFINE_TOP_N]
    if not candidates:
        return
    sem = asyncio.Semaphore(BOOK_CONCURRENCY)

    async def one(row: dict):
        async with sem:
            return await _book_bid_ask(client, row["yes_token_id"])

    results = await asyncio.gather(*(one(r) for r in candidates), return_exceptions=True)
    for row, res in zip(candidates, results):
        if isinstance(res, BaseException) or res is None:
            continue
        bid, ask = res
        if bid is not None:
            row["yes_bid"] = bid
        if ask is not None:
            row["yes_ask"] = ask


async def fetch_horizon(horizon_days: int) -> list[dict]:
    """Fetch active Polymarket markets closing within `horizon_days`.

    Returns a list of normalized market dicts (see module docstring).
    Never raises: on failure it returns whatever was collected so far.
    """
    now = datetime.now(timezone.utc)
    horizon_end = now + timedelta(days=max(int(horizon_days), 0))
    out: list[dict] = []
    seen: set[str] = set()
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "narve-forecast/1.0"}) as client:
            # Polymarket lists tens of thousands of short-dated micro-markets
            # (5-min crypto, per-game sports); an exhaustive sweep of the horizon
            # would flood every ingest cycle. When a tier's page budget runs out,
            # resume from the last endDate reached with a higher liquidity floor,
            # so the far end of the window still gets its meaningful markets.
            tiers = (
                (0, MAX_PAGES),
                (SUPPLEMENT_MIN_LIQUIDITY, SUPPLEMENT_MAX_PAGES),
                (FINAL_MIN_LIQUIDITY, FINAL_MAX_PAGES),
            )
            cursor = now
            done = False
            for min_liq, budget in tiers:
                extra = {"liquidity_num_min": min_liq} if min_liq else {}
                done, last_end = await _sweep(client, now, cursor, horizon_end, budget, extra, out, seen)
                if done:
                    break
                cursor = last_end or cursor
                logger.warning("polymarket: sweep (min_liquidity=%s) truncated at %d markets (endDate %s)", min_liq, len(out), _iso_z(cursor))
            if not done:
                logger.warning("polymarket: horizon window not fully covered; returning %d markets", len(out))
            await _refine_top_books(client, out)
    except Exception:
        logger.exception("polymarket fetch_horizon aborted; returning %d markets", len(out))
    return out


async def _sweep(
    client: httpx.AsyncClient,
    now: datetime,
    start: datetime,
    horizon_end: datetime,
    page_budget: int,
    extra_params: dict,
    out: list[dict],
    seen: set[str],
) -> tuple[bool, datetime | None]:
    """Paginate gamma from `start` to `horizon_end`, appending normalized rows.

    Returns (reached_horizon_end, max end_date seen).
    """
    cursor = start
    last_end: datetime | None = None
    pages = 0
    done = False
    while not done and pages < page_budget:
        offset = 0
        while pages < page_budget:
            params = {
                "closed": "false",
                "active": "true",
                "order": "endDate",
                "ascending": "true",
                "include_tag": "true",
                "limit": PAGE_LIMIT,
                "offset": offset,
                "end_date_min": _iso_z(cursor),
                "end_date_max": _iso_z(horizon_end),
                **extra_params,
            }
            page = await _get_page(client, params)
            pages += 1
            if page is _OFFSET_CAP:
                # Gamma rejects offsets past ~2000; restart from the last
                # endDate seen (results are endDate-ascending).
                if last_end is not None and last_end > cursor:
                    cursor = last_end
                elif last_end is not None and last_end == cursor:
                    logger.warning("polymarket: >%d markets share endDate %s; skipping the rest at that ts", offset, _iso_z(cursor))
                    cursor = cursor + timedelta(seconds=1)
                else:
                    done = True
                break
            if not isinstance(page, list) or not page:
                done = True
                break
            for m in page:
                if not isinstance(m, dict):
                    continue
                end_dt = _parse_iso(m.get("endDate"))
                if end_dt is None or end_dt < now:
                    continue
                if end_dt > horizon_end:
                    done = True
                    break
                if last_end is None or end_dt > last_end:
                    last_end = end_dt
                row = _normalize(m, end_dt)
                if row is not None and row["venue_id"] not in seen:
                    seen.add(row["venue_id"])
                    out.append(row)
            if done or len(page) < PAGE_LIMIT:
                done = True
                break
            offset += PAGE_LIMIT
    return done, last_end
