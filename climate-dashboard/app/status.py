"""Self-health snapshot — which upstream fetchers are working, which aren't.

This is the dashboard's own observability surface. Hits every fetcher
(through the cache, so it's cheap on repeat) and reports a simple per-source
record with status, last-known data point, and the upstream URL.

Used to power /api/status and the /status static page so the operator can
tell at a glance which best-effort URLs are healthy without poking at
individual endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional


def _last_value_summary(payload: Optional[dict]) -> Optional[str]:
    """Pick a short human-readable summary of the most recent data point."""
    if not payload:
        return None
    latest = payload.get("latest")
    if latest:
        for key, unit in (("ppm", " ppm"), ("ppb", " ppb"), ("ppt", " ppt"),
                          ("extent_mkm2", " M km²"), ("sea_level_mm", " mm"),
                          ("oni", " ONI"), ("ohc_1e22_J", " ×10²² J"),
                          ("anomaly_c", " °C")):
            if key in latest:
                year = latest.get("year")
                month = latest.get("month")
                stamp = f"{year}-{month:02d}" if year and month else str(year or "")
                return f"{latest[key]}{unit}{(' (' + stamp + ')') if stamp else ''}"
    if payload.get("annual"):
        last = payload["annual"][-1]
        return f"{last['anomaly_c']} °C ({last['year']})"
    if payload.get("yearly"):
        last = payload["yearly"][-1]
        return f"{last['ohc_1e22_J']} ×10²² J ({last['year']})"
    return None


def _check(name: str, url: Optional[str], fetcher: Callable[[], Any]) -> dict:
    """Run one fetcher and produce a {name, status, url, summary, fetched_at}."""
    try:
        data = fetcher()
    except Exception as e:
        return {"name": name, "status": "error", "url": url,
                "summary": f"{type(e).__name__}: {e}", "fetched_at": None}
    if not data:
        return {"name": name, "status": "down", "url": url,
                "summary": "upstream returned no data — URL may be wrong or service down",
                "fetched_at": None}
    return {
        "name": name,
        "status": "ok",
        "url": url,
        "summary": _last_value_summary(data),
        "fetched_at": data.get("fetched_at"),
    }


def compute(*, fetchers: dict) -> dict:
    """Build a {sources: [...], counts: {...}} payload.

    ``fetchers`` is a dict of {label: (url, callable)} so we don't import the
    server's module graph here — that'd create a circular dep.
    """
    sources = [_check(name, url, fn) for name, (url, fn) in fetchers.items()]
    counts = {"ok": 0, "down": 0, "error": 0}
    for s in sources:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "sources": sources,
        "counts": counts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
