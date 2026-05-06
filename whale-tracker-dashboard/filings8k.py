"""8-K filter for M&A signal.

8-Ks are catch-all "material event" filings — only a small slice are M&A.
We score using two channels:

  1. Reported items. M&A typically uses:
       1.01  Entry into a Material Definitive Agreement
       2.01  Completion of Acquisition or Disposition of Assets
       8.01  Other Events
     Item 1.01 alone is the strongest pre-deal signal.

  2. Headline keywords on the filing's first heading / Item 1.01 text.

We don't try to perfectly classify; we hand a numeric `ma_score` to the
synthesis layer and let the UI sort.
"""

from __future__ import annotations

import re

ITEM_WEIGHTS = {
    "1.01": 3.0,  # material definitive agreement — strongest pre-deal signal
    "2.01": 2.0,  # completion of acquisition / disposition
    "8.01": 0.5,
}

KEYWORD_WEIGHTS = [
    (re.compile(r"\bdefinitive\s+agreement\b", re.I), 2.5),
    (re.compile(r"\bagreement\s+and\s+plan\s+of\s+merger\b", re.I), 3.0),
    (re.compile(r"\bmerger\s+agreement\b", re.I), 2.5),
    (re.compile(r"\btender\s+offer\b", re.I), 2.0),
    (re.compile(r"\bbusiness\s+combination\b", re.I), 1.5),
    (re.compile(r"\bto\s+(acquire|be\s+acquired)\b", re.I), 1.5),
    (re.compile(r"\bstock\s+purchase\s+agreement\b", re.I), 1.5),
    (re.compile(r"\basset\s+purchase\s+agreement\b", re.I), 1.0),
    (re.compile(r"\bgoing[- ]private\b", re.I), 2.0),
    (re.compile(r"\bspin[- ]off\b", re.I), 1.0),
]

ITEM_PATTERN = re.compile(r"item\s+(\d{1,2}\.\d{2})", re.I)


def parse_items_from_summary(summary: str) -> list[str]:
    """The Atom feed `summary` field on an 8-K usually lists items as
    "Items 1.01, 2.01, 9.01". Extract them."""
    if not summary:
        return []
    return list(dict.fromkeys(ITEM_PATTERN.findall(summary)))


def score_8k(items: list[str], headline: str = "", body_excerpt: str = "") -> float:
    score = 0.0
    for it in items:
        score += ITEM_WEIGHTS.get(it, 0.0)
    text = f"{headline}\n{body_excerpt}"
    for pat, w in KEYWORD_WEIGHTS:
        if pat.search(text):
            score += w
    # Cap to keep the leaderboard sortable.
    return round(min(score, 12.0), 2)


def looks_like_ma(items: list[str], headline: str = "", body_excerpt: str = "") -> bool:
    return score_8k(items, headline, body_excerpt) >= 2.0
