"""Perceptual-hash dedup for cross-platform meme tracking.

Two pieces:

  * `compute_missing_phashes(limit)` — async worker that fetches images for
    items in the cache that have no `phash` yet, computes a 64-bit dHash, and
    writes it back. Runs after each scraper sweep in scheduler.py.

  * `cluster_items(items, max_distance)` — given the items already loaded
    from the cache, group ones whose dHashes are within `max_distance`
    Hamming bits, keep the highest-scoring representative per cluster, and
    attach the others as `extra.dupes` so the UI can show "also seen on…".

The whole module degrades gracefully if Pillow / imagehash aren't
installed: the worker returns 0 and clustering is a no-op pass-through.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import cache
from scrapers._http import client

log = logging.getLogger(__name__)

try:
    import imagehash  # type: ignore
    from PIL import Image  # type: ignore
    _HASHING_AVAILABLE = True
except ImportError:
    _HASHING_AVAILABLE = False
    log.info("dedup: imagehash/Pillow not installed; dedup disabled")


# Hamming distance threshold for "same image". 64-bit hash with d ≤ 8 catches
# crops, recompression and small overlays without false-merging unrelated
# images. Tunable via the env CULTURE_PHASH_MAX_DISTANCE.
_DEFAULT_MAX_DISTANCE = 8


async def compute_missing_phashes(limit: int = 30) -> int:
    """Fetch + hash images for cached items missing a phash. Returns count."""
    if not _HASHING_AVAILABLE:
        return 0
    rows = cache.items_missing_phash(limit=limit)
    if not rows:
        return 0
    sem = asyncio.Semaphore(6)

    async def one(row: dict) -> int:
        async with sem:
            ph = await _hash_url(row["image"])
            if ph:
                cache.set_phash(row["source"], row["key"], ph)
                return 1
            # Mark as attempted with a sentinel so we don't re-try forever.
            cache.set_phash(row["source"], row["key"], "FAIL")
            return 0

    results = await asyncio.gather(*[one(r) for r in rows])
    n = sum(results)
    if n:
        log.info("dedup: hashed %d / %d images", n, len(rows))
    return n


async def _hash_url(url: str) -> str | None:
    try:
        async with client() as c:
            r = await c.get(url)
            if r.status_code != 200 or not r.content:
                return None
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            return str(imagehash.dhash(img, hash_size=8))
    except Exception as e:  # noqa: BLE001
        log.debug("phash fetch failed for %s: %s", url, e)
        return None


def cluster_items(
    items: list[dict[str, Any]], max_distance: int | None = None,
) -> list[dict[str, Any]]:
    """Greedy single-link clustering on phash.

    Items without a (real) phash pass through untouched. We keep the
    highest-scoring representative per cluster and attach a `dupes` list to
    its `extra` showing the other sources / urls.
    """
    if not _HASHING_AVAILABLE or not items:
        return items
    threshold = max_distance if max_distance is not None else _DEFAULT_MAX_DISTANCE

    # Partition: items with usable hash vs not.
    hashed: list[tuple[int, dict, "imagehash.ImageHash"]] = []
    passthrough: list[dict] = []
    for it in items:
        ph = it.get("phash")
        if not ph or ph == "FAIL":
            passthrough.append(it)
            continue
        try:
            h = imagehash.hex_to_hash(ph)
        except (ValueError, TypeError):
            passthrough.append(it)
            continue
        hashed.append((len(hashed), it, h))

    # Single-link clustering: walk items in score-desc order, attach each to
    # the first existing cluster within threshold, else open a new one.
    hashed.sort(key=lambda t: float(t[1].get("score") or 0), reverse=True)
    clusters: list[list[tuple[dict, "imagehash.ImageHash"]]] = []
    for _, item, h in hashed:
        placed = False
        for cluster in clusters:
            if (cluster[0][1] - h) <= threshold:    # imagehash __sub__ = Hamming
                cluster.append((item, h))
                placed = True
                break
        if not placed:
            clusters.append([(item, h)])

    deduped: list[dict] = []
    for cluster in clusters:
        rep = cluster[0][0]
        if len(cluster) > 1:
            extra = dict(rep.get("extra") or {})
            extra["dupes"] = [
                {"source": d.get("source"), "url": d.get("url"),
                 "title": d.get("title")}
                for d, _ in cluster[1:]
            ]
            rep = {**rep, "extra": extra}
        deduped.append(rep)

    deduped.extend(passthrough)
    deduped.sort(key=lambda i: float(i.get("score") or 0), reverse=True)
    return deduped
