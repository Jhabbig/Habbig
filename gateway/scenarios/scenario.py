"""Conditional-probability / scenario engine.

Given an anchor market + a hypothetical outcome, estimate how each
correlated market's implied probability would shift.

The shift model is intentionally simple and directional — we're showing
traders what *tends to move together*, not forecasting actual outcomes.
Every response ships with the mandatory disclaimer.

Shift formula:

    expected_shift = r * (outcome_distance) * volatility_factor

  r               Pearson correlation on hourly deltas
  outcome_distance  anchor's distance from current_price to the hypothetical
                    resolution (1.0 or 0.0). YES ⇒ (1 − current), NO ⇒ current.
  volatility_factor min(1, 4 * stdev_of_other_market_deltas)

The 4× in volatility_factor is a heuristic: hourly-delta stdev is
typically 0.01–0.05 for liquid markets, so the factor maps that range
to ≈[0.04, 0.20]. We clamp to [0, 1] and cap the final shift to ±30 pp
so a high-r × high-volatility pair can't claim a 70 pp swing.

Outputs a dict the route can pass straight to JSON.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from scenarios.correlation import compute_market_correlations


log = logging.getLogger("scenarios.scenario")


DISCLAIMER = (
    "Correlations are derived from historical market movement and do not "
    "imply causation. Actual outcomes may differ significantly."
)
MAX_SHIFT = 0.30  # cap expected shift at 30pp regardless of r × volatility
VOLATILITY_SCALE = 4.0  # mapping hourly delta stdev → multiplier


# ── Pure shift math ─────────────────────────────────────────────────────────


def estimate_shift(
    *,
    correlation: float,
    anchor_current_price: float,
    hypothetical_outcome: str,
    other_volatility: float,
) -> float:
    """Return the signed expected probability shift for the other market.

    ``hypothetical_outcome`` is "yes" or "no". Anchor resolving YES moves
    the anchor's price from current → 1.0, so the distance is (1 − current).
    """
    outcome = (hypothetical_outcome or "").strip().lower()
    # Signed distance: YES pushes anchor → 1.0 (positive move), NO pushes
    # anchor → 0.0 (negative move). A positively-correlated market moves
    # WITH the anchor — so positive r × negative distance must yield
    # a negative shift for the NO branch.
    if outcome == "yes":
        distance = max(0.0, 1.0 - float(anchor_current_price))
    elif outcome == "no":
        distance = -max(0.0, float(anchor_current_price))
    else:
        return 0.0

    vol_factor = max(0.0, min(1.0, VOLATILITY_SCALE * float(other_volatility or 0.0)))
    # distance carries the anchor's move direction; correlation carries the
    # other market's co-movement; vol_factor scales by the other market's
    # own volatility. abs(vol_factor) guards against the sign already being
    # in distance (avoids double-negation).
    raw = float(correlation) * distance * abs(vol_factor)
    return max(-MAX_SHIFT, min(MAX_SHIFT, raw))


def _apply_shift(current_price: float, shift: float) -> float:
    """Clamp the projected price to [0, 1] so the UI never shows nonsense."""
    return max(0.0, min(1.0, float(current_price) + float(shift)))


# ── Public entrypoint ──────────────────────────────────────────────────────


async def compute_scenario(
    anchor_slug: str,
    hypothetical_outcome: str,
    *,
    min_abs: float = 0.25,
    days: int = 90,
    limit: int = 30,
    anchor_current_price: Optional[float] = None,
) -> dict:
    """Compute the expected shifts for every market correlated with *anchor*.

    If ``anchor_current_price`` is not provided, we'll look it up from the
    most recent snapshot in ``compute_market_correlations`` response — which
    doesn't include the anchor, so we do one extra read here from the cache
    layer. Keeping the signature minimal: tests and HTTP routes pass the
    price they already have.
    """
    outcome = (hypothetical_outcome or "").strip().lower()
    if outcome not in ("yes", "no"):
        return {
            "anchor_slug": anchor_slug,
            "hypothetical": outcome,
            "error": "hypothetical_outcome must be 'yes' or 'no'",
            "disclaimer": DISCLAIMER,
        }

    correlations = await compute_market_correlations(
        anchor_slug, min_abs=min_abs, days=days, limit=limit,
    )

    # Fall back to the anchor's most recent snapshot if caller didn't pass
    # a current price. We already have open DB access via the correlation
    # reader, but keeping this wrapper clean: just fetch one row.
    if anchor_current_price is None:
        anchor_current_price = _fetch_anchor_price(anchor_slug)
    if anchor_current_price is None:
        return {
            "anchor_slug": anchor_slug,
            "hypothetical": outcome,
            "shifts": [],
            "disclaimer": DISCLAIMER,
            "note": "Anchor has no recent price snapshot — shift estimation skipped.",
        }

    anchor_current_price = max(0.0, min(1.0, float(anchor_current_price)))

    shifts: list[dict] = []
    for corr in correlations:
        if corr.get("current_price") is None:
            continue
        shift = estimate_shift(
            correlation=corr["correlation"],
            anchor_current_price=anchor_current_price,
            hypothetical_outcome=outcome,
            other_volatility=corr.get("volatility") or 0.0,
        )
        projected = _apply_shift(corr["current_price"], shift)
        shifts.append({
            "slug": corr["slug"],
            "question": corr["question"],
            "category": corr.get("category"),
            "correlation": corr["correlation"],
            "sample_size": corr.get("sample_size"),
            "current_price": round(corr["current_price"], 4),
            "expected_shift": round(shift, 4),
            "projected_price": round(projected, 4),
        })

    # Sort by absolute shift so "biggest mover" is on top of the table.
    shifts.sort(key=lambda s: abs(s["expected_shift"]), reverse=True)

    return {
        "anchor_slug": anchor_slug,
        "hypothetical": outcome,
        "anchor_current_price": round(anchor_current_price, 4),
        "shifts": shifts,
        "computed_at": int(time.time()),
        "disclaimer": DISCLAIMER,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fetch_anchor_price(slug: str) -> Optional[float]:
    if not slug:
        return None
    import os, sqlite3
    from pathlib import Path

    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        db_path = Path(override)
        if not db_path.is_absolute():
            db_path = Path(__file__).parent.parent / db_path
    else:
        db_path = Path(__file__).parent.parent / "auth.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT yes_price FROM market_snapshots WHERE market_slug = ? "
            "ORDER BY snapshotted_at DESC LIMIT 1",
            (slug,),
        ).fetchone()
        if row and row["yes_price"] is not None:
            return float(row["yes_price"])
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None
