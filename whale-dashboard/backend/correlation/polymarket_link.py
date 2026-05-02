from __future__ import annotations
"""Correlate whale filings with Polymarket markets.

When a new activist_filing or insider_txn lands, we search Polymarket's Gamma
API for active markets that mention the issuer's ticker or name. For each
match we record a snapshot of the market's mid price at filing time.

A separate periodic pass updates `price_24h_after`, `price_7d_after`, and
`price_30d_after` once enough wall-clock time has elapsed since the
correlation was first recorded. That gives us a clean dataset to score "did
the whale signal predict the market move?" — which is the actual
differentiator vs every other 13F dashboard.

Why we don't use the gateway's polymarket_client directly: this dashboard
runs in its own container and shouldn't import code across container
boundaries. We make minimal HTTP calls to the public Gamma + CLOB endpoints
ourselves. (Convention is consistent with midterm-dashboard's
backend/aggregators/polymarket.py — each dashboard has its own light client.)
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Stop-words that produce useless Polymarket searches.
_STOP = {"inc", "corp", "corporation", "company", "co", "ltd", "the", "and",
         "of", "for", "llc", "lp", "plc", "holdings", "group", "class", "cl"}


def _company_keywords(name: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z]{3,}", name or "")
    return [t for t in tokens if t.lower() not in _STOP][:3]


# ---------------------------------------------------------------------------
# Gamma + CLOB
# ---------------------------------------------------------------------------

async def _gamma_search(session: aiohttp.ClientSession, query: str,
                        limit: int = 10) -> list[dict]:
    params = {"active": "true", "closed": "false", "limit": str(limit), "_q": query}
    async with session.get(f"{GAMMA_HOST}/markets", params=params) as r:
        if r.status != 200:
            return []
        data = await r.json()
        return data if isinstance(data, list) else []


async def _clob_midpoint(session: aiohttp.ClientSession,
                         token_id: str) -> Optional[float]:
    if not token_id:
        return None
    async with session.get(f"{CLOB_HOST}/midpoint",
                           params={"token_id": token_id}) as r:
        if r.status != 200:
            return None
        try:
            data = await r.json()
            return float(data.get("mid")) if data.get("mid") else None
        except Exception:
            return None


def _market_token_id(market: dict) -> Optional[str]:
    """Pull the YES-side CLOB token id from a Gamma market dict.

    Gamma returns `clobTokenIds` as a JSON-string list of two tokens (YES, NO).
    We use YES so price-up == event-yes, the convention every UI assumes.
    """
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    if isinstance(raw, str):
        import json as _json
        try:
            tokens = _json.loads(raw)
        except Exception:
            return None
    else:
        tokens = raw
    if isinstance(tokens, list) and tokens:
        return str(tokens[0])
    return None


# ---------------------------------------------------------------------------
# Top-level: link a single filing
# ---------------------------------------------------------------------------

async def link_filing(session: aiohttp.ClientSession,
                      source_table: str, source_id: int,
                      ticker: Optional[str], company_name: Optional[str]) -> int:
    """Search Polymarket for relevant markets and write correlation rows.

    Returns the number of correlation rows written.
    """
    queries: List[str] = []
    if ticker:
        queries.append(ticker)
    if company_name:
        kw = _company_keywords(company_name)
        if kw:
            queries.append(" ".join(kw))

    if not queries:
        return 0

    seen_market_ids: set[str] = set()
    n = 0
    now = datetime.now(timezone.utc).isoformat()

    for q in queries:
        markets = await _gamma_search(session, q, limit=8)
        for m in markets:
            market_id = str(m.get("conditionId") or m.get("id") or "")
            if not market_id or market_id in seen_market_ids:
                continue
            seen_market_ids.add(market_id)
            slug = m.get("slug")
            question = m.get("question") or m.get("title")
            token_id = _market_token_id(m)
            price = await _clob_midpoint(session, token_id) if token_id else None

            with get_conn() as conn:
                try:
                    conn.execute(
                        """INSERT INTO market_correlation
                             (source_table, source_id, polymarket_market_id,
                              polymarket_slug, polymarket_question,
                              price_at_filing, recorded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (source_table, source_id, market_id, slug,
                         question, price, now),
                    )
                    n += 1
                except Exception:
                    # UNIQUE conflict — already linked.
                    pass

    return n


