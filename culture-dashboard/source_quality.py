"""Per-source predictive quality.

When a topic cluster surges, each source that contributed to the cluster
gets attributed for the outcome. If the cluster's matched markets actually
moved within `window_hours`, every contributing source records a hit;
otherwise a miss. Higher hit rate → more predictive source.

We also fold in `culture_markets`-direct surge alerts so the markets
source itself gets scored on the same axis as Reddit / TikTok / Wikipedia /
etc.

This is a Bayesian-ish quality readout, not a recommendation engine —
small sample sizes are unreliable; the API exposes the raw counts so
callers can decide their own significance bar.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Any

import cache

log = logging.getLogger(__name__)


def _hit_threshold() -> float:
    try:
        return float(os.environ.get("CULTURE_BACKTEST_HIT_THRESHOLD", "0.05"))
    except ValueError:
        return 0.05


def _window_hours() -> int:
    try:
        return int(os.environ.get("CULTURE_BACKTEST_WINDOW_HOURS", "24"))
    except ValueError:
        return 24


def compute(days: int = 30, min_signal: float = 1.5) -> dict[str, Any]:
    """Walk every snapshot+alert in the window, attribute outcomes to sources."""
    since = time.time() - days * 86400
    threshold = _hit_threshold()
    weak_threshold = threshold / 2
    window_s = _window_hours() * 3600

    per_source = defaultdict(lambda: {"hit": 0, "weak": 0, "miss": 0,
                                       "insufficient": 0})

    # --- Topic snapshots: attribute the basket outcome to each source ---
    for snap in cache.topic_snapshots_since(since_ts=since, min_signal=min_signal):
        if not snap["market_slugs"] or not snap["sources"]:
            continue
        realised = _basket_move(snap["market_slugs"], snap["ts"], window_s)
        outcome = _classify(realised, threshold, weak_threshold)
        for src in snap["sources"]:
            per_source[src][outcome] += 1

    # --- Market-direct surge alerts: attribute to the culture_markets source ---
    for a in cache.market_alerts(source="culture_markets", since_ts=since):
        slug = _slug_from_key(a["key"])
        if not slug:
            continue
        at = cache.market_price_at(slug, a["alerted_at"], tolerance_s=window_s / 4)
        after = cache.market_price_at(slug, a["alerted_at"] + window_s,
                                       tolerance_s=window_s / 4)
        realised = _delta(at, after)
        outcome = _classify(realised, threshold, weak_threshold)
        per_source["culture_markets"][outcome] += 1

    # --- Aggregate ---
    out = []
    for src, counts in per_source.items():
        validatable = counts["hit"] + counts["weak"] + counts["miss"]
        out.append({
            "source": src,
            "hit": counts["hit"], "weak": counts["weak"], "miss": counts["miss"],
            "insufficient": counts["insufficient"],
            "validatable": validatable,
            "hit_rate": round(counts["hit"] / validatable, 3) if validatable else None,
        })
    # Rank: hit rate primarily, sample size as tie-breaker.
    out.sort(key=lambda s: (s["hit_rate"] or -1, s["validatable"]), reverse=True)
    return {
        "days": days,
        "window_hours": _window_hours(),
        "threshold_pct": threshold,
        "sources": out,
    }


def _basket_move(slugs: list[str], ts: float, window_s: float) -> float | None:
    moves = []
    for slug in slugs:
        at = cache.market_price_at(slug, ts, tolerance_s=window_s / 4)
        after = cache.market_price_at(slug, ts + window_s, tolerance_s=window_s / 4)
        d = _delta(at, after)
        if d is not None:
            moves.append(d)
    return sum(moves) / len(moves) if moves else None


def _delta(before: dict | None, after: dict | None) -> float | None:
    if not before or not after:
        return None
    p0 = before.get("mid_price") or before.get("favorite_price")
    p1 = after.get("mid_price") or after.get("favorite_price")
    if not p0 or not p1:
        return None
    return (p1 - p0) / p0


def _classify(realised: float | None, threshold: float, weak_threshold: float) -> str:
    if realised is None:
        return "insufficient"
    a = abs(realised)
    if a >= threshold:
        return "hit"
    if a >= weak_threshold:
        return "weak"
    return "miss"


def _slug_from_key(key: str) -> str | None:
    prefix = "polymarket.com/event/"
    idx = key.find(prefix)
    if idx == -1:
        return None
    slug = key[idx + len(prefix):].split("/", 1)[0].split("?", 1)[0]
    return slug or None
