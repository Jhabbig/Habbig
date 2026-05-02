from __future__ import annotations
"""Keyword-based intent classifier for Schedule 13D Item 4 ("Purpose of Transaction").

This is deliberately not ML — Item 4 prose is short, formal, and uses a
small vocabulary across activist filers. A weighted keyword score gets us
80%+ of the way to a useful label for free, without training data.

Classes (mutually exclusive, highest score wins):
    acquisition       acquirer signalling intent to buy the rest of the company
    activist          activist seeking board/strategy changes
    governance        narrower: governance/proxy/say-on-pay focus
    spinoff           pushing for separation, sale of unit, breakup
    passive           explicitly disclaims intent ("for investment purposes only")
    unknown           insufficient signal

Output is also a `score` in [0, 1] — caller can require score >= 0.4 to
trust the label, otherwise treat as 'unknown'.
"""

import re
from typing import Tuple

# Each class is a list of (regex, weight) — weights are roughly:
#   1.0  unique to this class
#   0.6  strong signal but ambiguous
#   0.3  weak corroborating signal
_RULES: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "acquisition": [
        (re.compile(r"\bacquire\s+all\b", re.I), 1.0),
        (re.compile(r"\btake[\s-]+private\b", re.I), 1.0),
        (re.compile(r"\bgo[\s-]+private\b", re.I), 1.0),
        (re.compile(r"\bbusiness\s+combination\b", re.I), 0.7),
        (re.compile(r"\bmerger\b", re.I), 0.6),
        (re.compile(r"\btender\s+offer\b", re.I), 0.7),
        (re.compile(r"\b(consummate|complete)\s+the\s+(transaction|merger)\b", re.I), 0.8),
        (re.compile(r"\bproposal\s+to\s+(acquire|purchase)\b", re.I), 0.9),
    ],
    "activist": [
        (re.compile(r"\bnominate\s+directors?\b", re.I), 1.0),
        (re.compile(r"\bproxy\s+(contest|fight|solicitation)\b", re.I), 0.9),
        (re.compile(r"\bboard\s+(of\s+directors|composition|seats?)\b", re.I), 0.6),
        (re.compile(r"\b(operational|strategic)\s+(changes|alternatives|review)\b", re.I), 0.7),
        (re.compile(r"\bshareholder\s+value\b", re.I), 0.4),
        (re.compile(r"\bengage(ment)?\s+with\s+(management|the\s+(?:board|company))\b", re.I), 0.8),
        (re.compile(r"\bconstructive\s+dialogue\b", re.I), 0.4),
    ],
    "governance": [
        (re.compile(r"\bsay[\s-]on[\s-]pay\b", re.I), 1.0),
        (re.compile(r"\bexecutive\s+compensation\b", re.I), 0.6),
        (re.compile(r"\bcorporate\s+governance\b", re.I), 0.7),
        (re.compile(r"\b(declassify|annual\s+election\s+of\s+directors)\b", re.I), 0.7),
        (re.compile(r"\bmajority\s+voting\s+standard\b", re.I), 0.6),
    ],
    "spinoff": [
        (re.compile(r"\bspin[\s-]?off\b", re.I), 1.0),
        (re.compile(r"\bbreak[\s-]?up\b", re.I), 0.9),
        (re.compile(r"\bsale\s+of\s+(business\s+)?(units?|divisions?|segments?|assets?)\b", re.I), 0.8),
        (re.compile(r"\bstrategic\s+alternatives?\b", re.I), 0.6),
        (re.compile(r"\bdivest(iture|ment|s)?\b", re.I), 0.7),
    ],
    "passive": [
        (re.compile(r"\bfor\s+investment\s+purposes\s+only\b", re.I), 1.0),
        (re.compile(r"\bsolely\s+for\s+investment\b", re.I), 1.0),
        (re.compile(r"\bordinary\s+course\s+of\s+business\b", re.I), 0.5),
        (re.compile(r"\bno\s+(plans?|proposals?)\s+to\b", re.I), 0.4),
        (re.compile(r"\bdoes\s+not\s+have\s+any\s+(plans?|proposals?)\b", re.I), 0.5),
    ],
}


def classify(text: str) -> Tuple[str, float]:
    """Return (label, score). Score is in [0, 1] roughly; ~0.4 is a sane
    threshold above which to trust the label."""
    if not text:
        return "unknown", 0.0
    body = text[:6000]  # cap

    scores: dict[str, float] = {}
    for cls, rules in _RULES.items():
        s = 0.0
        for pat, weight in rules:
            if pat.search(body):
                s += weight
        scores[cls] = s

    if not scores or max(scores.values()) == 0:
        return "unknown", 0.0

    label, raw = max(scores.items(), key=lambda kv: kv[1])
    # Normalize: divide by typical ceiling of ~3.0 to get a 0-1 number.
    norm = min(1.0, raw / 3.0)
    return label, round(norm, 3)


def backfill_existing() -> int:
    """Classify any activist_filings rows that haven't been labelled yet."""
    from database import get_conn
    n = 0
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, intent_summary FROM activist_filings
                WHERE intent_class IS NULL AND intent_summary IS NOT NULL"""
        ).fetchall()
        for r in rows:
            label, score = classify(r["intent_summary"] or "")
            conn.execute(
                "UPDATE activist_filings SET intent_class=?, intent_score=? WHERE id=?",
                (label, score, r["id"]),
            )
            n += 1
    return n
