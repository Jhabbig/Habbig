"""Admin /admin/health-monitor — single-pane status for all 13 services.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``admin_jobs_routes``, ``status_routes``, etc.).

Routes exposed:
    GET  /admin/health-monitor        HTML page (admin shell)
    GET  /api/admin/health-monitor    JSON status snapshot

The HTML page polls the JSON endpoint every 10s. Both routes go through
``server._require_admin_user`` — non-admin callers get 403. The JSON
endpoint is cached server-side for 5s so 10s polling from multiple admin
browsers doesn't fan out into a thundering herd against the subproduct
backends.

Status semantics
    up    — 2xx response, latency <500ms
    slow  — 2xx response, latency >=500ms
    down  — timeout, connection refused, or 5xx response

Monochrome-safe: the UI labels every tile with the text "UP / SLOW / DOWN"
in addition to colour. Auto-refresh is plain ``fetch`` + ``setInterval``;
no websocket.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
from admin_shell import render_admin_page

log = logging.getLogger("admin_health_monitor")


# Service registry. Ports match the task spec. ``slug`` is the kebab-case
# key used by the logs filter and by tests asserting every service is
# reachable in the rendered HTML.
SERVICES: list[dict] = [
    {"name": "Gateway",        "slug": "gateway",       "port": 7000},
    {"name": "Sports",         "slug": "sports",        "port": 8888},
    {"name": "Weather",        "slug": "weather",       "port": 5050},
    {"name": "World",          "slug": "world",         "port": 7050},
    {"name": "Crypto",         "slug": "crypto",        "port": 8000},
    {"name": "Midterm",        "slug": "midterm",       "port": 8051},
    {"name": "Top Traders",    "slug": "top-traders",   "port": 8052},
    {"name": "Voters",         "slug": "voters",        "port": 7051},
    {"name": "Climate",        "slug": "climate",       "port": 7052},
    {"name": "Disasters",      "slug": "disasters",     "port": 7060},
    {"name": "Whale",          "slug": "whale",         "port": 8053},
    {"name": "Central Bank",   "slug": "central-bank",  "port": 7061},
    {"name": "World Health",   "slug": "world-health",  "port": 7053},
    {"name": "Love",           "slug": "love",          "port": 7062},
]


# 5s response cache + 24h uptime ring.
_CACHE_TTL_SECONDS = 5
_cache_lock = threading.Lock()
_cache: dict = {"expires_at": 0.0, "payload": None}

_RING_WINDOW = 86400  # 24h
_ring_lock = threading.Lock()
_ring: list[tuple[float, str, bool]] = []


def _record(slug: str, ok: bool, now: float) -> None:
    with _ring_lock:
        cutoff = now - _RING_WINDOW
        if _ring and _ring[0][0] < cutoff:
            _ring[:] = [r for r in _ring if r[0] >= cutoff]
        _ring.append((now, slug, ok))


def _uptime_24h(slug: str) -> Optional[float]:
    """Percentage of OK samples for ``slug`` in the last 24h, or None if
    we have no samples yet."""
    cutoff = time.time() - _RING_WINDOW
    with _ring_lock:
        rows = [r for r in _ring if r[1] == slug and r[0] >= cutoff]
    if not rows:
        return None
    ok = sum(1 for _, _, o in rows if o)
    return round(100.0 * ok / len(rows), 2)


def _probe(service: dict, client: httpx.Client) -> dict:
    """HEAD http://localhost:<port>/health with a 2s timeout.

    Returns the public-facing dict shape: ``{name, slug, port, status,
    latency_ms, last_check, uptime_24h}``.
    """
    port = service["port"]
    url = f"http://localhost:{port}/health"
    started = time.monotonic()
    status = "down"
    latency_ms: Optional[int] = None
    try:
        resp = client.head(url, timeout=2.0)
        latency_ms = int((time.monotonic() - started) * 1000)
        if 200 <= resp.status_code < 300:
            status = "slow" if latency_ms >= 500 else "up"
        else:
            status = "down"
    except httpx.TimeoutException:
        latency_ms = 2000
        status = "down"
    except Exception:
        latency_ms = int((time.monotonic() - started) * 1000)
        status = "down"

    now = time.time()
    _record(service["slug"], status != "down", now)

    return {
        "name": service["name"],
        "slug": service["slug"],
        "port": port,
        "status": status,
        "latency_ms": latency_ms,
        "last_check": int(now),
        "uptime_24h": _uptime_24h(service["slug"]),
    }


def _probe_all() -> dict:
    """Probe every service. Used by the JSON endpoint and tests."""
    out: list[dict] = []
    with httpx.Client() as client:
        for svc in SERVICES:
            try:
                out.append(_probe(svc, client))
            except Exception:  # pragma: no cover
                log.exception("health-monitor probe crashed for %s", svc.get("slug"))
                out.append({
                    "name": svc["name"],
                    "slug": svc["slug"],
                    "port": svc["port"],
                    "status": "down",
                    "latency_ms": None,
                    "last_check": int(time.time()),
                    "uptime_24h": _uptime_24h(svc["slug"]),
                })
    return {"services": out, "count": len(out), "generated_at": int(time.time())}


def _cached_snapshot() -> dict:
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and now < _cache["expires_at"]:
            return _cache["payload"]
    snapshot = _probe_all()
    with _cache_lock:
        _cache["payload"] = snapshot
        _cache["expires_at"] = time.monotonic() + _CACHE_TTL_SECONDS
    return snapshot


# JSON API

@server.app.get("/api/admin/health-monitor")
async def admin_health_monitor_api(request: Request) -> JSONResponse:
    """Return the cached snapshot. Admin-only."""
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover
        raise HTTPException(status_code=403, detail="Admin required")
    return JSONResponse(_cached_snapshot())


# HTML page

@server.app.get("/admin/health-monitor", response_class=HTMLResponse)
async def admin_health_monitor_page(request: Request):
    """Render the admin-shell page. Non-admin -> 403 page, 2FA -> redirect."""
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    return render_admin_page(
        request,
        "admin/health_monitor.html",
        page_title="Health monitor",
        active_route="health-monitor",
        breadcrumb=[("Admin", "/admin"), ("Health monitor", "/admin/health-monitor")],
    )
