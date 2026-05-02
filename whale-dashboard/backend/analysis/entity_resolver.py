from __future__ import annotations
"""Resolve a CIK / filer name to a parent entity.

Strategy:
    1. Exact CIK match in cik_map (the curated seed handles ~80% of AUM).
    2. Name-based fuzzy match against existing entities (parent_name + sub_names).
       If confidence >= 0.9, auto-create a cik_map row at that confidence.
    3. Otherwise, create a new entity from the filer name (lowest priority —
       these need manual review).

The fuzzy match uses simple normalization (lowercase, strip punctuation, drop
common suffixes) plus token Jaccard. We deliberately don't use rapidfuzz here
to keep dependencies light; the seed list catches the names that matter.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from database import get_conn, map_cik, upsert_entity

logger = logging.getLogger(__name__)

_SUFFIXES = re.compile(
    r"\b(inc|llc|lp|l\.p\.|ltd|limited|plc|corp|corporation|company|co|"
    r"holdings|holding|group|partners|capital|management|mgmt|advisors|"
    r"advisers|na|n\.a\.|ag|sa|s\.a\.|sarl|gmbh)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9\s]+")


def _normalize(name: str) -> str:
    s = name.lower().strip()
    s = _SUFFIXES.sub("", s)
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _jaccard(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class ResolveResult:
    entity_id: int
    confidence: float
    created: bool


def resolve(cik: int, filer_name: str, authority: str = "13F") -> ResolveResult:
    """Map a CIK + filer name to an entity_id.

    Side effect: writes to cik_map (and possibly entities) so subsequent calls
    with the same CIK are O(1).
    """
    with get_conn() as conn:
        # Path 1: already mapped.
        row = conn.execute(
            "SELECT entity_id, confidence FROM cik_map WHERE cik=?", (cik,)
        ).fetchone()
        if row:
            return ResolveResult(int(row["entity_id"]), float(row["confidence"]), False)

        # Path 2: fuzzy match against known entities/sub-names.
        norm_filer = _normalize(filer_name)
        candidates = conn.execute(
            "SELECT id, parent_name FROM entities"
        ).fetchall()
        sub_names = conn.execute(
            "SELECT entity_id, sub_name FROM cik_map"
        ).fetchall()

        best_id: Optional[int] = None
        best_score = 0.0
        for c in candidates:
            score = _jaccard(norm_filer, _normalize(c["parent_name"]))
            if score > best_score:
                best_score = score
                best_id = int(c["id"])
        for s in sub_names:
            score = _jaccard(norm_filer, _normalize(s["sub_name"]))
            if score > best_score:
                best_score = score
                best_id = int(s["entity_id"])

        if best_id is not None and best_score >= 0.9:
            map_cik(cik=cik, entity_id=best_id, sub_name=filer_name,
                    filing_authority=authority, confidence=best_score)
            return ResolveResult(best_id, best_score, False)

    # Path 3: new entity (low confidence — needs manual review later).
    slug = re.sub(r"[^a-z0-9]+", "_", _normalize(filer_name)).strip("_")[:60]
    if not slug:
        slug = f"cik_{cik}"
    entity_id = upsert_entity(slug=slug, parent_name=filer_name, entity_type=None,
                              description="Auto-created — review and merge if duplicate.")
    map_cik(cik=cik, entity_id=entity_id, sub_name=filer_name,
            filing_authority=authority, confidence=0.5)
    logger.info("entity_resolver: auto-created entity slug=%s cik=%d (low confidence)",
                slug, cik)
    return ResolveResult(entity_id, 0.5, True)
