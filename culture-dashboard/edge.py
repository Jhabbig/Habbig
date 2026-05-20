"""Surface culture markets whose underlying topic is surging.

For each cross-source topic cluster we look up:
  * matching Polymarket markets (Jaccard on title keywords)
  * the strongest surge z-score across the topic's items (if any)

An "edge" is then a topic that (a) has at least one matched market and
(b) is surging beyond a threshold — the implication being that attention
is climbing faster than the contract price has caught up to. We can't
verify the price-side claim without a price history (next iteration), so
this view is best read as "markets worth a look right now".
"""

from __future__ import annotations

import os

import cache
import surge_calc
import topics


def _market_match_threshold() -> float:
    try:
        return float(os.environ.get("CULTURE_EDGE_MIN_OVERLAP", "0.25"))
    except ValueError:
        return 0.25


def _signal_threshold() -> float:
    try:
        return float(os.environ.get("CULTURE_EDGE_MIN_SIGNAL", "1.5"))
    except ValueError:
        return 1.5


def compute_topics_with_markets(limit: int = 20) -> list[dict]:
    """Cluster every item in cache, attach matched markets + surge signal."""
    # All currently-live items across every section. limit×2 because spread
    # only matters when items span sources; raw section caps would starve us.
    rows: list[dict] = []
    for section in (
        "memes", "attention", "entertainment", "markets", "news", "language", "lifestyle"
    ):
        rows.extend(cache.get_section(section, limit=80))

    clusters = topics.cluster_topics(rows)
    market_items = [r for r in rows if r.get("section") == "markets"]
    surges = {(s["source"], s["key"]): s["z_score"] for s in surge_calc.compute(limit=200)}

    for c in clusters:
        c["markets"] = _match_markets(c["keywords"], market_items)
        c["surge_signal"] = _topic_surge_signal(c["items"], surges)
    return clusters[:limit]


def compute_edges(limit: int = 20) -> list[dict]:
    """Topics that have matched markets AND a positive surge signal."""
    sig_min = _signal_threshold()
    out = []
    for c in compute_topics_with_markets(limit=200):
        if not c["markets"]:
            continue
        if c["surge_signal"] is None or c["surge_signal"] < sig_min:
            continue
        out.append({
            "label": c["label"],
            "keywords": c["keywords"][:10],
            "sources": c["sources"],
            "sections": c["sections"],
            "spread": c["spread"],
            "surge_signal": c["surge_signal"],
            "markets": c["markets"],
            "items": c["items"][:5],
        })
    out.sort(key=lambda e: e["surge_signal"], reverse=True)
    return out[:limit]


def _match_markets(keywords: list[str], market_items: list[dict]) -> list[dict]:
    """Match by overlap coefficient (|inter| / min(|a|,|b|)), not Jaccard.

    Market titles are short ("Will X announce Y in 2026?"); topic vocabularies
    are wide. Jaccard would underweight a true match because the union grows
    with topic size. Overlap-coef asks "how much of the smaller set is shared?"
    which is the right semantic for "does this market mention the topic?".
    """
    if not market_items or not keywords:
        return []
    kw = set(keywords)
    threshold = _market_match_threshold()
    out = []
    for m in market_items:
        mk = topics.extract_keywords(m)
        if not mk:
            continue
        inter = kw & mk
        if len(inter) < 2:    # need at least 2 shared tokens to avoid noise
            continue
        overlap = len(inter) / min(len(kw), len(mk))
        if overlap < threshold:
            continue
        out.append({
            "title": m.get("title"),
            "url": m.get("url"),
            "volume": m.get("score"),
            "overlap": round(overlap, 2),
            "shared": sorted(inter)[:6],
        })
    out.sort(key=lambda x: x["overlap"], reverse=True)
    return out[:5]


def _topic_surge_signal(items: list[dict], surges: dict) -> float | None:
    """Max z-score across the topic's items, or None if none have history."""
    zs = [surges[(it["source"], it.get("key") or it.get("url") or it.get("title"))]
          for it in items
          if (it["source"], it.get("key") or it.get("url") or it.get("title")) in surges]
    if not zs:
        return None
    return round(max(zs), 2)
