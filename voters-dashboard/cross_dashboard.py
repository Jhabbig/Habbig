"""
Cross-dashboard integration for the Voters Dashboard.

For a given country, fetches related entries from sibling dashboards:
  - midterm-dashboard:  any race in this country (currently US only)
  - polymarket markets via the polymarket bot's data feed (TBD slice 2)
  - commodity exposure: linkages into crypto-/stock-dashboard for any
    commodity tagged on the country.

All sibling fetches are best-effort with short timeouts. If a sibling is
down, an empty stub is returned so the country page never hard-fails.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

# ── Sibling endpoints ───────────────────────────────────────────────────
# Conservative: only call ones we know exist on the host. New siblings
# can be added by appending to SIBLING_SOURCES with their own fetch fn.
#
# Host resolution mirrors the gateway: default 127.0.0.1 for systemd-
# on-one-host; docker-compose sets DASHBOARD_HOST_TEMPLATE="{key}" so
# each sibling resolves to its own compose service name.

_HOST_TEMPLATE: str = os.environ.get("DASHBOARD_HOST_TEMPLATE", "127.0.0.1")


def _sibling_host(key: str) -> str:
    try:
        return _HOST_TEMPLATE.format(key=key)
    except (KeyError, IndexError):
        return _HOST_TEMPLATE


MIDTERM_TOP_RACES = f"http://{_sibling_host('midterm')}:8051/api/share/top-races"
WORLD_STATE_SHARE = f"http://{_sibling_host('world')}:7050/api/share/snapshot"
DEFAULT_TIMEOUT = 4.0

# Commodity → which sibling dashboard would have a linked instrument.
# Used to surface "see related markets" chips on the country drawer.
COMMODITY_SIBLINGS: dict[str, list[dict[str, str]]] = {
    "crude oil":          [{"dashboard": "stock", "symbol": "USO"}, {"dashboard": "stock", "symbol": "XLE"}],
    "natural gas":        [{"dashboard": "stock", "symbol": "UNG"}],
    "LNG":                [{"dashboard": "stock", "symbol": "LNG"}],
    "coal":               [{"dashboard": "stock", "symbol": "KOL"}],
    "iron ore":           [{"dashboard": "stock", "symbol": "VALE"}],
    "copper":             [{"dashboard": "stock", "symbol": "FCX"}],
    "lithium":            [{"dashboard": "stock", "symbol": "LIT"}],
    "gold":               [{"dashboard": "stock", "symbol": "GLD"}],
    "platinum":           [{"dashboard": "stock", "symbol": "PPLT"}],
    "rare earths":        [{"dashboard": "stock", "symbol": "MP"}],
    "nickel":             [{"dashboard": "stock", "symbol": "JJN"}],
    "soybeans":           [{"dashboard": "stock", "symbol": "SOYB"}],
    "corn":               [{"dashboard": "stock", "symbol": "CORN"}],
    "wheat":              [{"dashboard": "stock", "symbol": "WEAT"}],
    "cocoa":              [{"dashboard": "stock", "symbol": "NIB"}],
    "coffee":             [{"dashboard": "stock", "symbol": "JO"}],
    "semiconductors":     [{"dashboard": "stock", "symbol": "SMH"}],
    "vehicles":           [{"dashboard": "stock", "symbol": "CARZ"}],
}

# In-process cache so a flurry of country-detail requests doesn't spam siblings.
_cache: dict[str, dict[str, Any]] = {}
_cache_ts: dict[str, float] = {}
_CACHE_TTL = 60  # seconds


async def _fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


async def _midterm_for_country(iso: str) -> list[dict]:
    """Return midterm races filtered to this country. Currently US-only."""
    if iso != "USA":
        return []
    cached_key = "midterm"
    now = time.time()
    if cached_key in _cache and now - _cache_ts[cached_key] < _CACHE_TTL:
        data = _cache[cached_key]
    else:
        data = await _fetch_json(MIDTERM_TOP_RACES) or {}
        _cache[cached_key] = data
        _cache_ts[cached_key] = now
    races = data.get("races") or data.get("items") or []
    # Trim to a few headline rows for the country drawer.
    return races[:5]


def _commodity_links(commodities: list[str]) -> list[dict]:
    """For each commodity, return the best sibling-dashboard pointer."""
    out: list[dict] = []
    seen = set()
    for c in commodities or []:
        key = c.lower().strip()
        for entry in COMMODITY_SIBLINGS.get(key, []):
            sig = (entry["dashboard"], entry["symbol"])
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"commodity": c, **entry})
    return out


async def fetch_for_country(iso: str, name: str) -> dict[str, Any]:
    """Build the cross-dashboard block embedded in /api/country/{iso} responses."""
    midterm_task = _midterm_for_country(iso)
    # Future: polymarket task, weather task, etc. — gather them here.
    midterm_races = await asyncio.gather(midterm_task, return_exceptions=False)

    return {
        "midterm_races": midterm_races[0] if midterm_races and isinstance(midterm_races[0], list) else [],
        # Commodity links are filled in by the caller using the country's
        # own export/import lists (we don't re-load the YAML here to avoid
        # a circular import).
        "commodity_link_resolver": "use cross_dashboard.commodity_links(country['commodities_export'] + country['commodities_import'])",
    }


def commodity_links(commodities: list[str]) -> list[dict]:
    """Public helper for the server to call directly."""
    return _commodity_links(commodities)
