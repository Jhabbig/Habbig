"""Surface culture markets whose underlying topic is surging.

For each cross-source topic cluster we look up:
  * matching Polymarket markets (Jaccard on title keywords)
  * the strongest surge z-score across the topic's items
  * the matched markets' 24h price velocity from the market_prices table

A real "edge" is a topic with high surge AND a matched market whose price
has not yet moved — attention is climbing faster than the contract has
caught up to. Mispricing score = surge_signal − 10·min(|velocity_pct|);
edges with mispricing > threshold are surfaced and ranked.
"""

from __future__ import annotations

import os

import cache
import position
import price_velocity
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


def _velocity_penalty() -> float:
    """Coefficient applied to |velocity_pct| when computing mispricing score."""
    try:
        return float(os.environ.get("CULTURE_EDGE_VELOCITY_PENALTY", "10"))
    except ValueError:
        return 10.0


def compute_topics_with_markets(limit: int = 20) -> list[dict]:
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
    """Topics where attention is surging AND a matched market hasn't moved."""
    sig_min = _signal_threshold()
    penalty = _velocity_penalty()
    out = []
    for c in compute_topics_with_markets(limit=200):
        if not c["markets"]:
            continue
        if c["surge_signal"] is None or c["surge_signal"] < sig_min:
            continue
        # Markets without price history pass through unscored; the calmest
        # quantifiable market within the edge sets the mispricing score.
        velocities = [m["price_velocity_24h_pct"]
                      for m in c["markets"]
                      if m.get("price_velocity_24h_pct") is not None]
        if velocities:
            min_abs_vel = min(abs(v) for v in velocities)
            mispricing = c["surge_signal"] - penalty * min_abs_vel
        else:
            min_abs_vel = None
            mispricing = c["surge_signal"]
        if mispricing <= 0:
            continue
        markets = c["markets"]
        for m in markets:
            m["position"] = position.suggest(
                m.get("current_price"), round(mispricing, 2), m.get("spread_bps"),
            )
        out.append({
            "label": c["label"],
            "keywords": c["keywords"][:10],
            "sources": c["sources"],
            "sections": c["sections"],
            "spread": c["spread"],
            "surge_signal": c["surge_signal"],
            "min_abs_velocity": round(min_abs_vel, 4) if min_abs_vel is not None else None,
            "mispricing_score": round(mispricing, 2),
            "markets": markets,
            "items": c["items"][:5],
        })
    out.sort(key=lambda e: e["mispricing_score"], reverse=True)
    return out[:limit]


def _max_spread_bps() -> float:
    """Wider spreads = thinner liquidity. Markets above this are filtered."""
    try:
        return float(os.environ.get("CULTURE_MAX_SPREAD_BPS", "500"))
    except ValueError:
        return 500.0


def _match_markets(keywords: list[str], market_items: list[dict]) -> list[dict]:
    """Match by overlap coefficient (|inter| / min(|a|,|b|)), not Jaccard.

    Each matched market is enriched with `price_velocity_24h_pct` and
    `spread_bps` from market_prices, plus a downsampled trajectory for
    the inline sparkline. Markets wider than CULTURE_MAX_SPREAD_BPS are
    dropped — wide spread = thin liquidity = unreliable price signal.
    """
    if not market_items or not keywords:
        return []
    kw = set(keywords)
    threshold = _market_match_threshold()
    max_spread = _max_spread_bps()
    out = []
    for m in market_items:
        mk = topics.extract_keywords(m)
        if not mk:
            continue
        inter = kw & mk
        if len(inter) < 2:
            continue
        overlap = len(inter) / min(len(kw), len(mk))
        if overlap < threshold:
            continue
        extra = m.get("extra") or {}
        slug = extra.get("event_slug") or _slug_from_url(m.get("url") or "")
        vel = price_velocity.compute(slug) if slug else None
        spread = (vel.get("spread_bps") if vel else None) or extra.get("spread_bps")
        # Filter wide-spread markets unless we have no spread data at all.
        if spread is not None and spread > max_spread:
            continue
        out.append({
            "title": m.get("title"),
            "url": m.get("url"),
            "volume": m.get("score"),
            "overlap": round(overlap, 2),
            "shared": sorted(inter)[:6],
            "event_slug": slug or None,
            "favorite_question": extra.get("favorite_question"),
            "current_price": extra.get("mid_price") or extra.get("favorite_price"),
            "spread_bps": spread,
            "price_velocity_24h_pct": vel["pct"] if vel else None,
            "velocity_points": vel["points"] if vel else 0,
            "trajectory": price_velocity.trajectory(slug) if slug else [],
        })
    out.sort(key=lambda x: x["overlap"], reverse=True)
    return out[:5]


def _slug_from_url(url: str) -> str | None:
    """Extract the Polymarket slug from a `polymarket.com/event/<slug>` URL."""
    prefix = "polymarket.com/event/"
    idx = url.find(prefix)
    if idx == -1:
        return None
    return url[idx + len(prefix):].split("/", 1)[0].split("?", 1)[0] or None


def _topic_surge_signal(items: list[dict], surges: dict) -> float | None:
    """Max z-score across the topic's items, or None if none have history."""
    zs = [surges[(it["source"], it.get("key") or it.get("url") or it.get("title"))]
          for it in items
          if (it["source"], it.get("key") or it.get("url") or it.get("title")) in surges]
    if not zs:
        return None
    return round(max(zs), 2)
