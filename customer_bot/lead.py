"""Raw lead dataclass passed between sources and the runner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RawLead:
    source: str          # 'reddit' | 'hn' | 'polymarket'
    source_id: str       # globally unique within source (used for dedup)
    url: str
    author: str
    title: str
    body: str
    posted_at: int       # unix seconds (0 if unknown)
    engagement: int      # upvotes+comments / points / trade-size — source-dependent
    context_label: str   # 'r/CryptoCurrency', 'HN', 'Polymarket' — for UI display
