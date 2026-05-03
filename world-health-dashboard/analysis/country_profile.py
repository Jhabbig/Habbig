"""Per-country composite profile.

Pulls every catalog metric for a single country. Used by the country drill-down
side panel — the user clicks a country on the globe, and we return all metrics
for that country in one shot. Each metric is fetched from its source (WHO GHO
or World Bank) using the existing disk cache, so a fully-warm cache means this
is essentially a fan-in over JSON files.

We also expose `globe_layer(metric_id)` which returns the {iso3 → latest_value}
dict that the frontend uses to color the globe.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ingestion import metrics_catalog, who_gho, world_bank
from ingestion.country_codes import name_of, region_of

log = logging.getLogger(__name__)


def _fetch_for(metric: metrics_catalog.Metric) -> dict:
    if metric.source == "who_gho":
        return who_gho.fetch_indicator(metric.source_code)
    if metric.source == "world_bank":
        return world_bank.fetch_indicator(metric.source_code)
    log.warning("Unknown source for metric %s: %s", metric.id, metric.source)
    return {"by_country": {}, "latest": {}}


def globe_layer(metric_id: str) -> dict:
    """Return {iso3: latest_value} + min/max/quantiles for color scaling."""
    m = metrics_catalog.get(metric_id)
    if not m:
        return {"error": f"unknown metric_id: {metric_id}"}
    data = _fetch_for(m)
    latest = data.get("latest", {})
    values = [v["value"] for v in latest.values() if v and v.get("value") is not None]
    values.sort()
    n = len(values)
    if n == 0:
        return {
            "metric": m.to_dict(), "by_iso3": {},
            "min": None, "max": None, "p10": None, "p50": None, "p90": None,
            "fetched_at": data.get("fetched_at"),
            "stale": data.get("stale", False),
        }

    def _q(p: float) -> float:
        # Linear-interpolated percentile, no numpy.
        if n == 1:
            return values[0]
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return values[lo] * (1 - frac) + values[hi] * frac

    by_iso3 = {iso: {"value": v["value"], "year": v["year"]} for iso, v in latest.items()}
    return {
        "metric": m.to_dict(),
        "by_iso3": by_iso3,
        "min": values[0],
        "max": values[-1],
        "p10": _q(0.10),
        "p50": _q(0.50),
        "p90": _q(0.90),
        "n": n,
        "fetched_at": data.get("fetched_at"),
        "stale": data.get("stale", False),
    }


def history(metric_id: str, iso3: str) -> dict:
    """Return time series for a single metric in a single country."""
    m = metrics_catalog.get(metric_id)
    if not m:
        return {"error": f"unknown metric_id: {metric_id}"}
    iso = iso3.upper()
    data = _fetch_for(m)
    series = data.get("by_country", {}).get(iso, [])
    return {
        "metric": m.to_dict(),
        "iso3": iso,
        "country": name_of(iso),
        "points": series,
        "fetched_at": data.get("fetched_at"),
    }


def country_profile(iso3: str) -> dict:
    """Latest value of every catalog metric for a single country.

    Fetches all 60-odd metrics concurrently; on a cold cache this is the
    difference between ~2 min (sequential) and ~15 s (parallel). Once cached,
    the disk-read path is essentially instant either way.
    """
    iso = iso3.upper()
    name = name_of(iso)
    if not name:
        return {"error": f"unknown iso3: {iso3}"}

    def _one(m: metrics_catalog.Metric):
        try:
            data = _fetch_for(m)
        except Exception as exc:
            log.warning("Profile fetch failed for %s/%s: %s", iso, m.id, exc)
            return m.id, {"value": None, "year": None}
        latest = data.get("latest", {}).get(iso)
        if latest is None:
            return m.id, {"value": None, "year": None}
        return m.id, {"value": latest["value"], "year": latest["year"]}

    metrics: dict = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for mid, payload in pool.map(_one, metrics_catalog.CATALOG):
            metrics[mid] = payload

    return {
        "iso3": iso,
        "name": name,
        "region": region_of(iso),
        "metrics": metrics,
    }


def country_compare(iso_a: str, iso_b: str) -> dict:
    a = country_profile(iso_a)
    b = country_profile(iso_b)
    return {"a": a, "b": b}
