"""Shared types + conventions for external forecast providers.

The four provider modules each expose ``fetch_matching(market)``. They
return a list of these ``Candidate`` records — normalised so the
matcher (and the admin UI) only ever sees one shape regardless of
whether the source was a JSON API or a scraped Next.js page.

``probability`` is always the YES-side probability in [0, 1]. If the
provider is resolved (the real-world answer is known), probability is
still the last-known number — the sync job ignores already-resolved
rows before scoring so a resolved-YES market doesn't pin our chart at
1.0 forever.

Providers must catch their own HTTP / parsing errors and surface them
as ``ProviderError`` so the sync job can log + continue with the
next provider instead of crashing the batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Candidate:
    """One candidate market on an external platform.

    ``provider_market_id`` is whatever the platform uses as a stable
    identifier (integer PK on Manifold, slug on Metaculus). Admin links
    in /admin/equivalences use it to deep-link back to the source.

    ``close_at`` is a unix timestamp if known; helps the matcher weed
    out candidates with obviously-different close dates. None is fine.
    """
    provider: str
    provider_market_id: str
    question: str
    probability: float
    close_at: Optional[int] = None
    resolved: bool = False
    url: Optional[str] = None
    # Raw volume in provider-native units; the matcher uses presence
    # (not the value) as a "this market has eyes on it" tiebreaker.
    volume: Optional[float] = None


class ProviderError(Exception):
    """Raised by provider modules on any fetch/parse failure."""


# Advertised provider list — mirrors db_forecasts.SUPPORTED_PROVIDERS.
# Single source of truth so the sync job + admin UI can iterate.
PROVIDERS: tuple[str, ...] = (
    "metaculus",
    "manifold",
    "fivethirtyeight",
    "silver_bulletin",
)


# ── Tiny utility shared by all JSON providers ────────────────────────


def clamp_probability(raw) -> float:
    """Coerce an incoming probability to [0, 1]. Handles the common
    0..100 case (provider returns percentage) silently by dividing
    once. Anything still out of range raises ValueError so the sync
    job skips the row and surfaces it in logs."""
    try:
        p = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"probability not numeric: {raw!r}")
    if 1.0 < p <= 100.0:
        p = p / 100.0
    if p < 0.0 or p > 1.0:
        raise ValueError(f"probability out of range: {p}")
    return p
