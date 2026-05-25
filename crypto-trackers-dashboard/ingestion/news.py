"""Multi-source crypto news RSS aggregator.

Pulls headlines from 7 reputable sources, dedupes by URL, sorts by
publication time, and tags each item with its source. RSS feeds are
parsed via ``defusedxml`` (no XXE).

Sources:
  - CoinDesk:          mainstream + macro
  - Decrypt:           web3 + culture
  - The Block:         institutional + research
  - Bitcoin Magazine:  BTC-focused
  - Bankless:          DeFi-focused
  - Cointelegraph:     mainstream
  - Crypto.news:       fast-moving headlines

All sources are free, no API key required.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from defusedxml import ElementTree as DET

from . import _cache
from ._http import get as http_get

log = logging.getLogger("ct.news")

SOURCES = [
    ("coindesk",          "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("decrypt",           "https://decrypt.co/feed"),
    ("the-block",         "https://www.theblock.co/rss.xml"),
    ("bitcoin-magazine",  "https://bitcoinmagazine.com/feed"),
    ("bankless",          "https://newsletter.banklesshq.com/feed"),
    ("cointelegraph",     "https://cointelegraph.com/rss"),
    ("crypto-news",       "https://crypto.news/feed/"),
]


def _parse_feed(source: str, url: str) -> list[dict]:
    r = http_get(url, timeout=12,
                 headers={"Accept": "application/rss+xml,application/xml,text/xml"})
    if not r:
        return []
    try:
        root = DET.fromstring(r.content)
    except DET.ParseError as e:  # type: ignore[attr-defined]
        log.warning("%s RSS parse error: %s", source, e)
        return []
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    rows: list[dict] = []
    for it in items[:25]:
        title = (it.findtext("title") or
                 it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link_el = it.find("link") or it.find("{http://www.w3.org/2005/Atom}link")
        if link_el is not None and not link_el.text:
            link = link_el.get("href")
        else:
            link = link_el.text if link_el is not None else None
        pub = (it.findtext("pubDate") or
               it.findtext("{http://www.w3.org/2005/Atom}updated") or
               it.findtext("{http://www.w3.org/2005/Atom}published") or "").strip()
        desc_raw = (it.findtext("description") or
                    it.findtext("{http://www.w3.org/2005/Atom}summary") or "")
        desc = re.sub(r"<[^>]+>", " ", desc_raw)
        desc = re.sub(r"\s+", " ", desc).strip()
        cat_els = it.findall("category")
        categories = [c.text for c in cat_els if c is not None and c.text]
        rows.append({
            "source": source,
            "title": title[:200],
            "url": link,
            "pub_date": pub,
            "summary": desc[:280],
            "categories": categories[:5],
        })
    return rows


_RFC822_RX = re.compile(r"(\w{3}), (\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _to_epoch(pub: str) -> Optional[float]:
    """Coarse RFC-822 (RSS) + ISO-8601 (Atom) timestamp parser."""
    if not pub:
        return None
    m = _RFC822_RX.search(pub)
    if m:
        try:
            _, d, mon, y, hh, mm, ss = m.groups()
            return datetime(int(y), _MONTHS.get(mon, 1), int(d),
                            int(hh), int(mm), int(ss), tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError):
            pass
    try:
        return datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def headlines(limit: int = 60) -> dict:
    cache_key = f"news_{limit}"
    hit = _cache.get(cache_key, ttl_s=600)
    if hit is not None:
        return hit
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(_parse_feed, src, url): src for src, url in SOURCES}
        for f in futures:
            try:
                rows.extend(f.result())
            except Exception as e:  # noqa: BLE001
                log.warning("feed %s failed: %s", futures[f], e)
    # Dedupe by URL
    seen: set = set()
    deduped: list[dict] = []
    for r in rows:
        key = r.get("url") or r.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        r["_epoch"] = _to_epoch(r.get("pub_date") or "") or 0
        deduped.append(r)
    deduped.sort(key=lambda r: r["_epoch"], reverse=True)
    deduped = deduped[:limit]
    by_source: dict[str, int] = {}
    for r in deduped:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    out = {
        "source": "RSS aggregator (7 outlets)",
        "count": len(deduped),
        "by_source": by_source,
        "headlines": deduped,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(headlines(15), indent=2)[:2500])
