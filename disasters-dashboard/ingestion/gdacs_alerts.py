"""GDACS (Global Disaster Alert and Coordination System) feed.

GDACS publishes a free RSS feed of every active major disaster globally
along with a colour-coded severity (RED = >1000 fatalities or major impact,
ORANGE = significant impact, GREEN = minor). The feed covers earthquakes,
tropical cyclones, floods, volcanoes, droughts, and wildfires - so it's the
single best "is anything serious happening right now?" signal.

Endpoint: https://www.gdacs.org/xml/rss.xml
No key, no auth, polite UA required.

We parse with ``defusedxml`` to avoid XXE.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from defusedxml import ElementTree as DET

from . import _cache
from ._http import get as http_get

log = logging.getLogger("disasters.gdacs")

GDACS_RSS_URL = "https://www.gdacs.org/xml/rss.xml"

# GDACS namespace map
NS = {
    "gdacs": "http://www.gdacs.org",
    "georss": "http://www.georss.org/georss",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "asgard": "http://asgard.jrc.it",
}

ALERT_RANK = {"Red": 0, "Orange": 1, "Green": 2}


def _parse_point(text: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None
    parts = text.strip().split()
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None, None


def _parse_event(item) -> dict:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    pub = (item.findtext("pubDate") or "").strip()
    desc = (item.findtext("description") or "").strip()
    # Strip HTML from description
    desc_plain = re.sub(r"<[^>]+>", " ", desc).strip()
    desc_plain = re.sub(r"\s+", " ", desc_plain)[:300]
    cat = (item.findtext("category") or "").strip()

    alert_lvl = item.findtext("gdacs:alertlevel", namespaces=NS) or ""
    event_type = item.findtext("gdacs:eventtype", namespaces=NS) or ""
    event_id = item.findtext("gdacs:eventid", namespaces=NS) or ""
    severity = item.findtext("gdacs:severity", namespaces=NS) or ""
    population = item.findtext("gdacs:population", namespaces=NS) or ""
    country = item.findtext("gdacs:country", namespaces=NS) or ""
    from_date = item.findtext("gdacs:fromdate", namespaces=NS) or ""

    point = item.findtext("georss:point", namespaces=NS)
    lat, lon = _parse_point(point)

    return {
        "title": title,
        "link": link,
        "category": cat,
        "alert_level": alert_lvl.title(),
        "event_type": event_type,
        "event_id": event_id,
        "severity": severity,
        "country": country,
        "population_exposed": population,
        "from_date": from_date,
        "pub_date": pub,
        "lat": lat,
        "lon": lon,
        "summary": desc_plain,
    }


def active_events(min_alert: str = "Orange") -> dict:
    """Return active GDACS events filtered to >= min_alert (Red < Orange < Green)."""
    cache_key = f"gdacs_{min_alert}"
    hit = _cache.get(cache_key, ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(GDACS_RSS_URL, timeout=20,
                 headers={"Accept": "application/rss+xml,text/xml,application/xml"})
    if not r:
        return {"error": "GDACS fetch failed", "events": [], "count": 0}
    try:
        root = DET.fromstring(r.content)
    except DET.ParseError as e:  # type: ignore[attr-defined]
        log.warning("GDACS XML parse error: %s", e)
        return {"error": "GDACS parse failed", "events": [], "count": 0}

    items = root.findall(".//item")
    events: list[dict] = []
    cap_rank = ALERT_RANK.get(min_alert.title(), 1)
    for it in items:
        e = _parse_event(it)
        rank = ALERT_RANK.get(e.get("alert_level") or "", 99)
        if rank > cap_rank:
            continue
        events.append(e)
    # Sort: Red first, then Orange, then by recency
    events.sort(key=lambda e: (ALERT_RANK.get(e.get("alert_level") or "", 99),
                                  e.get("from_date") or ""))
    by_type: dict[str, int] = {}
    by_alert: dict[str, int] = {}
    for e in events:
        by_type[e.get("event_type") or "?"] = by_type.get(e.get("event_type") or "?", 0) + 1
        by_alert[e.get("alert_level") or "?"] = by_alert.get(e.get("alert_level") or "?", 0) + 1
    out = {
        "source": "GDACS RSS (https://www.gdacs.org/xml/rss.xml)",
        "min_alert": min_alert,
        "count": len(events),
        "events": events[:60],
        "by_event_type": by_type,
        "by_alert_level": by_alert,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_events(min_alert="Orange"), indent=2)[:2000])
