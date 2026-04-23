"""DB helpers for the external forecast benchmark feature.

Two logical surfaces in here:

  1. **Forecasts** — append-only time series of probabilities from
     Metaculus / Manifold / FiveThirtyEight / Silver Bulletin, one row
     per (market, provider, snapshot). Written by the sync job,
     read by the market detail page + /dashboard/models.

  2. **Equivalences** — cached "which market on <provider> is our
     equivalent?" decisions. Populated by the Haiku matcher in
     ``external_forecasts/matcher.py``. Admin can reject or manually
     set a row via /admin/equivalences.

Integer pence-style discipline isn't needed (probabilities are REAL),
but we clamp to [0, 1] on every write so a broken fetcher can't poison
the chart. Timestamps are stored as INTEGER Unix seconds to match the
rest of the codebase.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

import db


SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "metaculus",
    "manifold",
    "fivethirtyeight",
    "silver_bulletin",
)
# Equivalence rows expire after this many days — after that the matcher
# re-runs. Set to 90d per spec; admin_override rows ignore expiry.
EQUIV_TTL_SECONDS = 90 * 86400
# Anything below this confidence is surfaced in the admin review queue.
LOW_CONFIDENCE_THRESHOLD = 0.70


def _clamp01(p: float) -> float:
    """Reject anything outside [0, 1]. Fetchers occasionally return
    0.0..100.0 instead of 0.0..1.0 — callers should divide first, but
    this is a last-line defence against bad numbers breaking the chart.
    """
    try:
        v = float(p)
    except (TypeError, ValueError):
        raise ValueError(f"probability must be numeric, got {p!r}")
    if v < 0.0 or v > 1.0:
        raise ValueError(f"probability out of range [0, 1]: {v}")
    return v


# ── Forecast time series ─────────────────────────────────────────────


def record_forecast(
    *,
    market_slug: str,
    provider: str,
    probability: float,
    provider_market_id: Optional[str] = None,
    recorded_at: Optional[int] = None,
) -> bool:
    """Insert a snapshot. Returns True on insert, False on uniqueness
    collision (same slug+provider+timestamp already recorded)."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    slug = (market_slug or "").strip()
    if not slug:
        raise ValueError("market_slug required")
    prob = _clamp01(probability)
    ts = int(recorded_at) if recorded_at is not None else int(time.time())
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO external_forecasts "
                "(market_slug, provider, provider_market_id, probability, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, provider, provider_market_id, prob, ts),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def latest_forecast_per_provider(market_slug: str) -> dict[str, dict]:
    """Return ``{provider: {probability, recorded_at, provider_market_id}}``
    using the most recent row per provider. Used by the comparison
    header on market detail pages.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT provider, probability, recorded_at, provider_market_id "
            "FROM external_forecasts "
            "WHERE market_slug = ? "
            "  AND (provider, recorded_at) IN ("
            "    SELECT provider, MAX(recorded_at) "
            "    FROM external_forecasts "
            "    WHERE market_slug = ? "
            "    GROUP BY provider"
            "  )",
            (market_slug, market_slug),
        ).fetchall()
    return {
        r["provider"]: {
            "probability": float(r["probability"]),
            "recorded_at": int(r["recorded_at"]),
            "provider_market_id": r["provider_market_id"],
        }
        for r in rows
    }


def forecast_time_series(
    market_slug: str, *, since_ts: Optional[int] = None
) -> list[dict]:
    """Return all forecast rows for a market, oldest first. Used to draw
    the chart on the market detail page. Optional ``since_ts`` limits
    to the last N days (chart's [7d] / [30d] / [all] toggle)."""
    with db.conn() as c:
        if since_ts is None:
            rows = c.execute(
                "SELECT provider, probability, recorded_at "
                "FROM external_forecasts "
                "WHERE market_slug = ? ORDER BY recorded_at ASC",
                (market_slug,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT provider, probability, recorded_at "
                "FROM external_forecasts "
                "WHERE market_slug = ? AND recorded_at >= ? "
                "ORDER BY recorded_at ASC",
                (market_slug, int(since_ts)),
            ).fetchall()
    return [
        {
            "provider": r["provider"],
            "probability": float(r["probability"]),
            "recorded_at": int(r["recorded_at"]),
        }
        for r in rows
    ]


def provider_series_for_scoring(
    provider: str, *, since_ts: Optional[int] = None
) -> list[dict]:
    """All (market, probability, ts) rows for a provider — used by the
    Brier-score calc on /dashboard/models."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    with db.conn() as c:
        if since_ts is None:
            rows = c.execute(
                "SELECT market_slug, probability, recorded_at "
                "FROM external_forecasts WHERE provider = ? "
                "ORDER BY market_slug, recorded_at ASC",
                (provider,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT market_slug, probability, recorded_at "
                "FROM external_forecasts "
                "WHERE provider = ? AND recorded_at >= ? "
                "ORDER BY market_slug, recorded_at ASC",
                (provider, int(since_ts)),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Market equivalence cache ─────────────────────────────────────────


def get_equivalence(
    market_slug: str, provider: str
) -> Optional[sqlite3.Row]:
    """Cached match for (our slug, provider). Returns the row regardless
    of age/rejected — callers are expected to check expiry themselves."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM market_equivalences "
            "WHERE market_slug = ? AND provider = ?",
            (market_slug, provider),
        ).fetchone()


def equivalence_is_fresh(row: sqlite3.Row) -> bool:
    """A cached row is reusable if it's ``mapped_by='admin_override'``
    (human decision, never expires) OR younger than the TTL."""
    if row is None:
        return False
    if row["rejected"]:
        return False
    if row["mapped_by"] == "admin_override":
        return True
    age = int(time.time()) - int(row["mapped_at"])
    return age < EQUIV_TTL_SECONDS


def upsert_equivalence(
    *,
    market_slug: str,
    provider: str,
    provider_market_id: str,
    confidence: float,
    provider_question: Optional[str] = None,
    mapped_by: str = "auto",
) -> None:
    """Insert or replace a match. ``mapped_by='admin_override'`` pins
    the row so the matcher never overwrites a human decision."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    conf = max(0.0, min(1.0, float(confidence)))
    with db.conn() as c:
        c.execute(
            "INSERT INTO market_equivalences "
            "(market_slug, provider, provider_market_id, provider_question, "
            " confidence, mapped_by, mapped_at, rejected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(market_slug, provider) DO UPDATE SET "
            " provider_market_id = excluded.provider_market_id, "
            " provider_question = excluded.provider_question, "
            " confidence = excluded.confidence, "
            " mapped_by = excluded.mapped_by, "
            " mapped_at = excluded.mapped_at, "
            " rejected = 0",
            (
                market_slug, provider, provider_market_id, provider_question,
                conf, mapped_by, int(time.time()),
            ),
        )


def mark_equivalence_rejected(market_slug: str, provider: str) -> bool:
    """Admin rejects a bad match. Row stays so the matcher doesn't
    re-suggest the same candidate immediately."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE market_equivalences SET rejected = 1, mapped_by = 'admin_override' "
            "WHERE market_slug = ? AND provider = ?",
            (market_slug, provider),
        )
    return cur.rowcount > 0


