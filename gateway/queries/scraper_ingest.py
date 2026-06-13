"""Persistence layer for scraper-ingested raw posts.

The scraper service (separate process) scrapes Twitter / TruthSocial and
pushes batches of ``RawPost`` dicts to ``POST /api/scraper/ingest`` on the
main server. This module turns those raw posts into rows the rest of the
app can use.

IMPORTANT — there is NO ``raw_posts`` / ``social_posts`` table in the main
database (auth.db). The only path from a scraped post to persisted data is
the extraction pipeline: ``pipeline.extract_step.process_post(post_dict)``,
which runs the Claude-backed (regex-fallback) extractor and writes any
detected predictions into the ``predictions`` table.

So "ingest" here means: validate each incoming post and hand it to the
extraction pipeline. We do NOT insert raw posts into ``predictions``
directly — a post is not a prediction. ``process_post`` is the component
that decides whether a post contains a prediction and persists it.

Two entrypoints:

* ``ingest_posts(posts)``      — synchronous; usable from sync callers but
                                 does NOT run extraction (returns counts of
                                 accepted vs. skipped posts only). Use when
                                 the caller will run extraction itself, or
                                 wants a fast accept without paying the
                                 (Claude API) extraction cost inline.
* ``ingest_and_extract(posts)`` — async; validates AND runs extraction per
                                 post via ``process_post``. This is what the
                                 ingest endpoint awaits.

Both return ``{"inserted": int, "skipped": int, ...}``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("queries.scraper_ingest")


def _normalize_post(raw: Any) -> Optional[dict]:
    """Coerce one incoming RawPost dict into the shape ``process_post``
    expects (``content``, ``author_handle``, ``post_id``).

    Returns ``None`` for anything unusable (not a dict, or empty content) —
    the caller counts those as skipped.

    Mirrors ``scraper/storage/models.py:RawPost.to_dict()`` field names:
    ``id``, ``platform``, ``author_handle``, ``content``, ``posted_at`` …
    """
    if not isinstance(raw, dict):
        return None

    content = str(raw.get("content") or "").strip()
    if not content:
        return None

    # RawPost.to_dict() always carries ``author_handle``; tolerate the
    # alternative ``source_handle`` key just in case a future payload uses it.
    author_handle = str(
        raw.get("author_handle") or raw.get("source_handle") or "unknown"
    ).strip() or "unknown"

    # RawPost id is the stable dedup key ("twitter:{id}" / "truthsocial:{id}").
    post_id = raw.get("id") or raw.get("post_id")

    return {
        "post_id": str(post_id) if post_id else None,
        "id": str(post_id) if post_id else None,
        "author_handle": author_handle,
        "content": content,
        "platform": raw.get("platform"),
        # source_url, if the scraper ever supplies one, flows through to
        # predictions.source_url via process_post.
        "source_url": raw.get("source_url"),
    }


def ingest_posts(posts: list[dict]) -> dict:
    """Validate a batch of raw posts WITHOUT running extraction.

    Returns ``{"inserted": <accepted>, "skipped": <rejected>}``.

    "inserted" counts posts that passed validation and are ready for the
    extraction pipeline. It does NOT mean predictions were written — that
    only happens in ``ingest_and_extract`` / ``process_post``. This sync
    variant exists so non-async callers (or callers that defer extraction
    to a background job) can accept a batch cheaply.
    """
    accepted = 0
    skipped = 0
    for raw in posts or []:
        if _normalize_post(raw) is None:
            skipped += 1
        else:
            accepted += 1
    return {"inserted": accepted, "skipped": skipped}


async def ingest_and_extract(posts: list[dict]) -> dict:
    """Validate AND extract a batch of raw posts.

    For each valid post, calls ``pipeline.extract_step.process_post`` which
    runs the extractor and persists any detected predictions into the
    ``predictions`` table.

    Returns::

        {
          "inserted": int,            # posts handed to the extractor
          "skipped": int,             # malformed / empty posts rejected
          "predictions_written": int, # rows added to predictions
          "errors": int,              # posts whose extraction raised
        }
    """
    # Imported lazily so importing this query module never pulls in the
    # whole pipeline (and its optional AI deps) at server import time.
    from pipeline.extract_step import process_post

    inserted = 0
    skipped = 0
    predictions_written = 0
    errors = 0

    for raw in posts or []:
        post = _normalize_post(raw)
        if post is None:
            skipped += 1
            continue
        try:
            result = await process_post(post)
            predictions_written += int(result.get("predictions_written", 0) or 0)
            inserted += 1
        except Exception:  # extraction must never crash the ingest batch
            log.exception(
                "extraction failed for post %s", post.get("post_id")
            )
            errors += 1

    return {
        "inserted": inserted,
        "skipped": skipped,
        "predictions_written": predictions_written,
        "errors": errors,
    }
