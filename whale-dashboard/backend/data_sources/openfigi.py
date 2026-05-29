from __future__ import annotations
"""OpenFIGI CUSIP → ticker resolver.

OpenFIGI is Bloomberg's free reference data service. Their /v3/mapping
endpoint accepts CUSIP/ISIN/SEDOL queries and returns the canonical ticker
plus exchange code, share class, and security type. This is dramatically
better than the SEC `company_tickers.json` name-fuzzy-match fallback in
`cusip_seeder.resolve_unmapped_cusips`, especially for:

    - Multi-class issuers (BRK.A vs BRK.B — same issuer name, different CUSIPs)
    - Tickers SEC misses entirely (foreign issuers, ADRs, recently re-listed)
    - Bond / preferred / warrant CUSIPs that share a name prefix with the
      common stock but are different securities — fuzzy match would silently
      attribute them to the wrong ticker.

Rate limits (per OpenFIGI):
    Without API key:  25 jobs / minute, batch size 10
    With API key:     250 jobs / 6s,    batch size 100

We auto-detect via the OPENFIGI_API_KEY env var. Without a key the seeder
still works, just slower (it'll process the unmapped backlog in chunks of
10 every ~3 seconds).

We resolve to a US-listed common-stock ticker when one exists; otherwise
we leave the row unresolved and let the existing fuzzy seeder try.

Mappings written here get `source='openfigi'`, which the fuzzy seeder
treats as authoritative — fuzzy_name will never overwrite openfigi.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Exchange / market codes we trust as "primary US listing". OpenFIGI returns
# many quotes per security (every venue, every share class, sometimes a dozen
# rows for one CUSIP). We pick from this set in priority order.
_PREFERRED_EXCH = ["US", "UN", "UQ", "UR", "UA", "UP", "UF", "UV", "UW", "UN"]
# `marketSector` we want — OpenFIGI categorizes corporate actions, indices,
# bonds, etc. under different sectors. Equity = common stock + ADRs.
_EQUITY_SECTOR = "Equity"


def _has_api_key() -> bool:
    return bool(os.environ.get("OPENFIGI_API_KEY"))


def _batch_size() -> int:
    return 100 if _has_api_key() else 10


def _request_pacing_s() -> float:
    """Delay between batches to stay under the rate limit.

    With key: 250 jobs / 6s. We send 100 per batch → safe at 6s/2.5 = 2.4s/batch.
              Use 3s for headroom.
    Without:   25 jobs / minute. We send 10 per batch → 2.5 batches/min = 24s/batch.
              Use 26s for headroom.
    """
    return 3.0 if _has_api_key() else 26.0


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.environ.get("OPENFIGI_API_KEY")
    if key:
        h["X-OPENFIGI-APIKEY"] = key
    return h


# ---------------------------------------------------------------------------
# Result selection
# ---------------------------------------------------------------------------

def _pick_best(results: list[dict]) -> Optional[dict]:
    """From OpenFIGI's list of quotes for one CUSIP, pick the most-likely
    primary US common-stock listing. Returns None if no acceptable match.
    """
    if not results:
        return None

    # Filter to equity-sector rows with a ticker.
    equity = [
        r for r in results
        if (r.get("marketSector") or "") == _EQUITY_SECTOR
        and r.get("ticker")
    ]
    if not equity:
        # Some CUSIPs only have a "Corp" / "Pfd" / "Govt" sector — leave
        # unmapped rather than guess.
        return None

    # Prefer common stock over preferred/ADR/warrant.
    common = [r for r in equity if (r.get("securityType2") or "") == "Common Stock"]
    pool = common or equity

    # Score by exchange preference. OpenFIGI's "exchCode" is e.g. "US", "UN" (NYSE),
    # "UQ" (NASDAQ Global Select). Pick the highest-priority match.
    def score(r: dict) -> int:
        exch = (r.get("exchCode") or "").upper()
        if exch in _PREFERRED_EXCH:
            return 1000 - _PREFERRED_EXCH.index(exch)
        return 0

    pool.sort(key=score, reverse=True)
    chosen = pool[0]
    if score(chosen) == 0:
        # No US listing in the result set — likely a foreign-only issue.
        # Skip rather than write a non-US ticker we can't price-link.
        return None
    return chosen


# ---------------------------------------------------------------------------
# Batched lookup
# ---------------------------------------------------------------------------

async def _lookup_batch(session: aiohttp.ClientSession,
                        cusips: list[str]) -> dict[str, Optional[dict]]:
    """POST one batch of up to _batch_size() CUSIPs. Returns {cusip: best_match_or_None}.

    OpenFIGI returns results in the same order as the request, with
    `{"warning": "..."}` or `{"error": "..."}` for misses. We treat both as
    "no mapping" rather than retrying.
    """
    jobs = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
    try:
        async with session.post(OPENFIGI_URL, json=jobs, headers=_headers(),
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 429:
                # Rate-limited despite our pacing — back off the whole batch.
                logger.warning("openfigi: 429 rate-limited, sleeping 15s")
                await asyncio.sleep(15)
                return {c: None for c in cusips}
            if r.status >= 400:
                body = await r.text()
                logger.warning("openfigi: %d response — %s", r.status, body[:200])
                return {c: None for c in cusips}
            payload = await r.json()
    except Exception as e:
        logger.warning("openfigi: request failed — %s: %s", type(e).__name__, e)
        return {c: None for c in cusips}

    out: dict[str, Optional[dict]] = {}
    for cusip, slot in zip(cusips, payload):
        if not isinstance(slot, dict):
            out[cusip] = None
            continue
        if "data" in slot:
            out[cusip] = _pick_best(slot["data"] or [])
        else:
            # warning / error — no mapping
            out[cusip] = None
    return out


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

async def resolve_unmapped_cusips_via_openfigi(
        max_cusips: int = 500,
        session: Optional[aiohttp.ClientSession] = None) -> dict:
    """Find CUSIPs in `holdings` with no `cusip_ticker` row (or with a
    fuzzy_name row that we'd like to verify), look them up at OpenFIGI,
    and write authoritative mappings.

    `max_cusips` caps how many we look up per call so a single sweep
    doesn't exhaust the rate budget.

    Returns counts: {"queried": N, "resolved": M, "skipped": K, "errors": E}.
    """
    with get_conn() as conn:
        # Pull cusips we have NO mapping for, plus any fuzzy_name mappings
        # we want to upgrade. We query each cusip8 at most once.
        #
        # Priority order:
        #   1. Existing fuzzy_name mappings — these are the ones currently
        #      showing the wrong ticker on the dashboard, so fixing them
        #      has visible value. (`source` ordering: fuzzy_name < NULL)
        #   2. Most-frequently-held CUSIPs — if a position is held by 10
        #      whales it matters more than one held by 1 micro-cap whale.
        rows = conn.execute(
            """SELECT SUBSTR(h.cusip, 1, 9) AS cusip9,
                      MAX(ct.source) AS existing_source,
                      COUNT(*) AS n_holdings
                 FROM holdings h
                 LEFT JOIN cusip_ticker ct
                        ON ct.cusip8 = SUBSTR(h.cusip, 1, 8)
                WHERE h.cusip IS NOT NULL
                  AND LENGTH(h.cusip) >= 9
                  AND (ct.cusip8 IS NULL OR ct.source = 'fuzzy_name')
                  -- Don't re-query CUSIPs we already tried that returned no
                  -- match (foreign issuers, mutual funds, defunct securities).
                  AND COALESCE(MAX(ct.source), '') != 'openfigi_unmappable'
                GROUP BY SUBSTR(h.cusip, 1, 9)
                ORDER BY
                  -- fuzzy_name first (currently mislabeled), then unmapped
                  CASE WHEN MAX(ct.source) = 'fuzzy_name' THEN 0 ELSE 1 END,
                  -- within each bucket, most-held first
                  COUNT(*) DESC
                LIMIT ?""",
            (max_cusips,),
        ).fetchall()
    cusips = [r["cusip9"] for r in rows if r["cusip9"]]
    if not cusips:
        logger.info("openfigi: no unmapped/upgradeable cusips")
        return {"queried": 0, "resolved": 0, "skipped": 0, "errors": 0}

    logger.info("openfigi: looking up %d cusips (api_key=%s, batch=%d)",
                len(cusips), bool(_has_api_key()), _batch_size())

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    resolved = 0
    skipped = 0
    errors = 0
    try:
        bs = _batch_size()
        pacing = _request_pacing_s()
        for i in range(0, len(cusips), bs):
            batch = cusips[i:i + bs]
            try:
                mapping = await _lookup_batch(session, batch)
            except Exception as e:  # defensive — _lookup_batch already swallows
                logger.warning("openfigi: batch failed — %s", e)
                errors += len(batch)
                await asyncio.sleep(pacing)
                continue

            now = datetime.now(timezone.utc).isoformat()
            writes: list[tuple] = []
            unmappable: list[tuple] = []
            for cusip9, best in mapping.items():
                if not best or not best.get("ticker"):
                    # Remember that OpenFIGI gave nothing for this cusip so
                    # we don't keep re-querying it on every loop. Write a
                    # source='openfigi_unmappable' row that resolve_unmapped
                    # filters out (treated as "already tried, no match").
                    unmappable.append((cusip9[:8], "?", "(no match)",
                                       "openfigi_unmappable", now))
                    skipped += 1
                    continue
                # Strip share-class suffix like "BRK/A" → "BRK.A" for
                # consistency with how SEC writes them.
                ticker = (best["ticker"] or "").upper().replace("/", ".")
                name = best.get("name") or ""
                writes.append((cusip9[:8], ticker, name, "openfigi", now))
                resolved += 1
            # Persist the "tried but no match" rows so future loops skip them.
            if unmappable:
                with get_conn() as conn:
                    conn.executemany(
                        """INSERT INTO cusip_ticker
                             (cusip8, ticker, issuer_name, source, last_updated)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(cusip8) DO UPDATE SET
                             source=excluded.source,
                             last_updated=excluded.last_updated
                           WHERE cusip_ticker.source IN ('fuzzy_name',
                                                         'openfigi_unmappable')""",
                        unmappable,
                    )

            if writes:
                with get_conn() as conn:
                    conn.executemany(
                        """INSERT INTO cusip_ticker
                             (cusip8, ticker, issuer_name, source, last_updated)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(cusip8) DO UPDATE SET
                             ticker=excluded.ticker,
                             issuer_name=excluded.issuer_name,
                             source=excluded.source,
                             last_updated=excluded.last_updated
                           WHERE cusip_ticker.source != 'openfigi'
                              OR excluded.source = 'openfigi'""",
                        writes,
                    )
                    # Backfill ticker on holdings rows that just became resolvable.
                    conn.execute("""
                        UPDATE holdings
                           SET ticker = (
                               SELECT ticker FROM cusip_ticker
                                WHERE cusip8 = SUBSTR(holdings.cusip, 1, 8)
                           )
                         WHERE EXISTS (
                                   SELECT 1 FROM cusip_ticker
                                    WHERE cusip8 = SUBSTR(holdings.cusip, 1, 8)
                               )
                           AND (holdings.ticker IS NULL
                                OR holdings.ticker != (
                                    SELECT ticker FROM cusip_ticker
                                     WHERE cusip8 = SUBSTR(holdings.cusip, 1, 8)
                                ))
                    """)

            # Pace before the next batch unless this was the last one.
            if i + bs < len(cusips):
                await asyncio.sleep(pacing)
    finally:
        if own_session:
            await session.close()

    logger.info("openfigi: queried=%d resolved=%d skipped=%d errors=%d",
                len(cusips), resolved, skipped, errors)
    return {"queried": len(cusips), "resolved": resolved,
            "skipped": skipped, "errors": errors}