def list_low_confidence_equivalences(
    limit: int = 200, max_confidence: float = LOW_CONFIDENCE_THRESHOLD
) -> list[sqlite3.Row]:
    """Admin review queue — rows with confidence below threshold that
    aren't already admin-overridden."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM market_equivalences "
            "WHERE rejected = 0 "
            "  AND mapped_by != 'admin_override' "
            "  AND confidence < ? "
            "ORDER BY confidence ASC, mapped_at DESC "
            "LIMIT ?",
            (max_confidence, limit),
        ).fetchall()


def list_unmatched_active_markets(limit: int = 200) -> list[sqlite3.Row]:
    """Markets with recent snapshots that have NO equivalence row for
    ANY provider yet. Surfaced to admin so they can seed mappings
    manually when the matcher hasn't run or has failed."""
    with db.conn() as c:
        return c.execute(
            "SELECT DISTINCT s.market_slug, s.market_question, s.category "
            "FROM market_snapshots s "
            "LEFT JOIN market_equivalences e ON e.market_slug = s.market_slug "
            "WHERE s.snapshotted_at >= ? "
            "  AND e.market_slug IS NULL "
            "GROUP BY s.market_slug "
            "ORDER BY MAX(s.snapshotted_at) DESC "
            "LIMIT ?",
            (int(time.time()) - 7 * 86400, limit),
        ).fetchall()


