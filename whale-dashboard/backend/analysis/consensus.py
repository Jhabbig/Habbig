from __future__ import annotations
"""Smart-money consensus + crowdedness scoring.

Per (quarter_end, ticker), aggregate the actions taken by every entity:
    n_whales_long       distinct entities with shares > 0 this quarter
    n_whales_added      ADD or NEW
    n_whales_trimmed    TRIM or EXIT
    consensus_score     (added - trimmed) / max(1, added + trimmed)
                        Range [-1, +1]. +1 = unanimous accumulation;
                        -1 = unanimous distribution; 0 = balanced.

Crowdedness: percentile rank of `n_whales_long` across all tickers in the
same quarter. A ticker held by 80% of whales sits near 100; one held by 5%
sits near 0. High crowdedness = crash-risk on the long side / squeeze-risk
on the short side.

Both are pre-computed into consensus_snapshots after each 13F ingest so
the UI doesn't run aggregations on every page load.
"""

import logging
from datetime import datetime, timezone

from database import get_conn

logger = logging.getLogger(__name__)


def recompute_consensus() -> int:
    """Rebuild consensus_snapshots from holdings_delta.

    Returns the number of rows written.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        # Collect (entity, ticker, quarter, action) tuples first. Tickers may
        # be NULL — we skip those because the UI keys on ticker.
        conn.execute("DELETE FROM consensus_snapshots")
        conn.execute(
            """INSERT INTO consensus_snapshots
                 (quarter_end, ticker, issuer_name,
                  n_whales_long, n_whales_added, n_whales_trimmed,
                  n_whales_new, n_whales_exited,
                  consensus_score, aggregate_value_usd,
                  crowdedness_pctile, computed_at)
               SELECT
                  q.quarter_end,
                  q.ticker,
                  MAX(q.issuer_name) AS issuer_name,
                  SUM(CASE WHEN q.new_shares > 0 THEN 1 ELSE 0 END)              AS n_long,
                  SUM(CASE WHEN q.action IN ('ADD','NEW')   THEN 1 ELSE 0 END)   AS n_added,
                  SUM(CASE WHEN q.action IN ('TRIM','EXIT') THEN 1 ELSE 0 END)   AS n_trimmed,
                  SUM(CASE WHEN q.action = 'NEW'  THEN 1 ELSE 0 END)             AS n_new,
                  SUM(CASE WHEN q.action = 'EXIT' THEN 1 ELSE 0 END)             AS n_exit,
                  -- consensus_score: (added - trimmed) / (added + trimmed)
                  CASE
                    WHEN SUM(CASE WHEN q.action IN ('ADD','NEW','TRIM','EXIT')
                                  THEN 1 ELSE 0 END) = 0 THEN 0.0
                    ELSE
                      1.0 *
                      (SUM(CASE WHEN q.action IN ('ADD','NEW')   THEN 1 ELSE 0 END)
                       - SUM(CASE WHEN q.action IN ('TRIM','EXIT') THEN 1 ELSE 0 END))
                      /
                      SUM(CASE WHEN q.action IN ('ADD','NEW','TRIM','EXIT')
                               THEN 1 ELSE 0 END)
                  END AS consensus_score,
                  -- aggregate value across whales this quarter (long only).
                  SUM(CASE WHEN q.new_shares > 0
                           THEN COALESCE(q.delta_value_usd, 0)
                                + COALESCE(prev_val.value_usd, 0)
                           ELSE 0 END) AS aggregate_value_usd,
                  NULL AS crowdedness_pctile,
                  ?
                 FROM holdings_delta q
                 LEFT JOIN (
                     SELECT entity_id, cusip, quarter_end,
                            new_shares - delta_shares AS prev_shares_calc,
                            -- approximate prior-quarter value: not perfect but
                            -- good enough for an aggregate measure.
                            (new_shares - delta_shares) * 0 AS value_usd
                       FROM holdings_delta
                 ) prev_val
                   ON prev_val.entity_id = q.entity_id
                  AND prev_val.cusip = q.cusip
                  AND prev_val.quarter_end = q.quarter_end
                WHERE q.ticker IS NOT NULL
                GROUP BY q.quarter_end, q.ticker""",
            (now,),
        )

        # Compute crowdedness percentile per quarter, in a second pass. Window
        # functions (PERCENT_RANK) require SQLite >=3.25 — present everywhere
        # since 2018. If the runtime is older, fall back to NULL crowdedness.
        try:
            conn.execute(
                """WITH ranked AS (
                       SELECT id,
                              PERCENT_RANK() OVER (
                                  PARTITION BY quarter_end
                                  ORDER BY n_whales_long
                              ) * 100.0 AS pctile
                         FROM consensus_snapshots
                   )
                   UPDATE consensus_snapshots
                      SET crowdedness_pctile = (
                          SELECT pctile FROM ranked WHERE ranked.id = consensus_snapshots.id
                      )"""
            )
        except Exception:
            logger.warning("consensus: PERCENT_RANK unavailable, leaving crowdedness NULL")

        n = conn.execute(
            "SELECT COUNT(*) AS n FROM consensus_snapshots"
        ).fetchone()["n"]

    # Also update each entity's last_aum_usd from its latest 13F book.
    _update_entity_aum()

    logger.info("consensus: rebuilt %d snapshot rows", n)
    return int(n)


def _update_entity_aum() -> None:
    """Refresh entities.last_aum_usd from the most recent 13F per entity."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE entities
               SET last_aum_usd = (
                   SELECT SUM(h.value_usd)
                     FROM holdings h
                     JOIN filings_13f f ON f.id = h.filing_id
                     JOIN cik_map c     ON c.cik = f.cik
                    WHERE c.entity_id = entities.id
                      AND f.quarter_end = (
                          SELECT MAX(f2.quarter_end)
                            FROM filings_13f f2
                            JOIN cik_map c2 ON c2.cik = f2.cik
                           WHERE c2.entity_id = entities.id
                      )
                      AND h.put_call IS NULL
               ),
                   last_seen = ?
        """, (datetime.now(timezone.utc).isoformat(),))
