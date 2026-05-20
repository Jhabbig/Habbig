"""
Match Polymarket prediction markets to dossier entities.

Given an entity (with aliases) and the cached list of markets returned by
`fetch_polymarket()` in `server.py`, return the markets whose
question / slug / category mention the entity by name or alias.
"""
from __future__ import annotations

import re


def _word_boundary_match(text_lower: str, term: str) -> bool:
    if not term:
        return False
    return re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", text_lower) is not None


def _search_terms(entity: dict) -> list[str]:
    raw: list[str] = []
    name = (entity.get("name") or "").strip()
    if name:
        raw.append(name)
    for a in entity.get("aliases") or []:
        a = (a or "").strip()
        if a:
            raw.append(a)
    # Dedupe (case-insensitive) and drop terms shorter than 3 chars
    # to avoid noisy hits like "us"/"un" matching mid-word substrings —
    # the existing extractor accepts them but here we're scanning short
    # market questions where false positives bite harder.
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        low = t.lower()
        if len(low) < 3 or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def markets_for_entity(entity: dict, markets: list[dict], limit: int = 5) -> list[dict]:
    """Return up to `limit` markets matching the entity, sorted by 24h volume."""
    terms = _search_terms(entity)
    if not terms or not markets:
        return []
    hits: list[dict] = []
    for m in markets:
        haystack = " ".join([
            (m.get("question") or ""),
            (m.get("slug") or "").replace("-", " "),
            (m.get("category") or ""),
        ]).lower()
        if not haystack.strip():
            continue
        if any(_word_boundary_match(haystack, t) for t in terms):
            hits.append(m)
    hits.sort(key=lambda x: x.get("volume_24h") or 0, reverse=True)
    return hits[:limit]
