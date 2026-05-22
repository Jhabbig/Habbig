"""Smithsonian GVP (Global Volcanism Program) weekly volcanic-activity bulletin.

The GVP curates the authoritative weekly report of all currently-active or
recently-active volcanoes worldwide. Free, no key. RSS endpoint:

    https://volcano.si.edu/news/WeeklyVolcanoRSS.xml

This is a *much* tighter source than EONET for volcanoes - GVP includes the
volcano number, location, alert/aviation colour, and a short summary of the
week's observed behaviour (eruption, seismicity, ash cloud, lava flow, etc.).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from defusedxml import ElementTree as DET

from . import _cache
from ._http import get as http_get

log = logging.getLogger("disasters.volcanoes")

GVP_RSS_URL = "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml"


def _parse_item(item) -> dict:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    pub = (item.findtext("pubDate") or "").strip()
    desc = (item.findtext("description") or "").strip()
    # Strip HTML and crush whitespace
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    # Title is "<Volcano Name>, <Country>"
    if "," in title:
        name, country = title.rsplit(",", 1)
        name, country = name.strip(), country.strip()
    else:
        name, country = title, ""
    return {
        "name": name,
        "country": country,
        "link": link,
        "pub_date": pub,
        "summary": desc[:600],
    }


def weekly_active(limit: int = 30) -> dict:
    cache_key = f"gvp_weekly_{limit}"
    hit = _cache.get(cache_key, ttl_s=12 * 3600)  # 12h - GVP updates weekly
    if hit is not None:
        return hit
    r = http_get(GVP_RSS_URL, timeout=20,
                 headers={"Accept": "application/rss+xml,text/xml,application/xml"})
    if not r:
        return {"error": "GVP fetch failed", "volcanoes": [], "count": 0}
    try:
        root = DET.fromstring(r.content)
    except DET.ParseError as e:  # type: ignore[attr-defined]
        log.warning("GVP XML parse error: %s", e)
        return {"error": "GVP parse failed", "volcanoes": [], "count": 0}
    items = root.findall(".//item")[:limit]
    out_rows = [_parse_item(it) for it in items]
    out = {
        "source": "Smithsonian GVP Weekly Volcanic Activity Report",
        "count": len(out_rows),
        "volcanoes": out_rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(weekly_active(), indent=2)[:1500])
