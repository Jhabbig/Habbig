"""Cross-source topic clustering.

Takes items from across every section and groups them by overlapping
keywords. A cluster that spans multiple sources (Reddit + TikTok +
Wikipedia all mentioning "barbie") is a stronger cultural signal than any
single source.

Algorithm: greedy centroid-based clustering on Jaccard token-set
similarity. No ML deps — just a stopword list and regex tokenisation.
Tunable threshold via env (`CULTURE_TOPIC_MIN_OVERLAP`, default 0.30).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any

_STOPWORDS = {
    # Standard English stopwords (compact)
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had",
    "are", "was", "were", "will", "would", "what", "which", "when", "who",
    "how", "why", "into", "than", "then", "them", "they", "their", "there",
    "your", "you", "his", "her", "she", "him", "but", "not", "all", "any",
    "can", "out", "one", "two", "new", "now", "get", "got", "say", "says",
    "said", "just", "more", "most", "only", "also", "after", "over", "back",
    "down", "off", "still", "such", "some", "even", "way", "see", "make",
    "made", "took", "take", "going", "goes", "went", "very", "much", "many",
    # Platform/dashboard noise
    "tiktok", "instagram", "reddit", "twitter", "youtube", "video", "videos",
    "post", "posts", "viral", "trending", "fyp", "reels", "meme", "memes",
    "watch", "subscribe", "follow", "link", "bio", "feat", "feat.", "ft",
    # Time / generic noise
    "today", "yesterday", "year", "years", "week", "month", "day", "amp",
    "ep", "episode", "season", "vol", "volume",
}

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _minimum_overlap() -> float:
    try:
        return float(os.environ.get("CULTURE_TOPIC_MIN_OVERLAP", "0.30"))
    except ValueError:
        return 0.30


def extract_keywords(item: dict[str, Any]) -> set[str]:
    """Return the significant token set for an item."""
    text = item.get("title") or ""
    extra = item.get("extra") or {}
    if isinstance(extra, dict):
        hashtags = extra.get("hashtags")
        if isinstance(hashtags, list):
            text += " " + " ".join(str(h or "") for h in hashtags)
        # Generic catch-all: any string fields in extra (author handle, sub, …)
        for v in extra.values():
            if isinstance(v, str) and len(v) < 60:
                text += " " + v
    tokens = set(_TOKEN_RE.findall(text.lower()))
    return tokens - _STOPWORDS


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def cluster_topics(
    items: list[dict[str, Any]],
    min_overlap: float | None = None,
    min_tokens: int = 2,
    min_spread: int = 2,
) -> list[dict[str, Any]]:
    """Group items into cross-source topic clusters.

    Only items with ≥`min_tokens` significant words enter the clustering
    pass (otherwise single-word titles like "Sabrina" merge everything).
    Clusters with fewer than `min_spread` distinct sources are dropped —
    a single-source cluster isn't really a cross-platform topic.
    """
    threshold = min_overlap if min_overlap is not None else _minimum_overlap()
    tagged = []
    for it in items:
        tokens = extract_keywords(it)
        if len(tokens) >= min_tokens:
            tagged.append((it, tokens))
    # High-score items seed the clusters so labels skew toward strong signals.
    tagged.sort(key=lambda x: float(x[0].get("score") or 0), reverse=True)

    clusters: list[dict[str, Any]] = []
    for item, tokens in tagged:
        best: dict | None = None
        best_overlap = 0.0
        for c in clusters:
            o = _jaccard(tokens, c["_tokens"])
            if o >= threshold and o > best_overlap:
                best = c
                best_overlap = o
        if best is not None:
            best["items"].append(item)
            best["_tokens"] |= tokens
            best["_token_counts"].update(tokens)
            best["sources"].add(item.get("source"))
            best["sections"].add(item.get("section"))
        else:
            clusters.append({
                "items": [item],
                "_tokens": set(tokens),
                "_token_counts": Counter(tokens),
                "sources": {item.get("source")},
                "sections": {item.get("section")},
            })

    out: list[dict[str, Any]] = []
    for c in clusters:
        if len(c["sources"]) < min_spread:
            continue
        out.append({
            "label": _pick_label(c["_token_counts"]),
            "keywords": sorted(c["_tokens"]),
            "items": c["items"][:20],
            "sources": sorted(s for s in c["sources"] if s),
            "sections": sorted(s for s in c["sections"] if s),
            "spread": len(c["sources"]),
            "total_score": sum(float(it.get("score") or 0) for it in c["items"]),
        })
    # Spread first, then total score — cross-platform > raw volume.
    out.sort(key=lambda c: (c["spread"], c["total_score"]), reverse=True)
    return out


def _pick_label(counts: Counter[str]) -> str:
    """Pick the most-frequent non-stopword token as the cluster label."""
    if not counts:
        return "(untitled)"
    return counts.most_common(1)[0][0]
