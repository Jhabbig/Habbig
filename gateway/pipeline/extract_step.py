"""Pipeline extract step.

One entrypoint: ``process_post(post)`` runs the Claude-backed extractor
first, falls back to the existing regex/keyword extractor on Claude
failure, and writes any detected predictions into the ``predictions``
table through a module-local sqlite3 connection.

No imports from db.py — the pipeline is testable in isolation.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional


log = logging.getLogger("pipeline.extract_step")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


async def _extract_ai(post_text: str, post_id: Optional[str]) -> list[dict]:
    from ai.extractor import extract_predictions_from_post
    return await extract_predictions_from_post(post_text, post_id=post_id)


def _extract_regex_fallback(post_text: str) -> list[dict]:
    """Tolerant fallback when Claude is unavailable.

    Imports the legacy extractor lazily. If it's missing or errors, we
    return [] — a missed prediction is better than a broken pipeline.
    """
    try:
        from intelligence import prediction_extractor as legacy  # type: ignore
    except ImportError:
        return []
    # Legacy extractor signatures have varied across branches. Probe.
    if hasattr(legacy, "extract_predictions_from_text"):
        try:
            return list(legacy.extract_predictions_from_text(post_text) or [])  # type: ignore
        except Exception:
            return []
    if hasattr(legacy, "extract"):
        try:
            return list(legacy.extract(post_text) or [])  # type: ignore
        except Exception:
            return []
    return []


def _insert_predictions(
    source_handle: str,
    post_id: Optional[str],
    predictions: Iterable[dict],
) -> int:
    written = 0
    conn = _connect()
    try:
        now = int(time.time())
        for pred in predictions:
            if not pred:
                continue
            content = pred.get("claim") or pred.get("content") or ""
            if not content.strip():
                continue
            category = pred.get("category") or "other"
            direction = (pred.get("direction") or "").upper() or None
            prob = pred.get("explicit_probability") or pred.get("predicted_probability")
            conn.execute(
                "INSERT INTO predictions "
                "(source_handle, market_id, category, direction, "
                " predicted_probability, content, source_url, extracted_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    source_handle,
                    pred.get("market_slug"),
                    category,
                    direction,
                    prob,
                    content[:2000],
                    pred.get("source_url") or post_id,
                    now,
                ),
            )
            written += 1
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("insert predictions failed: %s", exc)
    finally:
        conn.close()
    return written


async def process_post(post: dict) -> dict:
    """Extract predictions from a single post and persist them.

    ``post`` is a dict with at least ``content`` and ``author_handle``.
    ``post_id`` / ``source_url`` are optional.
    """
    content = str(post.get("content") or "")
    if not content.strip():
        return {"post_id": post.get("post_id"), "predictions_written": 0, "fallback": False}
    source_handle = str(post.get("author_handle") or "unknown")
    post_id = str(post.get("post_id") or post.get("id") or "") or None

    try:
        predictions = await _extract_ai(content, post_id)
        used_fallback = False
    except Exception as exc:
        log.warning("ai extractor failed for post %s: %s", post_id, exc)
        predictions = []
        used_fallback = True

    if not predictions and not used_fallback:
        # Claude returned an empty list — respect it (post had no
        # prediction) rather than falling back.
        return {"post_id": post_id, "predictions_written": 0, "fallback": False}

    if not predictions and used_fallback:
        predictions = _extract_regex_fallback(content)

    written = _insert_predictions(source_handle, post_id, predictions)
    return {
        "post_id": post_id,
        "predictions_written": written,
        "fallback": used_fallback,
    }


async def process_posts_batch(posts: list[dict]) -> dict:
    total_written = 0
    fallback_count = 0
    for post in posts or []:
        r = await process_post(post)
        total_written += r.get("predictions_written", 0)
        if r.get("fallback"):
            fallback_count += 1
    return {
        "posts_processed": len(posts or []),
        "predictions_written": total_written,
        "fallback_count": fallback_count,
    }
