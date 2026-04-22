"""External forecast provider adapters.

One submodule per provider. Each exposes:

    async def fetch_matching(market: dict) -> list[Candidate]

``market`` is a snapshot row from ``market_snapshots`` (dict-shaped).
``Candidate`` is defined in ``base.py`` — a normalised "market on the
other platform we might map to ours" record with enough fields for
the matcher to make a call.

The providers never persist anything themselves — the sync job asks
them for candidates, asks the matcher to pick one, then writes the
mapping + forecast row via ``db_forecasts``. Keeping DB access out of
the fetchers means tests can swap in fakes trivially.
"""

from __future__ import annotations

from external_forecasts.base import Candidate, ProviderError, PROVIDERS  # noqa: F401
