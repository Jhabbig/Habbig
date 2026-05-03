"""
Cross-dashboard integration: fetch live data from sibling services.

Each sibling exposes a lightweight /api/share/* endpoint (localhost-only).
This module fetches from them in the background, caches results, and
provides a merged dict for inclusion in /api/all.

If a sibling is down, the cached (possibly stale) data is returned with
a "stale" flag.  If no cache exists, an empty dict is returned so that
the world-state dashboard never hard-fails on a sibling outage.
"""

import asyncio
import os
import time
from typing import Any

import httpx

# Host resolution mirrors the gateway: default 127.0.0.1 for the
# systemd-on-one-host deploy; docker-compose sets
# DASHBOARD_HOST_TEMPLATE="{key}" so each sibling resolves to its own
# compose service name.
_HOST_TEMPLATE: str = os.environ.get("DASHBOARD_HOST_TEMPLATE", "127.0.0.1")


def _sibling_host(key: str) -> str:
    try:
        return _HOST_TEMPLATE.format(key=key)
    except (KeyError, IndexError):
        return _HOST_TEMPLATE


# ── Configuration ────────────────────────────────────────────────────
SOURCES: dict[str, dict[str, Any]] = {
    "midterm_elections": {
        "url": f"http://{_sibling_host('midterm')}:8051/api/share/top-races",
        "ttl": 60,          # seconds
        "timeout": 5,       # request timeout
    },
    "crypto_signals": {
        "url": f"http://{_sibling_host('crypto')}:8000/api/share/snapshot",
        "ttl": 30,
        "timeout": 5,
    },
}

# ── Internal cache ───────────────────────────────────────────────────
_cache: dict[str, dict[str, Any]] = {}
_cache_ts: dict[str, float] = {}
_lock = asyncio.Lock()

# Shared client: HTTP/1.1 keep-alive avoids a fresh TCP handshake per fetch.
# Lazy-instantiated on first use so import order is irrelevant.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                )
    return _client


async def _fetch_one(key: str, cfg: dict) -> dict:
    """Fetch a single sibling endpoint.  Returns the JSON body or {}."""
    try:
        client = await _get_client()
        r = await client.get(cfg["url"], timeout=cfg.get("timeout", 5))
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        return {}


async def fetch_all() -> dict[str, Any]:
    """Fetch all sibling data in parallel and merge into one dict.

    Returns a dict like::

        {
            "midterm_elections": { ... },
            "crypto_signals":   { ... },
            "_cross_meta": {
                "midterm_elections": {"ok": True,  "age_s": 2},
                "crypto_signals":   {"ok": False, "age_s": 67, "stale": True},
            },
        }
    """
    now = time.time()
    tasks = {}

    for key, cfg in SOURCES.items():
        last_ts = _cache_ts.get(key, 0)
        if now - last_ts < cfg.get("ttl", 60):
            continue  # still fresh
        tasks[key] = cfg

    # Fetch stale/missing sources in parallel
    if tasks:
        results = await asyncio.gather(
            *[_fetch_one(k, c) for k, c in tasks.items()],
            return_exceptions=True,
        )
        async with _lock:
            for (key, _), result in zip(tasks.items(), results):
                if isinstance(result, dict) and result:
                    _cache[key] = result
                    _cache_ts[key] = now

    # Build output
    out: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for key in SOURCES:
        data = _cache.get(key, {})
        age = now - _cache_ts.get(key, 0)
        stale = age > SOURCES[key].get("ttl", 60) * 3
        out[key] = data
        meta[key] = {
            "ok": bool(data),
            "age_s": round(age, 1),
            "stale": stale,
        }
    out["_cross_meta"] = meta
    return out
