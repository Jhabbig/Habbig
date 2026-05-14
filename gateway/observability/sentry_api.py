"""Fetch error summary from Sentry HTTP API for admin panel display.

Uses SENTRY_AUTH_TOKEN to query the Sentry REST API. Gracefully degrades
to an empty summary if the token is not configured or the API is down —
the admin panel still renders.

Wiring:
    * Frontend: ``gateway/static/admin.html`` System Health tab calls
      ``/admin/api/sentry`` on tab activation.
    * Backend route: registered in ``admin_routes.register`` so it is
      bundled with the rest of the admin API surface.
    * The route never echoes ``SENTRY_AUTH_TOKEN`` back to the client.

Caching: results are memoised for ``_CACHE_TTL_SECONDS`` (5 minutes) to
stay well inside Sentry's 40 req/sec org-wide rate limit. With 5 min TTL
the upstream call rate is bounded at 12 req/hour even if every admin
re-loads the System Health tab on a tight loop.

Tests mock ``httpx.AsyncClient.get`` so the suite never hits sentry.io.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("observability.sentry_api")


# ── 5-minute cache ──────────────────────────────────────────────────────
# Cache TTL doubles as the upstream rate limiter: at most one Sentry API
# call per (TTL) window across the whole gateway process. Admin pollers
# all hit the in-memory cache.
_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _empty_summary(dsn_enabled: bool, dashboard_url: str, error: str | None) -> dict[str, Any]:
    return {
        "enabled": dsn_enabled,
        "dashboard_url": dashboard_url,
        "count_24h": 0,
        "recent": [],
        "error": error,
        "cached_at": int(time.time()),
    }


def _cached_payload() -> dict[str, Any] | None:
    """Return the cached payload if still fresh, else None."""
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and now < _cache["expires_at"]:
            return _cache["payload"]
    return None


def _store_cache(payload: dict[str, Any]) -> None:
    with _cache_lock:
        _cache["payload"] = payload
        _cache["expires_at"] = time.monotonic() + _CACHE_TTL_SECONDS


def invalidate_cache() -> None:
    """Clear the cached summary. Used by tests + manual admin refresh."""
    with _cache_lock:
        _cache["payload"] = None
        _cache["expires_at"] = 0.0


async def fetch_sentry_summary(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return ``{enabled, dashboard_url, count_24h, recent: [...], error}``.

    * ``enabled``       — True if ``SENTRY_DSN`` is configured (drives the
                          "Active / Not configured" badge).
    * ``dashboard_url`` — ``SENTRY_DASHBOARD_URL`` or empty string.
    * ``count_24h``     — integer count of unresolved issues in last 24h.
                          Bounded by Sentry's page size when over the limit.
    * ``recent``        — list of up to 20 issues, each as
                          ``{title, count, last_seen, level, permalink}``.
                          ``permalink`` falls back to ``dashboard_url``.
    * ``error``         — None on success, short human-readable string on
                          partial failures (so the UI can show "Partial"
                          without hiding any data we did manage to get).

    Cached for 5 minutes. Set ``force_refresh=True`` to bypass the cache
    (used by the admin "refresh" button).

    Never raises — failures degrade to an empty summary with ``error`` set.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    dashboard_url = os.getenv("SENTRY_DASHBOARD_URL", "").strip()
    token = os.getenv("SENTRY_AUTH_TOKEN", "").strip()
    org = os.getenv("SENTRY_ORG", "").strip()
    project = os.getenv("SENTRY_PROJECT", "").strip()

    if not force_refresh:
        cached = _cached_payload()
        if cached is not None:
            return cached

    summary = _empty_summary(bool(dsn), dashboard_url, None)

    if not (token and org and project):
        summary["error"] = "SENTRY_AUTH_TOKEN / SENTRY_ORG / SENTRY_PROJECT not set"
        _store_cache(summary)
        return summary

    try:
        import httpx
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://sentry.io/api/0/projects/{org}/{project}/issues/"
        params = {
            "query": "is:unresolved age:-24h",
            "limit": 20,
            "statsPeriod": "24h",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            summary["error"] = f"Sentry API {resp.status_code}"
            _store_cache(summary)
            return summary
        issues = resp.json() or []
        if not isinstance(issues, list):
            summary["error"] = "Sentry API returned unexpected shape"
            _store_cache(summary)
            return summary

        summary["count_24h"] = len(issues)
        recent: list[dict[str, Any]] = []
        for issue in issues[:20]:
            if not isinstance(issue, dict):
                continue
            title = (issue.get("title") or issue.get("culprit") or "Unknown")
            permalink = issue.get("permalink") or dashboard_url or ""
            # Force https:// — Sentry permalinks are always https but we
            # guard against any future shape change leaking javascript:
            # URLs into the admin panel.
            if permalink and not permalink.startswith(("http://", "https://")):
                permalink = dashboard_url or ""
            count_raw = issue.get("count")
            try:
                count_val = int(count_raw) if count_raw is not None else 0
            except (TypeError, ValueError):
                count_val = 0
            recent.append({
                "title": str(title)[:200],
                "count": count_val,
                "last_seen": str(issue.get("lastSeen") or "")[:64],
                "level": str(issue.get("level") or "error")[:32],
                "permalink": permalink[:512],
            })
        summary["recent"] = recent
    except Exception as e:  # pragma: no cover — network failure path
        log.warning("Sentry API fetch failed: %s", e)
        summary["error"] = str(e)[:200]

    _store_cache(summary)
    return summary
