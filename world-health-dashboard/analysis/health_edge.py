"""Cross-source health-market aggregator.

Combines Polymarket, Kalshi, and Manifold health markets into a single feed
with a stable normalized shape, sorted by activity. Also performs simple
cross-venue matching when the same question appears on multiple platforms,
so the user can spot inter-venue spreads (genuine arbitrage candidates).

Matching is done by Jaccard similarity over question tokens — not perfect,
but catches obvious duplicates like "Will WHO declare PHEIC for X by 2026?"
appearing on both Polymarket and Kalshi. A confidence threshold of 0.45
avoids noisy false positives.

This module does NOT compute model fair values yet — that's Phase 4. For now
the "edge" surface is just inter-venue divergence + the raw market table.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from ingestion import kalshi_health, manifold_health, polymarket_health

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "by",
    "will", "be", "is", "are", "was", "were", "have", "has", "had",
    "this", "that", "these", "those", "it", "its", "any", "with",
    "what", "when", "before", "after", "during", "until", "year",
    "yes", "no", "if", "than", "more", "less",
}


def _tokenize(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s or "") if t.lower() not in _STOPWORDS and len(t) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def fetch_all() -> dict:
    """Pull all three venues in parallel and return a combined payload."""
    sources = {
        "polymarket": polymarket_health.fetch,
        "kalshi":     kalshi_health.fetch,
        "manifold":   manifold_health.fetch,
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn) for name, fn in sources.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as exc:
                log.warning("Source %s failed: %s", name, exc)
                results[name] = {"markets": [], "fetched_at": 0, "error": str(exc)}
    return results


def _matches(markets_a: list[dict], markets_b: list[dict], threshold: float = 0.45) -> list[dict]:
    """Return cross-venue matches above the similarity threshold."""
    out: list[dict] = []
    tok_a = [(m, _tokenize(m["question"])) for m in markets_a]
    tok_b = [(m, _tokenize(m["question"])) for m in markets_b]
    for a, ta in tok_a:
        best = None
        best_sim = threshold
        for b, tb in tok_b:
            s = _jaccard(ta, tb)
            if s > best_sim:
                best_sim = s
                best = b
        if best:
            out.append({
                "a": {"source": a["source"], "id": a["id"], "question": a["question"],
                      "probability": a["probability"], "url": a["url"]},
                "b": {"source": best["source"], "id": best["id"], "question": best["question"],
                      "probability": best["probability"], "url": best["url"]},
                "similarity": round(best_sim, 2),
                "spread": round((a["probability"] or 0) - (best["probability"] or 0), 3)
                          if a["probability"] is not None and best["probability"] is not None
                          else None,
            })
    return out


def aggregate() -> dict:
    """Combined market feed + cross-venue matches."""
    src = fetch_all()
    poly = src["polymarket"].get("markets", [])
    kal = src["kalshi"].get("markets", [])
    man = src["manifold"].get("markets", [])

    all_markets = poly + kal + man
    # Sort by USD-ish volume — Manifold is play money, but we leave it ranked
    # alongside since the relative ordering inside each venue is meaningful.
    all_markets.sort(key=lambda x: -(x.get("volume") or 0.0))

    matches = []
    matches.extend(_matches(poly, kal))
    matches.extend(_matches(poly, man))
    matches.extend(_matches(kal, man))
    # Sort matches by absolute spread (genuine pricing differences first).
    matches.sort(key=lambda x: -abs(x.get("spread") or 0))

    return {
        "markets": all_markets,
        "by_source": {
            "polymarket": poly,
            "kalshi":     kal,
            "manifold":   man,
        },
        "counts": {
            "polymarket": len(poly),
            "kalshi":     len(kal),
            "manifold":   len(man),
            "total":      len(all_markets),
        },
        "cross_venue_matches": matches[:30],
        "fetched_at": max(
            src["polymarket"].get("fetched_at", 0),
            src["kalshi"].get("fetched_at", 0),
            src["manifold"].get("fetched_at", 0),
        ),
        "errors": {
            name: r.get("error")
            for name, r in src.items()
            if r.get("error")
        },
    }
