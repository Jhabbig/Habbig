"""Unified action feed.

Iterates every source declared in `sources.SOURCES` (RSS) and every module
in `_SCRAPED_SOURCES` (non-RSS, e.g. JFSA), fetches them all in parallel
via a ThreadPoolExecutor, classifies and dedupes the merged items, and
caches the result for 30 minutes.

v2.5 changes:
  - **Parallel fetch.** 55+ sources sequentially is ~minutes on a cold
    cache. Threadpool with workers=16 gets it to seconds. I/O bound, so
    threads work fine; no asyncio refactor needed.
  - **Cross-source dedup by `link`.** When SEC press releases and SEC-LIT
    both cover the same charge, the action shows twice in the merged
    feed. Drop the second occurrence; the first (in source-declaration
    order) wins. `deduped_count` exposed in the response.
  - **Stale-data flag per source.** A source returning `ok=True` but
    whose newest item is > 60 days old is probably silently broken
    (server returns a stale cached page, redirects to a holding page,
    etc.). Set `stale=True` so the UI can mark it yellow instead of
    green.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from analysis.classifier import classify_item

from ._rss import fetch_source
from . import jfsa_scraper
from .sources import SOURCES

log = logging.getLogger(__name__)

# Non-RSS sources — each module exposes a `SOURCE: RssSource` constant
# (for metadata + status) and a `fetch(max_items, since_days)` function
# returning the same item-dict shape `fetch_source` produces.
_SCRAPED_SOURCES = (jfsa_scraper,)

# Parallel-fetch concurrency. 16 is comfortably above source count after
# JFSA + future scrapers and well below file-descriptor / socket limits.
_FETCH_WORKERS = 16

# A source with no item newer than this is flagged `stale=true`, even if
# the HTTP fetch returned `ok=true`. 60 days is conservative — quarterly
# publishers stay green, but a silently-broken feed (returning a cached
# holding page) gets caught.
_STALE_AFTER_DAYS = 60

_CACHE_TTL = 30 * 60  # 30 min
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _rss_fetch_one(src, max_per_source: int, since_days: int | None) -> tuple:
    """ThreadPool worker for RSS sources. Returns (src, items, error).
    `error` is None on success; on failure, items is []."""
    try:
        got = fetch_source(src, max_items=max_per_source, since_days=since_days)
        return src, got, None
    except Exception as exc:
        log.warning("Source %s raised: %s", src.code, exc)
        return src, [], exc


def _scraped_fetch_one(mod, max_per_source: int, since_days: int | None) -> tuple:
    """ThreadPool worker for non-RSS modules. Same return shape as the
    RSS worker so the consumer can be uniform."""
    src = mod.SOURCE
    try:
        got = mod.fetch(max_items=max_per_source, since_days=since_days)
        return src, got, None
    except Exception as exc:
        log.warning("Scraped source %s raised: %s", src.code, exc)
        return src, [], exc


def _latest_published(items: list[dict]) -> str | None:
    """Return the most-recent ISO-8601 published timestamp among `items`,
    or None if none have a date."""
    best: str | None = None
    for it in items:
        pub = it.get("published")
        if pub and (best is None or pub > best):
            best = pub
    return best


def _is_stale(latest_published: str | None) -> bool:
    if not latest_published:
        return False  # absence of date isn't proof of staleness
    try:
        dt = datetime.fromisoformat(latest_published.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).days
    return age_days > _STALE_AFTER_DAYS


def _dedupe_by_link(items: list[dict]) -> tuple[list[dict], int]:
    """Drop subsequent occurrences of the same `link`. First wins so
    source-declaration order acts as a priority tiebreak (SEC press
    releases beat SEC-LIT on duplicates, etc.). Returns (deduped, count_dropped)."""
    seen: set[str] = set()
    out: list[dict] = []
    dropped = 0
    for it in items:
        link = (it.get("link") or "").strip()
        if link:
            if link in seen:
                dropped += 1
                continue
            seen.add(link)
        out.append(it)
    return out, dropped


def _fetch_all(max_per_source: int, since_days: int | None) -> dict:
    """Fetch every registered source in parallel, dedupe, classify, return
    items + per-source status + stale/dedup metadata."""
    # Submit all fetches in parallel. Two source kinds (RSS + scraped) but
    # the worker signature is the same so we can map them through one pool.
    results: list[tuple] = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        rss_futures = [ex.submit(_rss_fetch_one, s, max_per_source, since_days) for s in SOURCES]
        scraped_futures = [ex.submit(_scraped_fetch_one, m, max_per_source, since_days) for m in _SCRAPED_SOURCES]
        # Preserve declaration order in the response (rather than completion
        # order) so the per-source status row is stable across requests.
        for fut in rss_futures + scraped_futures:
            results.append(fut.result())

    items: list[dict] = []
    sources_status: list[dict] = []
    for src, got, exc in results:
        latest = _latest_published(got) if got else None
        status = {
            "code": src.code,
            "name": src.name,
            "jurisdiction": src.jurisdiction,
            "rss_url": src.rss_url,
            "ok": exc is None,
            "count": len(got),
            "error": str(exc) if exc else None,
            "latest_published": latest,
            "stale": _is_stale(latest) if exc is None else False,
        }
        sources_status.append(status)
        items.extend(got)

    # Cross-source dedupe by link. Source-declaration order in SOURCES
    # acts as the tiebreak — earlier sources win.
    items, deduped_count = _dedupe_by_link(items)

    # Tag every surviving item via the v0.1 classifier (in-place).
    for it in items:
        classify_item(it)

    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return {
        "items": items,
        "sources": sources_status,
        "deduped_count": deduped_count,
    }


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
    t0 = time.time()
    data = get_cached(force=True)
    elapsed = time.time() - t0
    print(f"items: {len(data['items'])}  deduped: {data['deduped_count']}  elapsed: {elapsed:.1f}s")
    for s in data["sources"]:
        flags = []
        if s["stale"]: flags.append("STALE")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        print(f"  {s['code']:12s} {s['jurisdiction']:4s} ok={s['ok']!s:5s} count={s['count']:3d}{flag_str}")
    if data["items"]:
        print(json.dumps(data["items"][:1], indent=2))
