"""Shared data model for every scraper.

Every scraper returns a list of `Item` (as plain dicts via `Item.to_dict()`)
so the cache, the API layer and the front-end see one uniform shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ── Section keys (single source of truth) ──────────────────────────────────
# The frontend, the index calculator and the scrapers all key off these.
SECTIONS: tuple[str, ...] = (
    "memes",          # TikTok / Instagram / Reddit memes / KYM
    "attention",      # Google Trends, Wikipedia, X trending, YouTube trending
    "entertainment",  # Box office, streaming top 10, music charts, Steam
    "markets",        # Polymarket / Kalshi culture-bucket contracts
    "news",           # Top headlines + sentiment
    "language",       # Slang, word-of-the-day, Substack rising
    "lifestyle",      # Books, fashion, food, religion (slow-changing)
    "composite",      # The single culture-index number (synthetic)
)


@dataclass
class Item:
    """One row of cultural signal."""

    section: str                       # one of SECTIONS
    source: str                        # e.g. "tiktok_trending", "reddit_memes"
    title: str
    url: Optional[str] = None
    image: Optional[str] = None
    summary: Optional[str] = None
    score: float = 0.0                 # source-specific popularity score
    velocity: float = 0.0              # change-over-time, if known
    fetched_at: float = 0.0            # unix seconds, set by cache layer
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Strip None for a tighter wire payload.
        return {k: v for k, v in d.items() if v is not None}
