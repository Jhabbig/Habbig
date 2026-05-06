"""Unified action feed.

Pulls every regulator source in parallel-ish (sequentially, but each wrapped
in try/except) and merges into one list sorted by published-desc. Cache
defaults to 30 min — RSS feeds don't update faster, and tighter polling
hammers the source.
"""

from __future__ import annotations

import logging
import time
from threading import Lock

from . import esma_rss, fca_rss, sec_rss

log = logging.getLogger(__name__)

# Each entry is (module, source_code) — extending in later versions is just
# adding to this tuple after writing the corresponding `*_rss.py` (or
# scraper) module that exposes a `fetch()` returning the same dict shape.
_SOURCES = (
    sec_rss,
    fca_rss,
    esma_rss,
)

_CACHE_TTL = 30 * 60  # 30 min
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_all(max_per_source: int, since_days: int | None) -> dict:
    """Fetch every source, swallow failures per-source, return a status dict."""
    items: list[dict] = []
    sources_status: list[dict] = []
    for mod in _SOURCES:
        src = mod.SOURCE
        try:
            got = mod.fetch(max_items=max_per_source, since_days=since_days)
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
        except Exception as exc:  # belt-and-braces — fetch_source already swallows
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

    # Newest first — items missing a published date sink to the bottom.
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
