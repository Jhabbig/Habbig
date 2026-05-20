"""LLM-powered "Today in culture" digest.

Runs hourly in the background. Packs the current dashboard state (composite
index, surges, cross-source topics, edges, top news/memes) into a single
JSON snapshot and asks Claude for a 150-200 word zeitgeist brief in markdown.

The system prompt is stable — `cache_control` is set on it for forward
compatibility (writes are cheap; reads engage once the prompt or model
crosses the cache-min threshold). Default model is Haiku 4.5 (cheapest);
override via `CULTURE_DIGEST_MODEL` env to e.g. `claude-opus-4-7` for
richer prose.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

try:
    import anthropic
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

import cache
import edge
import index_calc
import surge_calc

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """\
You are the culture editor for a single-page dashboard. Each hour you receive a JSON snapshot of the current state of pop culture across 19 sources — memes, attention, entertainment, markets, news, language, lifestyle — plus surge signals (items climbing past their 7-day baseline), cross-platform topic clusters, and culture-bucket prediction markets.

Write a 150-200 word zeitgeist digest in markdown. Structure:

- A 2-sentence lede: what defines today's culture
- 3-5 bullet items: the specific stories, names, or topics that matter
- One forward-looking line: what to watch next (markets, surging topics)

Rules:
- Be concrete: use specific names, not categories ("Taylor Swift dropping a surprise album", not "a major music release").
- Skip hedging ("might", "could be", "appears to") and throat-clearing ("Here is your digest..."). Lead with content.
- If signal is thin (low total_score across sections, no surges, no topics), say so plainly in one short paragraph.
- When a cross-source topic is notable, reference its spread (e.g. "across TikTok, Reddit, and Wikipedia").
- Describe what culture markets are pricing — never recommend trades.
"""


def build_user_prompt() -> str:
    """Pack current dashboard state into a single user message."""
    snapshot: dict[str, Any] = {
        "index": index_calc.compute(),
        "surges": [
            {
                "title": s["title"],
                "source": s["source"],
                "section": s["section"],
                "score": s.get("score"),
                "z_score": s.get("z_score"),
            }
            for s in surge_calc.compute(limit=15)
        ],
        "topics": [
            {
                "label": t["label"],
                "spread": t["spread"],
                "sources": t["sources"],
                "sections": t["sections"],
                "total_score": round(t["total_score"], 1),
                "surge_signal": t.get("surge_signal"),
                "items": [it["title"] for it in t["items"][:3]],
                "markets": [m["title"] for m in t.get("markets", [])][:3],
            }
            for t in edge.compute_topics_with_markets(limit=12)
        ],
        "edges": [
            {
                "label": e["label"],
                "surge_signal": e["surge_signal"],
                "sections": e["sections"],
                "markets": [{"title": m["title"], "volume": m["volume"]}
                            for m in e["markets"]],
            }
            for e in edge.compute_edges(limit=6)
        ],
        "top_news": [
            {"title": n["title"], "source": (n.get("extra") or {}).get("feed")}
            for n in cache.get_section("news", limit=5)
        ],
        "top_memes": [
            {"title": m["title"], "source": m["source"]}
            for m in cache.get_section("memes", limit=5)
        ],
    }
    return (
        "Current state of culture:\n\n```json\n"
        + json.dumps(snapshot, indent=2, default=str)
        + "\n```"
    )


def generate(model: str | None = None) -> dict[str, Any] | None:
    """Generate a fresh digest. Returns None if API key or SDK is missing."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.info("digest: ANTHROPIC_API_KEY not set; skipping")
        return None
    if not _SDK_AVAILABLE:
        log.info("digest: anthropic SDK not installed; skipping")
        return None

    model = model or os.environ.get("CULTURE_DIGEST_MODEL", DEFAULT_MODEL)
    client = anthropic.Anthropic()
    user_text = build_user_prompt()

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            # Cache marker is forward-compat: engages once the system prompt
            # crosses the model's cache-min threshold (4096 tokens on Haiku 4.5
            # / Opus 4.7). At the current prompt size it's a no-op.
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }],
        messages=[{"role": "user", "content": user_text}],
    )
    body = next((b.text for b in response.content if b.type == "text"), "")
    return {
        "ts": time.time(),
        "model": model,
        "body_md": body,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    }
