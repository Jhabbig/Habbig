"""Claude-powered prediction extraction (F10).

Replaces/augments keyword-based prediction detection with LLM understanding.
Processes batches of scraped posts through Claude Haiku to extract structured
prediction data: direction, probability, category, and market keywords.

Follows the same pattern as intelligence/environmental.py:
  - Lazy (only called when posts need processing)
  - Structured JSON output
  - Cost-controlled (Haiku model, batched, rate-limited)
  - Stub on failure (never crashes the pipeline)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("intelligence.extractor")

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-haiku-4-5-20250929")
EXTRACTION_MAX_TOKENS = 2048
BATCH_SIZE = 15  # posts per Claude call

EXTRACTION_SYSTEM_PROMPT = """\
You are a prediction extraction engine for prediction markets (Polymarket, Kalshi).
Analyze social media posts and identify explicit or implicit predictions about
future events.

For each post, determine:
1. Is this a genuine prediction? (Exclude jokes, sarcasm, questions, retweets of others)
2. What direction? (YES the event will happen, or NO it won't)
3. What probability does the author assign? (Extract if stated; infer if implied)
4. What category? (politics, crypto, sports, weather, geopolitics, economics, other)
5. What market keywords would help match this to a prediction market?

Respond ONLY with a JSON array. One element per post, using the post's index:

[
  {
    "index": 0,
    "is_prediction": true,
    "direction": "YES",
    "probability": 0.75,
    "category": "politics",
    "market_keywords": ["trump", "2024 election", "republican nominee"],
    "confidence": "high",
    "reasoning": "Author explicitly states probability"
  },
  {
    "index": 1,
    "is_prediction": false,
    "direction": null,
    "probability": null,
    "category": null,
    "market_keywords": [],
    "confidence": null,
    "reasoning": "This is a question, not a prediction"
  }
]

Rules:
- "probability" should be 0.0-1.0 (null if not estimable)
- "confidence" in your extraction: "high" (explicit prediction), "medium" (implied), "low" (weak signal)
- Sarcasm, jokes, and rhetorical questions are NOT predictions
- "I think X might happen" IS a prediction (medium confidence, ~0.55-0.65 probability)
- "X is definitely going to happen" IS a prediction (high confidence, 0.85+)
- "I bet X" IS a prediction even if informal
- Always return the same number of elements as posts received
"""


async def extract_predictions_from_posts(
    posts: list[dict],
) -> list[dict]:
    """Process a batch of posts through Claude Haiku to extract predictions.

    Each post dict should have at least: {content: str, author_handle: str}
    Returns a list of extraction results (same length as input).
    """
    if not posts:
        return []

    # Build user message
    post_lines = []
    for i, post in enumerate(posts[:BATCH_SIZE]):
        content = (post.get("content") or "")[:500]  # truncate for token budget
        author = post.get("author_handle", "unknown")
        post_lines.append(f"[Post {i}] @{author}: {content}")

    user_message = "\n\n".join(post_lines)

    # Call Claude
    response_text = await _call_claude(user_message)
    if response_text is None:
        # Stub: return "not a prediction" for everything
        return [_stub_result(i) for i in range(len(posts))]

    # Parse response
    try:
        results = json.loads(response_text)
        if not isinstance(results, list):
            results = [results]
    except (json.JSONDecodeError, TypeError):
        log.warning("Extraction response not valid JSON, returning stubs")
        return [_stub_result(i) for i in range(len(posts))]

    # Pad if Claude returned fewer results than posts
    while len(results) < len(posts):
        results.append(_stub_result(len(results)))

    return results[:len(posts)]


async def _call_claude(user_message: str) -> Optional[str]:
    """Call Claude Haiku for extraction. Returns raw text or None."""
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed, skipping extraction")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=EXTRACTION_MAX_TOKENS,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text if response.content else None
    except Exception as exc:
        log.exception("Claude extraction call failed: %s", exc)
        return None


def _stub_result(index: int) -> dict:
    """Fallback result when extraction fails."""
    return {
        "index": index,
        "is_prediction": False,
        "direction": None,
        "probability": None,
        "category": None,
        "market_keywords": [],
        "confidence": None,
        "reasoning": "extraction_failed",
    }
