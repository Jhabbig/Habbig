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
    prior = rows[0]["favorite_price"]
    current = rows[-1]["favorite_price"]
    if prior is None or current is None or prior == 0:
        return None
    pct = (current - prior) / prior
    return {
        "current": round(current, 4),
        "prior": round(prior, 4),
        "pct": round(pct, 4),
        "abs_pct": round(abs(pct), 4),
        "points": len(rows),
        "hours": h,
    }
