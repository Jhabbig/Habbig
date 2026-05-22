from __future__ import annotations
"""Seed the cusip_ticker map and the issuer_watchlist.

Two free SEC sources do most of the work:
    1. https://www.sec.gov/files/company_tickers.json
       Maps ticker -> {cik_str, ticker, title}. Covers every US-listed issuer
       that files with EDGAR. ~10k entries. We use this to populate
       issuer_watchlist (CIK + ticker + name) wholesale.
    2. The holdings table itself, which surfaces (cusip, issuer_name) pairs
       as we ingest 13F filings. We fuzzy-match those issuer names against
       company_tickers.json titles to learn the cusip8 -> ticker mapping
       organically.

CUSIPs are not directly exposed in any free SEC feed, so the bootstrap
mapping is name-based. Once we've seen a (cusip, issuer_name) pair and
matched it to a ticker, we cache the cusip8 -> ticker assignment forever.

For ambiguous matches we DON'T write to cusip_ticker — better to leave the
ticker NULL than to attribute Berkshire Hathaway A shares to the B-share
ticker by accident.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
RATE_DELAY_S = 0.13


def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT env var is required")
    return ua


_PUNCT = re.compile(r"[^a-z0-9\s]+")
_SUFFIXES = re.compile(
    r"\b(inc|llc|lp|l\.p\.|ltd|limited|plc|corp|corporation|company|co|"
    r"holdings|holding|group|the|class|cl|com|new|ord)\b\.?",
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    s = (name or "").lower().strip()
    s = _SUFFIXES.sub("", s)
    s = _PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Issuer watchlist (also doubles as our ticker -> CIK and CIK -> name index)
# ---------------------------------------------------------------------------

async def refresh_issuer_watchlist(session: Optional[aiohttp.ClientSession] = None) -> int:
    """Fetch SEC company_tickers.json and upsert into issuer_watchlist.

    Returns the number of rows touched.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    try:
        await asyncio.sleep(RATE_DELAY_S)
        async with session.get(SEC_TICKERS_URL,
                               headers={"User-Agent": _user_agent()}) as r:
            r.raise_for_status()
            payload = await r.json(content_type=None)
    finally:
        if own_session:
            await session.close()

    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    rows = []
    for v in payload.values():
        try:
            cik = int(v["cik_str"])
            ticker = (v["ticker"] or "").upper()
            title = v["title"] or ""
            if not ticker or not title:
                continue
            rows.append((cik, ticker, title))
        except (KeyError, TypeError, ValueError):
            continue

    n = 0
    with get_conn() as conn:
        for cik, ticker, title in rows:
            conn.execute(
                """INSERT INTO issuer_watchlist (cik, ticker, issuer_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(cik) DO UPDATE SET
                     ticker=excluded.ticker,
                     issuer_name=excluded.issuer_name""",
                (cik, ticker, title),
            )
            n += 1
    logger.info("cusip_seeder: refreshed issuer_watchlist (%d rows)", n)
    return n


# ---------------------------------------------------------------------------
# CUSIP -> ticker via name match
# ---------------------------------------------------------------------------

