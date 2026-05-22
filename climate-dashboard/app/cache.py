"""TTL-keyed in-memory cache shared across fetchers.

All upstream sources are free public APIs; pulling NASA / NOAA CSVs every
minute would be wasteful and rude. Each source registers a TTL appropriate to
its update cadence (monthly CSVs cached 12h, daily 3-6h, market data 5min).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional

_cache: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()

_DEFAULT_TTL_S = 60 * 60  # 1h
_MAX_ENTRIES = 64

# Per-key TTL overrides. Keys here must match the cache_key passed by each
# fetcher's `fetch()`.
TTL: dict[str, int] = {
    "gistemp": 60 * 60 * 12,
    "co2": 60 * 60 * 12,
    "methane": 60 * 60 * 12,
    "n2o": 60 * 60 * 12,
    "sf6": 60 * 60 * 12,
    "owid_emissions": 60 * 60 * 24,  # country emissions update annually
    "ocean_heat": 60 * 60 * 24,      # NCEI publishes seasonal/yearly
    "snow_cover": 60 * 60 * 12,      # Rutgers updates monthly
    "sea_level": 60 * 60 * 12,       # NOAA STAR updates monthly-ish
    "sea_ice": 60 * 60 * 6,
    "sst": 60 * 60 * 3,
    "oni": 60 * 60 * 12,
    "polymarket": 60 * 5,
}


def get(key: str) -> Optional[Any]:
    with _lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = TTL.get(key, _DEFAULT_TTL_S)
        if time.time() - entry["t"] > ttl:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return entry["data"]


def set(key: str, data: Any) -> None:  # noqa: A001 - module-qualified at call sites
    with _lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def clear() -> None:
    """Test helper — drop everything."""
    with _lock:
        _cache.clear()
