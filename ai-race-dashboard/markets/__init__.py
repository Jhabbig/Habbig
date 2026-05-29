"""Market venue aggregator — Polymarket + Kalshi → unified view.

Exposes three functions for the server:
  - get_featured()     → curated events with full multi-outcome trees
  - get_movers()       → top 24h price movers (Polymarket only — Kalshi's
                         public events endpoint doesn't surface a 1d delta)
  - cross_venue_pairs(events) → best-effort same-question pairing for
                         visualizing Polymarket↔Kalshi spreads
"""

from __future__ import annotations

import re

import data as ai_data
from . import kalshi, polymarket


def get_featured() -> dict:
    poly_events = polymarket.fetch_featured(ai_data.AI_POLY_EVENT_SLUGS)
    kalshi_events = kalshi.fetch_featured(ai_data.AI_KALSHI_SERIES)
    events = poly_events + kalshi_events
    # Sort by 24h volume across the whole event so high-traffic things lead.
    events.sort(key=lambda e: e.get("volume_24h_total", 0), reverse=True)
    pairs = cross_venue_pairs(events)
    return {
        "events": events,
        "pairs": pairs,
        "counts": {
            "polymarket": len(poly_events),
            "kalshi": len(kalshi_events),
            "cross_venue_pairs": len(pairs),
        },
    }


def get_movers(min_change: float = 0.05, limit: int = 12) -> dict:
    rows = polymarket.fetch_movers(ai_data.AI_MARKET_KEYWORDS, min_change=min_change)
    return {"movers": rows[:limit], "min_change": min_change}


# ── Cross-venue pairing ──────────────────────────────────────────────────────
# Heuristic: identical-topic events on both venues by tokenized title overlap.
# Conservative — only pair when >=3 substantive tokens match and at least one
# is a named entity (lab name, model name, "AGI").

_STOP = {"the", "a", "an", "of", "in", "on", "by", "to", "for", "and", "or",
         "be", "is", "are", "will", "with", "next", "any", "out", "be", "do",
         "have", "before", "after", "by", "end", "year", "year-end", "yearend",
         "market", "markets"}

_ENTITIES = {
    "openai", "anthropic", "claude", "gpt", "gpt-5", "gpt5",
    "gemini", "deepmind", "grok", "xai", "deepseek", "qwen", "llama",
    "mistral", "agi", "asi", "nvidia", "lmarena", "swe-bench",
}


def _tokens(text: str) -> set[str]:
    t = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in t if len(w) > 2 and w not in _STOP}


def cross_venue_pairs(events: list[dict]) -> list[dict]:
    polys = [e for e in events if e["venue"] == "polymarket"]
    kals = [e for e in events if e["venue"] == "kalshi"]
    pairs: list[dict] = []
    seen_kals: set[str] = set()
    for p in polys:
        p_tokens = _tokens(p.get("title", ""))
        for k in kals:
            if k["slug"] in seen_kals:
                continue
            k_tokens = _tokens(k.get("title", ""))
            overlap = p_tokens & k_tokens
            entity_hit = overlap & _ENTITIES
            # Two paths: entity-anchored (≥2 tokens incl. entity), or strict
            # (≥3 tokens regardless of entity). The entity gate prevents false
            # positives like generic dates lining up across unrelated markets.
            if not (entity_hit and len(overlap) >= 2) and len(overlap) < 3:
                continue
            if not entity_hit:
                continue
            # Pair the dominant binary outcome — use the highest-priced market
            # on each side. Spread = polymarket yes − kalshi yes (positive ⇒
            # Polymarket overpriced relative to Kalshi).
            p_yes = p["markets"][0]["yes_price"] if p["markets"] else None
            k_yes = k["markets"][0]["yes_price"] if k["markets"] else None
            if p_yes is None or k_yes is None:
                continue
            pairs.append({
                "topic": " ".join(sorted(overlap & _ENTITIES))[:60],
                "polymarket": {
                    "title": p["title"], "url": p["url"], "yes_price": p_yes,
                    "question": p["markets"][0]["question"],
                },
                "kalshi": {
                    "title": k["title"], "url": k["url"], "yes_price": k_yes,
                    "question": k["markets"][0]["question"],
                },
                "spread": round(p_yes - k_yes, 4),
                "overlap_tokens": sorted(overlap),
            })
            seen_kals.add(k["slug"])
            break
    pairs.sort(key=lambda r: abs(r["spread"]), reverse=True)
    return pairs