def resolve_unmapped_cusips(min_token_overlap: float = 0.75) -> int:
    """For each (cusip, issuer_name) pair we've seen in holdings but haven't
    mapped yet, attempt a fuzzy match against issuer_watchlist (issuer_name).
    Insert into cusip_ticker only when the match is confident.

    Returns the number of new cusip mappings written.

    Confidence rule: token Jaccard >= min_token_overlap AND no other watchlist
    entry within 0.05 of the best score (i.e., no near-tie). The near-tie
    check protects against issuers with similar names (e.g., "Berkshire
    Hathaway Inc Cl A" vs "Cl B" — different CUSIPs, different tickers).
    """
    with get_conn() as conn:
        unmapped = conn.execute("""
            SELECT DISTINCT h.cusip, h.issuer_name
              FROM holdings h
              LEFT JOIN cusip_ticker ct ON ct.cusip8 = SUBSTR(h.cusip, 1, 8)
             WHERE ct.cusip8 IS NULL
        """).fetchall()
        watchlist = conn.execute(
            "SELECT cik, ticker, issuer_name FROM issuer_watchlist"
        ).fetchall()

    if not watchlist:
        logger.warning("cusip_seeder: issuer_watchlist is empty — call "
                       "refresh_issuer_watchlist() first")
        return 0

    # Pre-tokenize watchlist once.
    wl_tokens = [
        (w["ticker"], w["issuer_name"], set(_normalize(w["issuer_name"]).split()))
        for w in watchlist
    ]

    now = datetime.now(timezone.utc).isoformat()
    new_rows: list[tuple[str, str, str, str]] = []

    for u in unmapped:
        ut = set(_normalize(u["issuer_name"]).split())
        if not ut:
            continue

        best_score = 0.0
        runner_up = 0.0
        best_ticker: Optional[str] = None
        best_name: Optional[str] = None

        for ticker, name, tokens in wl_tokens:
            if not tokens:
                continue
            inter = len(ut & tokens)
            if inter == 0:
                continue
            score = inter / len(ut | tokens)
            if score > best_score:
                runner_up = best_score
                best_score = score
                best_ticker = ticker
                best_name = name
            elif score > runner_up:
                runner_up = score

        if (best_ticker
                and best_score >= min_token_overlap
                and (best_score - runner_up) >= 0.05):
            new_rows.append((u["cusip"][:8], best_ticker, best_name or "", now))

    if not new_rows:
        logger.info("cusip_seeder: 0 new cusip mappings")
        return 0

    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO cusip_ticker (cusip8, ticker, issuer_name, source, last_updated)
               VALUES (?, ?, ?, 'fuzzy_name', ?)
               ON CONFLICT(cusip8) DO UPDATE SET
                 ticker=excluded.ticker,
                 issuer_name=excluded.issuer_name,
                 last_updated=excluded.last_updated
               WHERE cusip_ticker.source != 'openfigi'""",
            new_rows,
        )
        # Backfill ticker on any holdings rows that just became resolvable.
        conn.execute("""
            UPDATE holdings
               SET ticker = (
                   SELECT ticker FROM cusip_ticker
                    WHERE cusip8 = SUBSTR(holdings.cusip, 1, 8)
               )
             WHERE ticker IS NULL
               AND EXISTS (
                   SELECT 1 FROM cusip_ticker
                    WHERE cusip8 = SUBSTR(holdings.cusip, 1, 8)
               )
        """)

    logger.info("cusip_seeder: wrote %d new cusip mappings", len(new_rows))
    return len(new_rows)


async def run_full_seed(use_openfigi: bool = True,
                        openfigi_max: int = 500) -> dict:
    """One-shot bootstrap: refresh watchlist, then resolve unmapped CUSIPs.

    Order of operations:
      1. Refresh issuer_watchlist from SEC company_tickers.json (always free).
      2. (Optional) OpenFIGI lookup for authoritative cusip → ticker.
         These are tagged source='openfigi' and fuzzy_name will not overwrite
         them on the next pass.
      3. Fuzzy name match against issuer_watchlist for whatever's still
         unmapped — these get source='fuzzy_name'.
    """
    n_watch = await refresh_issuer_watchlist()

    figi: dict = {"queried": 0, "resolved": 0, "skipped": 0, "errors": 0}
    if use_openfigi:
        try:
            from data_sources.openfigi import resolve_unmapped_cusips_via_openfigi
            figi = await resolve_unmapped_cusips_via_openfigi(max_cusips=openfigi_max)
        except Exception as e:
            logger.warning("cusip_seeder: openfigi pass failed — %s: %s",
                           type(e).__name__, e)

    n_cusip = resolve_unmapped_cusips()
    return {
        "watchlist_rows": n_watch,
        "openfigi": figi,
        "new_cusip_mappings": n_cusip,
    }