def compute_brier_scores(
    *, since_ts: Optional[int] = None, min_samples: int = 1,
) -> dict[str, dict]:
    """Brier score per external provider, against resolved markets.

    For each provider we join ``external_forecasts`` to the
    ``predictions`` table on ``market_slug = market_id`` (the two
    columns use different names but hold the same platform slug) and
    pick rows where the market has ``resolved = 1``. The provider's
    probability is scored against the binary outcome (1 if YES
    resolved correct, 0 if NO resolved correct).

    Brier = mean((probability - outcome)^2). 0.0 is perfect,
    0.25 is a coin-flip, 1.0 is maximally wrong.

    Returned shape: ``{provider: {"samples": int, "brier": float,
    "markets": int}}``. Providers with < ``min_samples`` resolved
    snapshots are omitted so the UI can gate "no data yet" cleanly.

    Gotcha: ``predictions`` is a separate table from the market
    snapshots — it holds extracted source predictions. The
    ``resolved`` + ``resolved_correct`` flags there are the closest
    thing the codebase has to binary market outcomes. If multiple
    predictions resolve the same market, we take the most recent
    authoritative row per slug (each resolved row agrees by
    construction).
    """
    with db.conn() as c:
        # Build a resolution map: slug → 0 / 1 (correct = YES-resolved).
        # Take one row per slug — the predictions table can have many
        # extracted rows per market, but resolved_correct agrees once
        # resolution lands.
        res_rows = c.execute(
            "SELECT market_id AS slug, MAX(resolved_correct) AS outcome "
            "FROM predictions "
            "WHERE resolved = 1 "
            "  AND market_id IS NOT NULL "
            "  AND resolved_correct IS NOT NULL "
            "GROUP BY market_id"
        ).fetchall()
    resolutions = {
        r["slug"]: 1 if int(r["outcome"]) == 1 else 0
        for r in res_rows
    }
    if not resolutions:
        return {}

    scores: dict[str, dict] = {}
    for provider in SUPPORTED_PROVIDERS:
        rows = provider_series_for_scoring(provider, since_ts=since_ts)
        errs: list[float] = []
        markets_hit: set[str] = set()
        for row in rows:
            slug = row["market_slug"]
            outcome = resolutions.get(slug)
            if outcome is None:
                continue
            p = float(row["probability"])
            errs.append((p - outcome) ** 2)
            markets_hit.add(slug)
        if len(errs) < min_samples:
            continue
        scores[provider] = {
            "samples": len(errs),
            "markets": len(markets_hit),
            "brier": sum(errs) / len(errs),
        }
    return scores


def equivalence_summary() -> dict:
    """KPIs for /admin/equivalences header."""
    with db.conn() as c:
        row = c.execute(
            "SELECT "
            " COUNT(*) AS total, "
            " SUM(CASE WHEN rejected = 1 THEN 1 ELSE 0 END) AS rejected, "
            " SUM(CASE WHEN mapped_by = 'admin_override' THEN 1 ELSE 0 END) AS admin_overrides, "
            " SUM(CASE WHEN confidence < ? AND rejected = 0 "
            "            AND mapped_by != 'admin_override' THEN 1 ELSE 0 END) AS low_conf "
            "FROM market_equivalences",
            (LOW_CONFIDENCE_THRESHOLD,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "rejected": int(row["rejected"] or 0),
        "admin_overrides": int(row["admin_overrides"] or 0),
        "low_confidence": int(row["low_conf"] or 0),
    }
