"""Predictive validation: did surges on culture-market items actually precede price moves?

For each historical `surge_alerts` entry where the source is `culture_markets`,
we extract the event slug from the item key, then look up the market's price
at the alert timestamp and at alert+24h. If the realised |price change| is at
or above a threshold, we count it as a hit.

This validates the **market-source half** of the surge signal — i.e. does
a volume spike on a culture market lead to a price move within 24h? Topic-
level surges (Reddit/TikTok/etc.) are harder to validate because we don't
persist which topic clusters existed at past timestamps; that requires
re-clustering across time and is left for a later iteration.

Aggregate metrics:
    hit_rate            — fraction of validatable surges with |Δ| ≥ threshold
    weak_rate           — fraction with threshold/2 ≤ |Δ| < threshold
    miss_rate           — fraction with |Δ| < threshold/2
    insufficient_data   — alerts with no post-alert price snapshot in window
"""

from __future__ import annotations

import logging
import os
import time

import cache

log = logging.getLogger(__name__)


def _threshold_pct() -> float:
    try:
        return float(os.environ.get("CULTURE_BACKTEST_HIT_THRESHOLD", "0.05"))
    except ValueError:
        return 0.05


def _window_hours() -> int:
    try:
        return int(os.environ.get("CULTURE_BACKTEST_WINDOW_HOURS", "24"))
    except ValueError:
        return 24


def _slug_from_key(key: str) -> str | None:
    prefix = "polymarket.com/event/"
    idx = key.find(prefix)
    if idx == -1:
        return None
    slug = key[idx + len(prefix):].split("/", 1)[0].split("?", 1)[0]
    return slug or None


def validate(days: int = 30, limit: int = 200) -> dict:
    """Replay culture_markets surge alerts and check the next-24h price move."""
    since = time.time() - days * 86400
    alerts = cache.market_alerts(source="culture_markets", since_ts=since)
    threshold = _threshold_pct()
    weak_threshold = threshold / 2
    window_s = _window_hours() * 3600

    examples: list[dict] = []
    hit = weak = miss = insufficient = 0

    for a in alerts:
        slug = _slug_from_key(a["key"])
        if not slug:
            insufficient += 1
            continue
        at_alert = cache.market_price_at(slug, a["alerted_at"], tolerance_s=window_s / 4)
        after = cache.market_price_at(slug, a["alerted_at"] + window_s, tolerance_s=window_s / 4)
        if not at_alert or not after:
            insufficient += 1
            continue
        p0 = at_alert.get("mid_price") or at_alert.get("favorite_price")
        p1 = after.get("mid_price") or after.get("favorite_price")
        if not p0 or not p1:
            insufficient += 1
            continue
        realised = (p1 - p0) / p0
        abs_r = abs(realised)
        if abs_r >= threshold:
            kind = "hit"
            hit += 1
        elif abs_r >= weak_threshold:
            kind = "weak"
            weak += 1
        else:
            kind = "miss"
            miss += 1
        examples.append({
            "ts": a["alerted_at"],
            "slug": slug,
            "z_score": a["z_score"],
            "price_at_alert": round(float(p0), 4),
            "price_after": round(float(p1), 4),
            "realised_pct": round(realised, 4),
            "classification": kind,
        })

    total = hit + weak + miss
    return {
        "window_days": days,
        "window_hours": _window_hours(),
        "threshold_pct": threshold,
        "total_alerts": len(alerts),
        "validatable": total,
        "hit_rate": round(hit / total, 3) if total else None,
        "weak_rate": round(weak / total, 3) if total else None,
        "miss_rate": round(miss / total, 3) if total else None,
        "insufficient_data": insufficient,
        "examples": sorted(examples, key=lambda e: e["ts"], reverse=True)[:limit],
    }
