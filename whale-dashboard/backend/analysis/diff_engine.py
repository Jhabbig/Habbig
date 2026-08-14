from __future__ import annotations
"""Q-over-Q delta computation.

For each (entity, cusip, quarter), compare summed shares across all CIKs
belonging to that entity vs. the previous quarter, and tag the action:
    NEW    prev_shares = 0,  new_shares > 0
    EXIT   prev_shares > 0,  new_shares = 0
    ADD    new_shares > prev_shares
    TRIM   new_shares < prev_shares
    HOLD   new_shares == prev_shares

Why pre-compute? The UI shows "biggest moves last quarter" on every page load;
joining holdings to itself across two quarters at request time is slow once
there are millions of position rows. We recompute after each ingest and store
the result in holdings_delta.
"""

import logging

from database import get_conn

logger = logging.getLogger(__name__)


def _action(prev: int, new: int) -> str:
    if prev == 0 and new > 0:
        return "NEW"
    if prev > 0 and new == 0:
        return "EXIT"
    if new > prev:
        return "ADD"
    if new < prev:
        return "TRIM"
    return "HOLD"


def recompute_all_deltas() -> int:
    """Rebuild holdings_delta from scratch. Returns row count.

    This is intentionally a full rebuild rather than an incremental update —
    13F amendments (13F-HR/A) can rewrite history, and a full rebuild keeps
    the delta table consistent without bookkeeping. ~1M rows recomputes in
    seconds on SQLite.
    """
    with get_conn() as conn:
        # Aggregate shares + value per (entity, cusip, quarter).
        # We use the LATEST filing per (cik, quarter) to handle amendments.
        conn.execute("DROP TABLE IF EXISTS _entity_q")
        conn.execute("""
            CREATE TEMP TABLE _entity_q AS
            WITH latest_per_cik_q AS (
                SELECT cik, quarter_end, MAX(filed_date) AS max_filed
                  FROM filings_13f
                 GROUP BY cik, quarter_end
            ),
            chosen_filings AS (
                SELECT f.id AS filing_id, f.cik, f.quarter_end
                  FROM filings_13f f
                  JOIN latest_per_cik_q lpc
                    ON lpc.cik=f.cik
                   AND lpc.quarter_end=f.quarter_end
                   AND lpc.max_filed=f.filed_date
            )
            SELECT c.entity_id,
                   h.cusip,
                   MAX(h.ticker) AS ticker,
                   MAX(h.issuer_name) AS issuer_name,
                   cf.quarter_end,
                   SUM(h.shares) AS shares,
                   SUM(h.value_usd) AS value_usd
              FROM chosen_filings cf
              JOIN holdings h ON h.filing_id=cf.filing_id
              JOIN cik_map c  ON c.cik=cf.cik
             WHERE h.put_call IS NULL  -- exclude options for the headline diff
             GROUP BY c.entity_id, h.cusip, cf.quarter_end
        """)
        conn.execute("CREATE INDEX idx_eq ON _entity_q(entity_id, cusip, quarter_end)")

        # For each (entity, cusip, quarter), find the immediately-prior quarter
        # for the same (entity, cusip). Use a correlated subquery — clear and
        # SQLite handles it fine at this scale.
        conn.execute("DELETE FROM holdings_delta")
        conn.execute("""
            INSERT INTO holdings_delta
                (entity_id, cusip, ticker, issuer_name, quarter_end,
                 prev_shares, new_shares, delta_shares, delta_value_usd,
                 delta_pct, action)
            SELECT
                cur.entity_id,
                cur.cusip,
                cur.ticker,
                cur.issuer_name,
                cur.quarter_end,
                COALESCE(prv.shares, 0) AS prev_shares,
                cur.shares AS new_shares,
                cur.shares - COALESCE(prv.shares, 0) AS delta_shares,
                cur.value_usd - COALESCE(prv.value_usd, 0) AS delta_value_usd,
                CASE
                  WHEN COALESCE(prv.shares, 0) = 0 THEN NULL
                  ELSE 100.0 * (cur.shares - prv.shares) / prv.shares
                END AS delta_pct,
                CASE
                  WHEN COALESCE(prv.shares, 0) = 0 AND cur.shares > 0 THEN 'NEW'
                  WHEN COALESCE(prv.shares, 0) > 0 AND cur.shares = 0 THEN 'EXIT'
                  WHEN cur.shares > COALESCE(prv.shares, 0) THEN 'ADD'
                  WHEN cur.shares < COALESCE(prv.shares, 0) THEN 'TRIM'
                  ELSE 'HOLD'
                END AS action
              FROM _entity_q cur
              LEFT JOIN _entity_q prv
                ON prv.entity_id = cur.entity_id
               AND prv.cusip     = cur.cusip
               AND prv.quarter_end = (
                     SELECT MAX(q2.quarter_end)
                       FROM _entity_q q2
                      WHERE q2.entity_id = cur.entity_id
                        AND q2.cusip     = cur.cusip
                        AND q2.quarter_end < cur.quarter_end
                   )
        """)

        n = conn.execute("SELECT COUNT(*) AS n FROM holdings_delta").fetchone()["n"]
        conn.execute("DROP TABLE _entity_q")

    logger.info("diff_engine: rebuilt holdings_delta, %d rows", n)
    return int(n)