# ---------------------------------------------------------------------------
# Batch sweeps
# ---------------------------------------------------------------------------

async def sweep_recent_filings(hours_back: int = 48) -> dict:
    """Find filings recorded in the last N hours that don't yet have any
    correlation rows, and link them."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    candidates: list[tuple] = []
    with get_conn() as conn:
        for r in conn.execute("""
            SELECT id, target_ticker, target_name FROM activist_filings
             WHERE fetched_at >= ?
               AND id NOT IN (SELECT source_id FROM market_correlation
                               WHERE source_table='activist_filings')
        """, (cutoff,)):
            candidates.append(("activist_filings", int(r["id"]),
                               r["target_ticker"], r["target_name"]))
        for r in conn.execute("""
            SELECT id, issuer_ticker, issuer_name FROM insider_txns
             WHERE fetched_at >= ?
               AND txn_code IN ('P','S')        -- only headline buys/sells
               AND id NOT IN (SELECT source_id FROM market_correlation
                               WHERE source_table='insider_txns')
        """, (cutoff,)):
            candidates.append(("insider_txns", int(r["id"]),
                               r["issuer_ticker"], r["issuer_name"]))

    if not candidates:
        return {"linked": 0, "candidates": 0}

    total_links = 0
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for src_table, src_id, ticker, name in candidates:
            try:
                total_links += await link_filing(
                    session, src_table, src_id, ticker, name,
                )
                # Polite to Gamma's rate limit.
                await asyncio.sleep(0.1)
            except Exception:
                logger.exception("polymarket_link: failed src=%s id=%d",
                                 src_table, src_id)

    return {"linked": total_links, "candidates": len(candidates)}


async def update_followup_prices() -> dict:
    """Fill in price_24h_after / 7d / 30d for correlations whose recorded_at
    is sufficiently in the past. We re-fetch the CLOB midpoint for the same
    market and write into the appropriate column."""
    now = datetime.now(timezone.utc)
    windows = [
        ("price_24h_after", 24),
        ("price_7d_after", 24 * 7),
        ("price_30d_after", 24 * 30),
    ]
    updated = 0
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for col, hours in windows:
            cutoff = (now - timedelta(hours=hours)).isoformat()
            with get_conn() as conn:
                rows = conn.execute(
                    f"""SELECT id, polymarket_market_id, price_at_filing
                          FROM market_correlation
                         WHERE recorded_at <= ?
                           AND {col} IS NULL""",
                    (cutoff,),
                ).fetchall()
            for r in rows:
                # We need the token_id to query midpoint. Look up via Gamma
                # using the conditionId.
                try:
                    async with session.get(
                        f"{GAMMA_HOST}/markets",
                        params={"condition_ids": r["polymarket_market_id"]},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        if not isinstance(data, list) or not data:
                            continue
                    token_id = _market_token_id(data[0])
                    price = await _clob_midpoint(session, token_id) if token_id else None
                    if price is None:
                        continue
                    edge_bps: Optional[float] = None
                    if r["price_at_filing"]:
                        edge_bps = abs(price - r["price_at_filing"]) * 10_000
                    with get_conn() as conn:
                        conn.execute(
                            f"UPDATE market_correlation SET {col}=?, "
                            f"edge_bps=COALESCE(?, edge_bps) WHERE id=?",
                            (price, edge_bps, r["id"]),
                        )
                    updated += 1
                    await asyncio.sleep(0.1)
                except Exception:
                    logger.exception("polymarket_link: followup update failed id=%s",
                                     r["id"])
    return {"updated": updated}
