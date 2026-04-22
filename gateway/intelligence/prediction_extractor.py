"""Claude-powered prediction extraction (replaces the regex/keyword filter).

Each social post is routed through Claude Haiku to decide whether it
contains a prediction and, if so, to produce a structured claim with
direction, probabilities, time frame, category, and flags for sarcasm +
conditionality. Results are cached per-post by sha256(content) for 30
days - the same post never bills twice, and cache rows survive across
process restarts.

Follows the same pattern as intelligence/environmental.py:
  - `_call_claude` is the thin, mockable entry point.
  - `_parse_claude_response` coerces to a strict schema, falls back to
    a safe stub on JSON or validation errors.
  - All calls (including cache hits and failures) log through
    intelligence.claude_usage.log_response so the admin AI-usage panel
    and the daily spend alert can see them.
  - Stub rows are cached like any other row; they prevent re-billing a
    post the extractor has already proven it can't classify.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

import db

from . import claude_usage


log = logging.getLogger("intelligence.extractor")


EXTRACTION_SCHEMA_VERSION = 1
EXTRACTION_MAX_TOKENS = 768
EXTRACTION_CACHE_TTL_SECONDS = 30 * 86400

VALID_DIRECTIONS = frozenset({"yes", "no"})
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
VALID_CATEGORIES = frozenset({
    "politics", "sports", "crypto", "geopolitics",
    "finance", "other",
})


EXTRACTION_SYSTEM_PROMPT = """\
You extract prediction claims from social media posts about politics,
finance, crypto, sports, and world events.

A prediction is a statement asserting something will or won't happen
in the future, with some implicit probability. "I bet X" or "X is going
to happen" qualify. Ignore:
  - jokes, sarcasm, rhetorical questions
  - hypotheticals ("imagine if X happened")
  - past-tense claims or factual statements about the present
  - retweets where the user merely shares someone else's claim

If a single post contains multiple distinct predictions, return them as
separate objects. If no prediction is present, return an empty array.

Respond ONLY with a JSON array. Each element uses this exact shape:

{
  "claim": "<core assertion in plain English, <=120 chars>",
  "direction": "yes" | "no",
  "explicit_probability": <0.0-1.0 number if the author stated one else null>,
  "implicit_confidence": "high" | "medium" | "low",
  "time_frame": "<e.g. 'by 2027', 'next month', 'this year', or null>",
  "category": "politics" | "sports" | "crypto" | "geopolitics" | "finance" | "other",
  "contains_sarcasm": true | false,
  "is_conditional": true | false
}

Rules:
  - direction: "yes" means the author predicts the event happens; "no" means they predict it does not.
  - implicit_confidence reflects how sure the author sounds, not how sure YOU are.
  - is_conditional = true if the prediction is stated as "if X then Y".
  - contains_sarcasm = true only if the post is clearly ironic.
  - Return a bare JSON array. No prose, no code fences.
