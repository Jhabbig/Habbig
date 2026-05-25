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

from . import cache


def _last_value_summary(payload) -> Optional[str]:
    """Pick a short human-readable summary of the most recent data point.

    Most fetchers return dicts shaped like {latest: {...}, monthly: [...]};
    the markets fetchers (Polymarket, Kalshi) return plain lists. We accept
    either and surface a sensible one-line summary.
    """
    if not payload:
        return None
    if isinstance(payload, list):
        return f"{len(payload)} markets"
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


def _fmt_age(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds/60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds/3600:.0f}h ago"
    return f"{seconds/86400:.1f}d ago"


def _check(name: str, url: Optional[str], fetcher: Callable[[], Any],
           cache_key: Optional[str] = None) -> dict:
    """Run one fetcher and produce a status record."""
    try:
        data = fetcher()
    except Exception as e:
        return {"name": name, "status": "error", "url": url,
                "summary": f"{type(e).__name__}: {e}", "fetched_at": None,
                "cache_age": None, "cache_ttl_s": None}
    cache_age = cache.age_seconds(cache_key) if cache_key else None
    cache_ttl = cache.ttl_seconds(cache_key) if cache_key else None
    if not data:
        return {"name": name, "status": "down", "url": url,
                "summary": "upstream returned no data — URL may be wrong or service down",
                "fetched_at": None,
                "cache_age": _fmt_age(cache_age),
                "cache_ttl_s": cache_ttl}
    # Markets fetchers return lists; everything else returns a dict with a
    # fetched_at field. Handle both.
    fetched_at = data.get("fetched_at") if isinstance(data, dict) else None
    return {
        "name": name,
        "status": "ok",
        "url": url,
        "summary": _last_value_summary(data),
        "fetched_at": fetched_at,
        "cache_age": _fmt_age(cache_age),
        "cache_ttl_s": cache_ttl,
    }


def compute(*, fetchers: dict) -> dict:
    """Build a {sources: [...], counts: {...}} payload.

    ``fetchers`` is a dict of {label: (url, callable, cache_key)} — cache_key
    is optional and lets the status page show how stale each cached value
    is. Backwards-compatible with the older (url, callable) 2-tuple form.
    """
    sources = []
    for name, val in fetchers.items():
        if len(val) == 3:
            url, fn, cache_key = val
        else:
            url, fn = val
            cache_key = None
        sources.append(_check(name, url, fn, cache_key))
    counts = {"ok": 0, "down": 0, "error": 0}
    for s in sources:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "sources": sources,
        "counts": counts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
