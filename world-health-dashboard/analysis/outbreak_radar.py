"""Outbreak radar — combines feeds and shapes them for the frontend.

The frontend needs three views over the same WHO DON data:
  1. A flat chronological list (latest 100 outbreaks).
  2. A globe-pin layer: {iso3: {disease, count, last_published, severity}}.
  3. A disease-grouped view (e.g. how many active mpox vs marburg vs cholera).

We compute a crude "severity" score per country pin so the globe can vary the
pin radius / brightness — based on how many active DONs and how recently the
country was named. A higher score means more concerning per the available
signal (NOT case-counts; WHO DON is descriptive, not quantitative).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from ingestion import outbreak_feeds

log = logging.getLogger(__name__)


def _age_days(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 86400.0


def _severity(items: list[dict]) -> float:
    """0..1 score combining recency + count. 90-day half-life decay."""
    if not items:
        return 0.0
    score = 0.0
    for it in items:
        age = _age_days(it.get("published"))
        if age is None:
            score += 0.3
            continue
        # exponential decay with 90-day half-life
        score += 2.0 ** (-age / 90.0)
    # squash to 0..1 with a soft cap
    return min(1.0, score / 5.0)


def radar(limit: int = 100) -> dict:
    """Outbreak radar payload."""
    feed = outbreak_feeds.fetch_outbreaks()
    items = feed.get("items", [])

    by_country = outbreak_feeds.by_country(feed)
    by_disease = outbreak_feeds.by_disease(feed)

    # Globe pins: one per country with at least one DON.
    pins = {}
    for iso, country_items in by_country.items():
        country_items.sort(key=lambda x: x.get("published") or "", reverse=True)
        top = country_items[0]
        pins[iso] = {
            "iso3": iso,
            "country": top.get("country_name"),
            "count": len(country_items),
            "last_disease": top.get("disease"),
            "last_published": top.get("published"),
            "severity": round(_severity(country_items), 3),
            "diseases": list(dict.fromkeys(it["disease"] for it in country_items)),
        }

    # Disease summary.
    disease_summary = sorted(
        ({"disease": d, "count": len(its),
          "countries": len({i["country_iso3"] for i in its if i["country_iso3"]})}
         for d, its in by_disease.items()),
        key=lambda x: -x["count"],
    )

    # Scope counts.
    scope_counts = Counter(i.get("scope") or "unknown" for i in items)

    return {
        "items": items[:limit],
        "pins": pins,
        "disease_summary": disease_summary[:30],
        "scope_counts": dict(scope_counts),
        "total_items": len(items),
        "fetched_at": feed.get("fetched_at"),
        "stale": feed.get("stale", False),
    }
