"""RSS 2.0 feed generator for /status/feed.xml.

Small, hand-rolled XML — no feedgen/PyRSS2Gen dependency. RSS 2.0 is
stable and the schema is 20 lines long; a dependency would be overkill.

We emit one `<item>` per incident update (not per incident). That way a
subscriber in a feed reader sees every admin-posted status change as its
own entry, which is what readers expect from a status page feed.
"""

from __future__ import annotations

import datetime as _dt
import html
from typing import Optional

from status_system import db as status_db


def _rfc822(ts: int) -> str:
    """Format epoch seconds as RFC-822 per the RSS 2.0 spec."""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )


def _esc(s: object) -> str:
    return html.escape(str(s or ""), quote=True)


def _item_xml(
    incident: dict, update: dict, base_url: str
) -> str:
    guid = f"{base_url}/status#incident-{incident['id']}-update-{update['id']}"
    link = f"{base_url}/status#incident-{incident['id']}"
    title = (
        f"[{update['status'].upper()}] {incident['title']}"
    )
    description = (
        f"<p><strong>{_esc(update['status'].title())}:</strong> "
        f"{_esc(update['message'])}</p>"
        f"<p>Affected: {_esc(', '.join(incident['affected_components']) or 'n/a')}. "
        f"Severity: {_esc(incident['severity'])}.</p>"
    )
    return (
        "    <item>\n"
        f"      <title>{_esc(title)}</title>\n"
        f"      <link>{_esc(link)}</link>\n"
        f"      <guid isPermaLink=\"false\">{_esc(guid)}</guid>\n"
        f"      <pubDate>{_rfc822(update['timestamp'])}</pubDate>\n"
        f"      <description>{_esc(description)}</description>\n"
        "    </item>"
    )


def build_rss_feed(
    base_url: str = "https://narve.ai",
    *,
    limit: int = 50,
    incidents: Optional[list[dict]] = None,
) -> str:
    """Build the full RSS 2.0 document.

    `incidents` can be passed explicitly (used in tests) — otherwise we
    pull the most recent N from the DB.
    """
    if incidents is None:
        incidents = status_db.list_recent_incidents(limit=limit)

    # Flatten into (incident, update) pairs, newest update first.
    pairs: list[tuple[dict, dict]] = []
    for inc in incidents:
        updates = status_db.list_incident_updates(inc["id"])
        for upd in updates:
            pairs.append((inc, upd))
    pairs.sort(key=lambda p: p[1]["timestamp"], reverse=True)
    pairs = pairs[:limit]

    items_xml = "\n".join(_item_xml(inc, upd, base_url) for inc, upd in pairs)
    last_build = _rfc822(pairs[0][1]["timestamp"]) if pairs else _rfc822(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>narve.ai — Status</title>\n"
        f"    <link>{_esc(base_url)}/status</link>\n"
        f"    <description>Service status and incident history for narve.ai</description>\n"
        f"    <language>en-us</language>\n"
        f"    <lastBuildDate>{last_build}</lastBuildDate>\n"
        f"    <generator>narve-gateway</generator>\n"
        f"{items_xml}\n"
        "  </channel>\n"
        "</rss>\n"
    )
