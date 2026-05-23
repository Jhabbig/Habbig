"""Unified action feed.

Iterates every source declared in `sources.SOURCES`, fetches via the
shared `_rss.fetch_source` (defusedxml + same RSS/Atom parser), and
merges into one list sorted by published-desc. Per-source try/except so
one bad URL never breaks the rest.

Cache defaults to 30 min — RSS feeds don't update faster, and tighter
polling hammers the source.
"""

from __future__ import annotations

import logging
import time
from threading import Lock

from analysis.classifier import classify_item

from ._rss import fetch_source
from . import jfsa_scraper
from .sources import SOURCES

log = logging.getLogger(__name__)

# Non-RSS sources — each module exposes a `SOURCE: RssSource` constant
# (for metadata + status) and a `fetch(max_items, since_days)` function
# returning the same item-dict shape `fetch_source` produces. Add new
# scraped sources by importing them here.
_SCRAPED_SOURCES = (jfsa_scraper,)

_CACHE_TTL = 30 * 60  # 30 min
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_all(max_per_source: int, since_days: int | None) -> dict:
    """Fetch every registered source, swallow failures per-source, return
    items + per-source status."""
    items: list[dict] = []
    sources_status: list[dict] = []
    for src in SOURCES:
        try:
            got = fetch_source(src, max_items=max_per_source, since_days=since_days)
            sources_status.append({
                "code": src.code,
                "name": src.name,
                "jurisdiction": src.jurisdiction,
                "rss_url": src.rss_url,
                "ok": True,
                "count": len(got),
                "error": None,
            })
            items.extend(got)
        except Exception as exc:
            log.warning("Source %s raised: %s", src.code, exc)
            sources_status.append({
                "code": src.code,
                "name": src.name,
                "jurisdiction": src.jurisdiction,
                "rss_url": src.rss_url,
                "ok": False,
                "count": 0,
                "error": str(exc),
            })

    # v2.2 — non-RSS scraped sources (JFSA, future HTML scrapers).
    for mod in _SCRAPED_SOURCES:
        src = mod.SOURCE
        try:
            got = mod.fetch(max_items=max_per_source, since_days=since_days)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": True, "count": len(got), "error": None,
            })
            items.extend(got)
        except Exception as exc:
            log.warning("Scraped source %s raised: %s", src.code, exc)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": False, "count": 0, "error": str(exc),
            })

    # Tag every item via the v0.1 classifier (in-place).
    for it in items:
        classify_item(it)

    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return {"items": items, "sources": sources_status}


def get_cached(force: bool = False, max_per_source: int = 50, since_days: int = 90) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]

    payload = _fetch_all(max_per_source=max_per_source, since_days=since_days)
    payload["fetched_at"] = now
    payload["since_days"] = since_days

    with _lock:
        _CACHE["data"] = payload
        _CACHE["fetched_at"] = now
    return payload


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    data = get_cached(force=True)
    print(f"items: {len(data['items'])}")
    for s in data["sources"]:
        print(f"  {s['code']:5s} {s['jurisdiction']:3s} ok={s['ok']}  count={s['count']}  err={s['error']}")
    print(json.dumps(data["items"][:3], indent=2))
