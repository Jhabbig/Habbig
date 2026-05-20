"""Per-event price velocity from the market_prices history.

For each event slug, compute the percentage change in the favorite market's
price over a trailing window (default 24h). Returns None when we don't have
enough history to compute a meaningful delta.

This is the price half of the "edge" calculation: a topic surging hard while
its matched market hasn't moved is the mispricing we're looking for.
"""

from __future__ import annotations

import logging
import os

import cache

log = logging.getLogger(__name__)


def _hours() -> int:
    try:
        return int(os.environ.get("CULTURE_PRICE_VELOCITY_HOURS", "24"))
    except ValueError:
        return 24


def compute(event_slug: str, hours: int | None = None) -> dict | None:
    if not event_slug:
        return None
    h = hours if hours is not None else _hours()
    rows = cache.market_price_history(event_slug, hours=h)
    if len(rows) < 2:
        return None

    def _price(r: dict) -> float | None:
        # Prefer mid_price (more honest than last-trade); fall back to favorite.
        return r.get("mid_price") or r.get("favorite_price")

    prior = _price(rows[0])
    current = _price(rows[-1])
    if prior is None or current is None or prior == 0:
        return None
    pct = (current - prior) / prior
    last_spread = rows[-1].get("spread_bps")
    return {
        "current": round(current, 4),
        "prior": round(prior, 4),
        "pct": round(pct, 4),
        "abs_pct": round(abs(pct), 4),
        "spread_bps": round(last_spread, 1) if last_spread is not None else None,
        "points": len(rows),
        "hours": h,
    }


def trajectory(event_slug: str, hours: int | None = None, max_points: int = 24) -> list[list]:
    """Return [[ts, price], …] downsampled to ~max_points entries for sparklines."""
    if not event_slug:
        return []
    h = hours if hours is not None else _hours()
    rows = cache.market_price_history(event_slug, hours=h)
    if not rows:
        return []
    step = max(1, len(rows) // max_points)
    sampled = rows[::step]
    out = []
    for r in sampled:
        p = r.get("mid_price") or r.get("favorite_price")
        if p is not None:
            out.append([round(r["ts"], 1), round(float(p), 4)])
    # Always include the latest point even if the slice missed it.
    last_p = rows[-1].get("mid_price") or rows[-1].get("favorite_price")
    if last_p is not None and (not out or out[-1][0] != rows[-1]["ts"]):
        out.append([round(rows[-1]["ts"], 1), round(float(last_p), 4)])
    return out