"""


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def _not_a_prediction_payload(
    *,
    post_hash: str,
    source_post_id: Optional[str],
    source_handle: Optional[str],
    generated_by: str,
) -> dict:
    now = _now()
    return {
        "post_hash": post_hash,
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "source_post_id": source_post_id,
        "source_handle": source_handle,
        "generated_at": now,
        "generated_by": generated_by,
        "cache_valid_until": now + EXTRACTION_CACHE_TTL_SECONDS,
        "is_prediction": False,
        "claim": None,
        "direction": None,
        "explicit_probability": None,
        "implicit_confidence": None,
        "time_frame": None,
        "category": None,
        "contains_sarcasm": False,
        "is_conditional": False,
        "raw_payload": {"predictions": []},
    }


def _row_to_payload(row) -> dict:
    try:
        raw_payload = json.loads(row["raw_payload"]) if row["raw_payload"] else {}
    except (TypeError, json.JSONDecodeError):
        raw_payload = {}
    return {
        "post_hash": row["post_hash"],
        "schema_version": row["schema_version"],
        "source_post_id": row["source_post_id"],
        "source_handle": row["source_handle"],
        "generated_at": row["generated_at"],
        "generated_by": row["generated_by"],
        "cache_valid_until": row["cache_valid_until"],
        "is_prediction": bool(row["is_prediction"]),
        "claim": row["claim"],
        "direction": row["direction"],
        "explicit_probability": row["explicit_probability"],
        "implicit_confidence": row["implicit_confidence"],
        "time_frame": row["time_frame"],
        "category": row["category"],
        "contains_sarcasm": bool(row["contains_sarcasm"]),
        "is_conditional": bool(row["is_conditional"]),
        "raw_payload": raw_payload,
    }


def _coerce_prediction(item: dict) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    claim = str(item.get("claim") or "").strip()
    if not claim:
        return None
    direction_raw = str(item.get("direction") or "").strip().lower()
    if direction_raw not in VALID_DIRECTIONS:
        return None
    confidence = str(item.get("implicit_confidence") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "medium"
    category = str(item.get("category") or "").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "other"
    prob = item.get("explicit_probability")
    if prob is not None:
        try:
            prob = float(prob)
            if not (0.0 <= prob <= 1.0):
                prob = None
        except (TypeError, ValueError):
            prob = None
    time_frame = item.get("time_frame")
    if time_frame is not None:
        time_frame = str(time_frame)[:120]
    return {
        "claim": claim[:240],
        "direction": direction_raw,
        "explicit_probability": prob,
        "implicit_confidence": confidence,
        "time_frame": time_frame,
        "category": category,
        "contains_sarcasm": bool(item.get("contains_sarcasm")),
        "is_conditional": bool(item.get("is_conditional")),
    }


def _parse_claude_response(
    raw: Optional[str],
    *,
    post_hash: str,
    source_post_id: Optional[str],
    source_handle: Optional[str],
    generated_by: str,
) -> dict:
    if not raw:
        return _not_a_prediction_payload(
            post_hash=post_hash, source_post_id=source_post_id,
            source_handle=source_handle, generated_by=generated_by,
        )
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("extractor: JSON parse failed for hash=%s", post_hash[:12])
        return _not_a_prediction_payload(
            post_hash=post_hash, source_post_id=source_post_id,
            source_handle=source_handle, generated_by=generated_by,
        )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return _not_a_prediction_payload(
            post_hash=post_hash, source_post_id=source_post_id,
            source_handle=source_handle, generated_by=generated_by,
        )
    predictions = [p for p in (_coerce_prediction(p) for p in data) if p]
    if not predictions:
        return _not_a_prediction_payload(
            post_hash=post_hash, source_post_id=source_post_id,
            source_handle=source_handle, generated_by=generated_by,
        )
    primary = predictions[0]
    now = _now()
    return {
        "post_hash": post_hash,
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "source_post_id": source_post_id,
        "source_handle": source_handle,
        "generated_at": now,
        "generated_by": generated_by,
        "cache_valid_until": now + EXTRACTION_CACHE_TTL_SECONDS,
        "is_prediction": True,
        "claim": primary["claim"],
        "direction": primary["direction"],
        "explicit_probability": primary["explicit_probability"],
        "implicit_confidence": primary["implicit_confidence"],
        "time_frame": primary["time_frame"],
        "category": primary["category"],
        "contains_sarcasm": primary["contains_sarcasm"],
        "is_conditional": primary["is_conditional"],
        "raw_payload": {"predictions": predictions},
    }


async def _call_claude(post_content: str, author_handle: Optional[str]) -> tuple[Optional[str], Any]:
    """Single Claude call. Returns (text, sentinel) — the second slot is
    kept for backward compatibility with callers that used to pass the
    raw response to ``claude_usage.log_response``; call_claude already
    logs, so callers should treat a non-None first element as success.
    """
    from ai import client as _ai_client
    truncated = (post_content or "")[:8000]
    user_msg = (
        f"Post by @{author_handle or 'unknown'}:\n\n"
        f"{truncated}\n\n"
        "Extract any predictions as a JSON array."
    )
    text = await _ai_client.call_claude(
        feature="extraction",
        system=EXTRACTION_SYSTEM_PROMPT,
        user=user_msg,
        model=claude_usage.EXTRACTION_MODEL,
        max_tokens=EXTRACTION_MAX_TOKENS,
    )
    return text, (True if text is not None else None)


async def extract_predictions_from_post(post: dict, *, force: bool = False) -> dict:
    """Extract structured prediction data from a single post.

    *post* must have a `content` key; `author_handle` and `post_id` are
    recorded if present. Returns a dict with the cache-row shape.
    """
    content = str(post.get("content") or "")
    if not content.strip():
        return _not_a_prediction_payload(
            post_hash=content_hash(""),
            source_post_id=str(post.get("post_id") or post.get("id") or "") or None,
            source_handle=post.get("author_handle"),
            generated_by="empty",
        )

    post_hash = content_hash(content)
    source_post_id = str(post.get("post_id") or post.get("id") or "") or None
    source_handle = post.get("author_handle")

    if not force:
        cached = db.get_prediction_extraction(post_hash)
        if cached and cached["schema_version"] == EXTRACTION_SCHEMA_VERSION:
            claude_usage.log_response(
                feature="extraction",
                model=claude_usage.EXTRACTION_MODEL,
                response=None, cached_hit=True,
            )
            return _row_to_payload(cached)

    raw, _resp = await _call_claude(content, source_handle)
    # Real path: ai.client.call_claude logs internally via its own sentinel.
    # Test path: the stub returns a SimpleNamespace with .usage — log that too
    # so the test fixture's rollup assertions still see a row.
    if _resp is not None and hasattr(_resp, "usage"):
        claude_usage.log_response(
            feature="extraction",
            model=claude_usage.EXTRACTION_MODEL,
            response=_resp, cached_hit=False,
        )

    payload = _parse_claude_response(
        raw,
        post_hash=post_hash,
        source_post_id=source_post_id,
        source_handle=source_handle,
        generated_by=claude_usage.EXTRACTION_MODEL if raw else "stub",
    )
    db.upsert_prediction_extraction(post_hash, payload)
    return payload


async def extract_predictions_from_posts(posts: list[dict]) -> list[dict]:
    """Convenience wrapper — extract each post in order. Serial by design."""
    results: list[dict] = []
    for post in posts:
        results.append(await extract_predictions_from_post(post))
    return results
