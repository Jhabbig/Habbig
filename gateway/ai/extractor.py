"""Claude Haiku prediction extraction.

Replaces regex/keyword detection in the scraper pipeline. For each post we:

  1. sha256 the text — that's the cache key.
  2. Check ``ai_cache``. On hit, log a cache-hit row and return.
  3. Call Claude Haiku with the strict-JSON extraction prompt.
  4. Parse + validate. Cache the result (including empty arrays) for 30 d.

The function is intentionally side-effect-light: the ONLY writes are to
``ai_cache`` (via ai.cache) and ``claude_usage_log`` (via ai.client).
The caller is responsible for inserting the returned predictions into
``predictions`` / related tables.

Failure policy: on Claude error we return an empty list and cache it
briefly (5 min) so we don't hammer. The fallback regex extractor is in
``intelligence/prediction_extractor.py``; pipeline wiring is expected to
try this module first and fall back there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Optional

from ai import cache, client


log = logging.getLogger("ai.extractor")


EXTRACTION_TTL_SECONDS = 30 * 86400
FAILURE_TTL_SECONDS = 300
EXTRACTION_MAX_TOKENS = 900

VALID_DIRECTIONS = frozenset({"yes", "no"})
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
VALID_CATEGORIES = frozenset({
    "politics", "sports", "crypto", "geopolitics",
    "finance", "other",
})


EXTRACTION_SYSTEM_PROMPT = """\
You extract prediction claims from social-media posts about politics,
finance, crypto, sports and world events.

A prediction is a statement asserting something will or won't happen in
the future with some implicit probability. Ignore: jokes, sarcasm,
rhetorical questions, hypotheticals, past-tense claims, retweets.

If a post contains multiple distinct predictions, return each separately.
If none, return an empty array.

Return ONLY a JSON array. Each element:

{
  "claim": "<core assertion, <=120 chars>",
  "direction": "yes" | "no",
  "explicit_probability": <0.0-1.0 if stated, else null>,
  "implicit_confidence": "high" | "medium" | "low",
  "time_frame": "<e.g. 'by 2027', or null>",
  "category": "politics" | "sports" | "crypto" | "geopolitics" | "finance" | "other",
  "contains_sarcasm": true | false,
  "is_conditional": true | false
}

No prose. No code fences.
"""


def content_hash(post_text: str) -> str:
    return hashlib.sha256((post_text or "").encode("utf-8")).hexdigest()


def _cache_key(post_text: str) -> str:
    return f"extract:{content_hash(post_text)}"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _coerce_prediction(item: dict) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    claim = str(item.get("claim") or "").strip()
    if not claim:
        return None
    direction = str(item.get("direction") or "").strip().lower()
    if direction not in VALID_DIRECTIONS:
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
        "direction": direction,
        "explicit_probability": prob,
        "implicit_confidence": confidence,
        "time_frame": time_frame,
        "category": category,
        "contains_sarcasm": bool(item.get("contains_sarcasm")),
        "is_conditional": bool(item.get("is_conditional")),
    }


def _parse(raw: Optional[str]) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        log.warning("extractor: JSON parse failed")
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [p for p in (_coerce_prediction(p) for p in data) if p]


async def _call_claude(post_text: str) -> tuple[Optional[str], Any]:
    """Thin async wrapper — tests monkey-patch this."""
    sdk = client.get_async_client()
    if sdk is None:
        return None, None
    try:
        resp = await sdk.messages.create(
            model=client.ANTHROPIC_MODELS["extraction"],
            max_tokens=EXTRACTION_MAX_TOKENS,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": post_text[:8000]}],
        )
    except Exception as exc:
        log.error("extractor: Claude call failed: %s", exc)
        return None, None
    parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return ("".join(parts) if parts else None), resp


async def extract_predictions_from_post(
    post_text: str,
    post_id: Optional[str] = None,
    *,
    force: bool = False,
) -> list[dict]:
    """Public entrypoint. Returns a list of validated prediction dicts.

    ``post_id`` is recorded alongside each prediction for downstream
    idempotency; it is NOT part of the cache key — two posts with
    identical text bill once regardless of id.
    """
    if not post_text or not post_text.strip():
        return []

    key = _cache_key(post_text)

    if not force:
        cached = cache.get(key)
        if cached is not None:
            client.log_response(
                feature="extraction",
                model=client.ANTHROPIC_MODELS["extraction"],
                response=None, cached_hit=True,
            )
            return _attach_post_id(cached, post_id)

    raw, resp = await _call_claude(post_text)

    if resp is not None:
        client.log_response(
            feature="extraction",
            model=client.ANTHROPIC_MODELS["extraction"],
            response=resp, cached_hit=False,
        )
    else:
        client.log_failure(
            feature="extraction",
            model=client.ANTHROPIC_MODELS["extraction"],
        )

    predictions = _parse(raw)

    # Negative results get a short TTL so retries can happen; confirmed
    # predictions get the full 30-day window.
    ttl = EXTRACTION_TTL_SECONDS if (resp is not None and predictions) else FAILURE_TTL_SECONDS
    cache.set(
        key, predictions,
        ttl_seconds=ttl,
        feature="extraction",
        model=client.ANTHROPIC_MODELS["extraction"],
    )
    return _attach_post_id(predictions, post_id)


def _attach_post_id(predictions: list[dict], post_id: Optional[str]) -> list[dict]:
    if not post_id:
        return predictions
    return [dict(p, source_post_id=str(post_id)) for p in predictions]
