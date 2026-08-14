"""Tsunami warning feeds: PTWC (Pacific) + NTWC (US/Atlantic + Caribbean).

Both publish a single live ATOM/RSS feed with currently-active warnings
or messages. Free, no key. Parsed via ``defusedxml``.

  * PTWC: https://www.tsunami.gov/events/xml/PAAQAtom.xml (Pacific, Indian
    Ocean, Caribbean and Atlantic via NTWC ingest)
  * NTWC: https://www.tsunami.gov/events/xml/PAAQAtom.xml is the unified feed

We always treat "no warnings" as the default state. When a warning is
present we surface its severity (Information / Watch / Advisory / Warning),
the originating event (often an earthquake link), and the affected coastal
region.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from defusedxml import ElementTree as DET

from . import _cache
from ._http import get as http_get

log = logging.getLogger("disasters.tsunami")

# NOAA's unified Atom feed for all NTWC + PTWC products
TSUNAMI_FEED_URL = "https://www.tsunami.gov/events/xml/PAAQAtom.xml"


def _parse_entry(entry, ns: dict) -> dict:
    title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
    summary = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
    summary = re.sub(r"\s+", " ", summary)[:500]
    updated = (entry.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
    link_el = entry.find("{http://www.w3.org/2005/Atom}link")
    link = link_el.get("href") if link_el is not None else None

    # Title-encoded severity: "Tsunami Warning -..", "Tsunami Watch -..", etc.
    severity = "Information"
    tl = title.lower()
    if "tsunami warning" in tl: severity = "Warning"
    elif "tsunami advisory" in tl: severity = "Advisory"
    elif "tsunami watch" in tl: severity = "Watch"
    return {
        "title": title,
        "severity": severity,
        "updated": updated,
        "summary": summary,
        "link": link,
    }


def active_warnings() -> dict:
    hit = _cache.get("tsunami_active", ttl_s=300)  # 5 min - tsunami warnings move fast
    if hit is not None:
        return hit
    r = http_get(TSUNAMI_FEED_URL, timeout=15,
                 headers={"Accept": "application/atom+xml,application/xml,text/xml"})
    if not r:
        return {"error": "Tsunami feed fetch failed", "entries": [], "count": 0}
    try:
        root = DET.fromstring(r.content)
    except DET.ParseError as e:  # type: ignore[attr-defined]
        log.warning("Tsunami feed parse error: %s", e)
        return {"error": "Tsunami feed parse failed", "entries": [], "count": 0}
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    rows = [_parse_entry(e, ns) for e in entries[:25]]
    by_severity: dict[str, int] = {}
    for r_ in rows:
        by_severity[r_.get("severity") or "?"] = by_severity.get(r_.get("severity") or "?", 0) + 1
    out = {
        "source": "NOAA tsunami.gov PAAQAtom unified feed",
        "count": len(rows),
        "entries": rows,
        "by_severity": by_severity,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("tsunami_active", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_warnings(), indent=2))
