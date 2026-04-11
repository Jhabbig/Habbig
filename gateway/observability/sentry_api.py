"""Fetch error summary from Sentry HTTP API for admin panel display.

Uses SENTRY_AUTH_TOKEN to query the Sentry REST API. Gracefully degrades
to an empty summary if the token is not configured or the API is down —
the admin panel still renders.
"""

from __future__ import annotations

import os
from typing import Any


async def fetch_sentry_summary() -> dict[str, Any]:
    """Return {enabled, dashboard_url, count_24h, recent: [...]}.

    `enabled` reflects whether SENTRY_DSN is configured at all.
    `recent` is at most 5 issue dicts with title + last_seen.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    token = os.getenv("SENTRY_AUTH_TOKEN", "").strip()
    dashboard_url = os.getenv("SENTRY_DASHBOARD_URL", "").strip()
    org = os.getenv("SENTRY_ORG", "").strip()
    project = os.getenv("SENTRY_PROJECT", "").strip()

    summary: dict[str, Any] = {
        "enabled": bool(dsn),
        "dashboard_url": dashboard_url,
        "count_24h": 0,
        "recent": [],
        "error": None,
    }
    if not (token and org and project):
        summary["error"] = "SENTRY_AUTH_TOKEN / SENTRY_ORG / SENTRY_PROJECT not set"
        return summary

    try:
        import httpx
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://sentry.io/api/0/projects/{org}/{project}/issues/"
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(url, headers=headers, params={"query": "is:unresolved age:-24h", "limit": 5})
            if resp.status_code != 200:
                summary["error"] = f"Sentry API {resp.status_code}"
                return summary
            issues = resp.json() or []
            summary["count_24h"] = len(issues)
            summary["recent"] = [
                {
                    "title": (issue.get("title") or issue.get("culprit") or "Unknown")[:200],
                    "last_seen": issue.get("lastSeen") or "",
                    "permalink": issue.get("permalink") or "",
                    "level": issue.get("level") or "error",
                }
                for issue in issues[:5]
            ]
    except Exception as e:
        summary["error"] = str(e)[:200]
    return summary
