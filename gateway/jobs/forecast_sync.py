"""Nightly sync: for every active, reasonably-trafficked market on
narve, fetch the current probability from each external provider and
record a row in ``external_forecasts``.

Strategy (per market):
  1. For each provider:
     a. Ask the fetcher for up to 8 candidate markets.
     b. Hand them to the matcher (which reuses or refreshes the
        equivalence cache in ``market_equivalences``).
     c. If we get a match, insert a forecast snapshot.
  2. Space requests politely — 2.1s between provider hits, per
     Metaculus's 30 req/min ceiling.

Idempotency: ``external_forecasts`` has a UNIQUE constraint on
``(market_slug, provider, recorded_at)``, and the recorded_at is the
sync run's wall clock rounded to the minute. A second run in the same
minute no-ops. A rerun at a later minute records a new row — that's
intentional, the chart needs multiple data points to draw a line.

Scope: markets with volume > $10k in the last 24h. Reading volume
from market_snapshots without touching the main db.py surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.forecast_sync")


# ── Config ───────────────────────────────────────────────────────────

_PROVIDER_SPACING_SECONDS = 2.1       # polite ceiling for Metaculus + friends
_MIN_VOLUME_USD = 10_000.0
_RECENT_SNAPSHOT_WINDOW_SECONDS = 24 * 3600
_MAX_MARKETS_PER_RUN = 500            # safety valve — usually runs under


@register_job("forecast_sync")
async def forecast_sync(limit: int = _MAX_MARKETS_PER_RUN) -> dict[str, Any]:
    """Main entry. Returns a summary dict for the job-status UI."""
    import db_forecasts
    from external_forecasts.base import PROVIDERS
    from external_forecasts import metaculus, manifold, fivethirtyeight, silver_bulletin
    from external_forecasts.matcher import find_equivalent

    fetchers = {
        "metaculus": metaculus.fetch_matching,
        "manifold": manifold.fetch_matching,
        "fivethirtyeight": fivethirtyeight.fetch_matching,
        "silver_bulletin": silver_bulletin.fetch_matching,
    }

    markets = _list_active_markets(limit=limit)
    if not markets:
        log.info("forecast_sync: no active markets above $%d volume", _MIN_VOLUME_USD)
        return {"markets": 0, "snapshots": 0, "providers": list(PROVIDERS)}

    snapshots_written = 0
    # Pin the run timestamp so every snapshot in this pass sorts together
    # and a same-minute rerun no-ops on the UNIQUE constraint.
    run_ts = int(time.time() // 60 * 60)

    for market in markets:
        market_dict = dict(market)
        slug = market_dict.get("market_slug")
        if not slug:
            continue

        for provider in PROVIDERS:
            fetch = fetchers[provider]
            try:
                candidates = await fetch(market_dict)
            except Exception:  # noqa: BLE001 — keep going
                log.exception("forecast_sync: %s fetch crashed for %s", provider, slug)
                candidates = []

            try:
                chosen, confidence = await find_equivalent(
                    market_dict, candidates, provider=provider,
                )
            except Exception:  # noqa: BLE001
                log.exception("forecast_sync: matcher crashed for %s/%s", slug, provider)
                chosen, confidence = (None, 0.0)

            if chosen is None:
                # No match this run; matcher already recorded the decision.
                await asyncio.sleep(_PROVIDER_SPACING_SECONDS)
                continue

            # Skip resolved markets — pinning at 1.0 or 0.0 is misleading
            # on a time-series chart once the outcome is known.
            if chosen.resolved:
                await asyncio.sleep(_PROVIDER_SPACING_SECONDS)
                continue

            try:
                inserted = db_forecasts.record_forecast(
                    market_slug=slug,
                    provider=provider,
                    probability=chosen.probability,
                    provider_market_id=chosen.provider_market_id,
                    recorded_at=run_ts,
                )
            except ValueError:
                # clamp_probability rejected the number — fetcher bug.
                log.warning("forecast_sync: bad probability from %s for %s", provider, slug)
                inserted = False

            if inserted:
                snapshots_written += 1
                log.info(
                    "forecast_sync: %s %s=%.3f (conf=%.2f)",
                    slug, provider, chosen.probability, confidence,
                )

            await asyncio.sleep(_PROVIDER_SPACING_SECONDS)

    return {
        "markets": len(markets),
        "snapshots": snapshots_written,
        "providers": list(PROVIDERS),
        "recorded_at": run_ts,
    }


def _list_active_markets(limit: int) -> list[dict]:
    """Pull the markets that are worth spending matcher calls on.

    We read directly from ``market_snapshots`` rather than go through a
    higher-level helper because this job has a specific definition of
    "active" (recent + volume-gated) that differs from the general
    market list used elsewhere. Kept local so the query doesn't leak
    into db.py.
    """
    import db
    cutoff = int(time.time()) - _RECENT_SNAPSHOT_WINDOW_SECONDS
    with db.conn() as c:
        # market_snapshots stores the market's closing/resolution time in
        # the ``close_time`` column. A refactor renamed it from
        # ``close_at`` elsewhere but this query was missed — produced
        # a ``no such column: close_at`` every nightly run until 2026-04-23.
        # Alias back to ``close_at`` in the result set so downstream
        # consumers (matcher) don't need a matching rename.
        rows = c.execute(
            "SELECT market_slug, market_question, category, "
            "       MAX(volume) AS volume, MAX(close_time) AS close_at "
            "FROM market_snapshots "
            "WHERE snapshotted_at >= ? "
            "GROUP BY market_slug "
            "HAVING volume IS NOT NULL AND volume >= ? "
            "ORDER BY volume DESC "
            "LIMIT ?",
            (cutoff, _MIN_VOLUME_USD, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Schedule ─────────────────────────────────────────────────────────

# Daily at 03:15 UTC — offset from affiliate (02:10) + other 02:00 jobs
# so we don't all fight for the sqlite write lock at once.
register_cron("forecast_sync", hour=3, minute=15)
