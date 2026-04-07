"""
Background data poller for the gateway.

Periodically fetches each dashboard's main API endpoints, stores the
responses in Redis, and publishes data_updated events so SSE clients
refresh instantly.

This means:
  - First page load is always instant (served from cache)
  - Data is never more than one poll interval stale
  - SSE clients get pushed the moment fresh data lands
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from cache import cache, DEFAULT_TTLS

log = logging.getLogger("gateway.poller")

# ── Per-dashboard poll config ────────────────────────────────────────────────
# endpoint: the path to poll on the local backend
# interval: seconds between polls (should be < the cache TTL)
# The poller hits http://127.0.0.1:{port}{endpoint} for each entry.

POLL_TARGETS: dict[str, list[dict]] = {
    "sports": [
        {"endpoint": "/api/data", "interval": 25},
    ],
    "weather": [
        {"endpoint": "/api/markets", "interval": 55},
    ],
    "world": [
        {"endpoint": "/api/conflicts", "interval": 25},
        {"endpoint": "/api/news", "interval": 25},
        {"endpoint": "/api/polymarket", "interval": 25},
    ],
    "crypto": [
        {"endpoint": "/api/state", "interval": 12},
    ],
    "midterm": [
        {"endpoint": "/data/overview", "interval": 55},
        {"endpoint": "/data/races", "interval": 55},
    ],
    "top_traders": [
        {"endpoint": "/api/leaderboard?window=1d&limit=20", "interval": 12},
        {"endpoint": "/api/top-traders", "interval": 12},
    ],
}


class Poller:
    """Manages background polling tasks for all dashboards."""

    def __init__(self, dashboards: dict):
        """dashboards: gateway config.json['dashboards'] dict."""
        self._dashboards = dashboards
        self._tasks: list[asyncio.Task] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    async def start(self) -> None:
        """Launch one async task per (dashboard, endpoint) pair."""
        if not cache.available:
            log.warning("Poller not starting — Redis unavailable")
            return

        self._client = httpx.AsyncClient(timeout=15.0)
        self._running = True

        for dash_key, targets in POLL_TARGETS.items():
            cfg = self._dashboards.get(dash_key)
            if not cfg:
                continue
            port = cfg["target"]
            for target in targets:
                task = asyncio.create_task(
                    self._poll_loop(dash_key, port, target["endpoint"], target["interval"]),
                    name=f"poller:{dash_key}:{target['endpoint']}",
                )
                self._tasks.append(task)

        log.info(
            "Poller started: %d tasks across %d dashboards",
            len(self._tasks),
            len(set(t["endpoint"] for targets in POLL_TARGETS.values() for t in targets)),
        )

    async def stop(self) -> None:
        """Cancel all polling tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._client:
            await self._client.aclose()
            self._client = None
        log.info("Poller stopped")

    async def _poll_loop(self, dashboard: str, port: int, endpoint: str, interval: int) -> None:
        """Repeatedly fetch one endpoint and cache the result."""
        url = f"http://127.0.0.1:{port}{endpoint}"
        ttl = DEFAULT_TTLS.get(dashboard, 30)
        # Cache TTL should be longer than poll interval so there's always
        # a warm entry between polls.
        cache_ttl = max(ttl, interval + 10)

        while self._running:
            try:
                t0 = time.monotonic()
                resp = await self._client.get(url)
                elapsed = time.monotonic() - t0

                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "application/json")
                    cache.set_api(dashboard, endpoint.split("?")[0], resp.content, content_type, cache_ttl)
                    cache.publish(dashboard, "data_updated", {
                        "endpoint": endpoint,
                        "size": len(resp.content),
                        "elapsed_ms": round(elapsed * 1000),
                    })
                    log.debug("Polled %s → %d bytes in %dms", url, len(resp.content), elapsed * 1000)
                else:
                    log.warning("Poll %s returned %d", url, resp.status_code)

            except httpx.ConnectError:
                log.debug("Poll %s — backend offline", url)
            except Exception as e:
                log.warning("Poll %s error: %s", url, e)

            await asyncio.sleep(interval)

    def stats(self) -> dict:
        """Return poller status for the admin panel."""
        return {
            "running": self._running,
            "tasks": len(self._tasks),
            "targets": {
                k: [t["endpoint"] for t in v]
                for k, v in POLL_TARGETS.items()
            },
        }
