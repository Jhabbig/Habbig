"""RSS 2.0 alert feed renderer — v1.5.

Same filter semantics as `/api/feed` (jurisdiction, source, tag, severity,
topic, q, has_market), but the output is a valid RSS 2.0 XML document.
Subscribe by URL — feed readers handle delivery.

This is v1.5 scoped tight: an alert *channel* (RSS), not a subscription
system. Each subscriber crafts a URL like:

    /feed.xml?tag=enforcement&jurisdiction=US&severity=high,severe

and plugs it into their reader. Email digest with managed subscribers
+ unsubscribe tokens + bounce handling is the genuine v1.6 lift; RSS
delivers ~80% of the value for ~5% of the build cost (mirroring the
"Trade Poly / Trade Kalshi deep-link" call in v0.5).

Output is hand-rendered XML rather than using a library — RSS 2.0 is
~12 elements per item and zero deps wins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Iterable


def _xml_escape(s: str | None) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _rfc822(iso_dt: str | None) -> str:
    if not iso_dt:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
    except ValueError:
        return format_datetime(datetime.now(timezone.utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def render(items: Iterable[dict], *,
           channel_title: str = "Regulators Dashboard",
           channel_description: str = "Filtered regulator action feed",
           channel_link: str = "",
           self_url: str = "",
           limit: int = 50) -> str:
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        '<channel>',
        f'<title>{_xml_escape(channel_title)}</title>',
        f'<description>{_xml_escape(channel_description)}</description>',
        f'<link>{_xml_escape(channel_link or "")}</link>',
        f'<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>',
        '<generator>regulators-dashboard/1.5</generator>',
    ]
    if self_url:
        parts.append(f'<atom:link href="{_xml_escape(self_url)}" rel="self" type="application/rss+xml" />')

    for it in list(items)[:limit]:
        title = it.get("title") or "(untitled)"
        # Description: summary + classifier metadata so the feed reader
        # shows the same context the dashboard surface does.
        desc_lines: list[str] = []
        if it.get("summary"):
            desc_lines.append(it["summary"])
        meta_bits: list[str] = []
        if it.get("source"):
            meta_bits.append(f"Source: {it['source']}")
        if it.get("primary_tag"):
            meta_bits.append(f"Type: {it['primary_tag']}")
        sev = it.get("severity")
        if sev:
            ccy = sev.get("currency", "")
            amt = sev.get("amount_native")
            bucket = sev.get("bucket", "")
            if amt is not None:
                meta_bits.append(f"Severity: {bucket} ({amt:,.0f} {ccy})")
            else:
                meta_bits.append(f"Severity: {bucket}")
        if it.get("topics"):
            meta_bits.append(f"Topics: {', '.join(it['topics'])}")
        if meta_bits:
            desc_lines.append(" · ".join(meta_bits))
        description = "\n\n".join(desc_lines)

        link = it.get("link") or ""
        # GUID is the link by default; falls back to title hash for items
        # without links so feed readers can dedupe.
        guid = it.get("id") or it.get("link") or it.get("title", "")
        pub_date = _rfc822(it.get("published"))

        parts.append('<item>')
        parts.append(f'<title>{_xml_escape(title)}</title>')
        if link:
            parts.append(f'<link>{_xml_escape(link)}</link>')
        parts.append(f'<guid isPermaLink="false">{_xml_escape(guid)}</guid>')
        parts.append(f'<pubDate>{pub_date}</pubDate>')
        if it.get("source"):
            parts.append(f'<category>{_xml_escape(it["source"])}</category>')
        if it.get("primary_tag"):
            parts.append(f'<category>{_xml_escape(it["primary_tag"])}</category>')
        for t in it.get("topics", []):
            parts.append(f'<category>{_xml_escape(t)}</category>')
        if description:
            parts.append(f'<description>{_xml_escape(description)}</description>')
        parts.append('</item>')

    parts.append('</channel></rss>')
    return "\n".join(parts)


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    items = [
        {
            "id": "SEC::https://sec.gov/news/x",
            "title": "SEC charges firm with fraud",
            "link": "https://sec.gov/news/x",
            "summary": "Today announced a $200 million civil penalty for misrepresentations.",
            "published": "2026-05-15T13:00:00+00:00",
            "source": "SEC",
            "primary_tag": "enforcement",
            "severity": {"bucket": "severe", "amount_native": 200_000_000, "currency": "USD"},
            "topics": ["aml", "crypto"],
        },
        {
            "title": "Speech by FCA CEO with <ampersand & quoted \"thing\">",
            "link": "https://fca.org.uk/x",
            "summary": "Notes on the Consumer Duty.",
            "published": "2026-05-10T10:00:00+00:00",
            "source": "FCA",
            "primary_tag": "speech",
            "topics": [],
        },
    ]
    xml = render(items, channel_link="https://regulators.example.com",
                 self_url="https://regulators.example.com/feed.xml?tag=enforcement")
    # Parse it back to verify it's valid XML
    from defusedxml.ElementTree import fromstring
    root = fromstring(xml.encode("utf-8"))
    channel = root.find("channel")
    assert channel is not None
    items_in_xml = channel.findall("item")
    assert len(items_in_xml) == 2
    # Verify escaping survived parsing
    second_title = items_in_xml[1].findtext("title")
    assert "ampersand" in second_title and "&" in second_title
    # Verify metadata baked into description
    first_desc = items_in_xml[0].findtext("description")
    assert "severe" in first_desc
    assert "aml" in first_desc
    print("smoke OK")
    print()
    print(xml[:1200])
