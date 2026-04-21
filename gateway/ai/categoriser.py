"""Claude Haiku market categorisation.

Called lazily when a market is first seen by the pipeline. Cached per
market_slug for 365 days — markets don't change category during their
life. The keyword matcher in backend.markets.unified_markets remains as
the hot-path fallback for uncached markets.

Output schema matches the spec exactly:

  primary_category, sub_category, tags[], political_leaning,
  sensitivity, relevance flags {insider_trading, environmental, expert}

Cached via ai.cache keyed ``categorise:<slug>``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ai import cache, client


log = logging.getLogger("ai.categoriser")


CATEGORISATION_TTL_SECONDS = 365 * 86400
FAILURE_TTL_SECONDS = 3600
CATEGORISATION_MAX_TOKENS = 600

VALID_PRIMARY = frozenset({
    "politics", "sports", "crypto", "finance", "geopolitics",
    "science_tech", "entertainment", "weather", "environment", "other",
})
VALID_LEANING = frozenset({"conservative", "liberal", "neutral", "n_a"})
VALID_SENSITIVITY = frozenset({"normal", "sensitive"})


CATEGORISATION_SYSTEM_PROMPT = """\
You categorise prediction markets for a prediction-market analytics product.

Given a market question and optional description, respond with ONE JSON
object matching this exact schema:

{
  "primary_category": "politics" | "sports" | "crypto" | "finance" |
                      "geopolitics" | "science_tech" | "entertainment" |
                      "weather" | "environment" | "other",
  "sub_category": "<short snake_case label>",
  "tags": ["5 to 10 relevant tags"],
  "political_leaning": "conservative" | "liberal" | "neutral" | "n_a",
  "sensitivity": "normal" | "sensitive",
  "relevance_signals": {
    "insider_trading_relevant": true | false,
    "environmental_impact_relevant": true | false,
    "requires_expert_knowledge": true | false
  }
}

No prose. No code fences.
"""


def _cache_key(market_slug: str) -> str:
    return f"categorise:{market_slug}"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse(raw: Optional[str], market_question: str) -> dict:
    if not raw:
        return _fallback(market_question)
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        log.warning("categoriser: JSON parse failed")
        return _fallback(market_question)
    if not isinstance(data, dict):
        return _fallback(market_question)

    primary = str(data.get("primary_category") or "").strip().lower()
    if primary not in VALID_PRIMARY:
        primary = "other"
    leaning = str(data.get("political_leaning") or "").strip().lower()
    if leaning not in VALID_LEANING:
        leaning = "n_a"
    sensitivity = str(data.get("sensitivity") or "").strip().lower()
    if sensitivity not in VALID_SENSITIVITY:
        sensitivity = "normal"

    tags_raw = data.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t)[:40] for t in tags_raw[:10] if t]

    signals = data.get("relevance_signals") or {}
    if not isinstance(signals, dict):
        signals = {}

    return {
        "primary_category": primary,
        "sub_category": (str(data.get("sub_category")) if data.get("sub_category") else None),
        "tags": tags,
        "political_leaning": leaning,
        "sensitivity": sensitivity,
        "insider_trading_relevant": bool(signals.get("insider_trading_relevant")),
        "environmental_impact_relevant": bool(signals.get("environmental_impact_relevant")),
        "requires_expert_knowledge": bool(signals.get("requires_expert_knowledge")),
    }


def _fallback(market_question: str) -> dict:
    """Cheap keyword fallback for when Claude is unavailable."""
    q = (market_question or "").lower()
    kw = {
        "crypto": ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"),
        "sports": ("nfl", "nba", "mlb", "soccer", "premier league", "tennis", "ufc", "mma"),
        "politics": ("election", "president", "senate", "midterm", "congress", "vote"),
        "finance": ("fed", "rates", "inflation", "cpi", "stock", "s&p"),
        "weather": ("weather", "rain", "snow", "temperature", "hurricane"),
        "environment": ("carbon", "co2", "emissions", "climate"),
        "geopolitics": ("russia", "ukraine", "china", "taiwan", "iran", "war"),
    }
    primary = "other"
    for cat, words in kw.items():
        if any(w in q for w in words):
            primary = cat
            break
    return {
        "primary_category": primary,
        "sub_category": None,
        "tags": [],
        "political_leaning": "n_a",
        "sensitivity": "normal",
        "insider_trading_relevant": False,
        "environmental_impact_relevant": primary == "environment",
        "requires_expert_knowledge": False,
    }


async def _call_claude(market_question: str, market_description: str) -> tuple[Optional[str], Any]:
    sdk = client.get_async_client()
    if sdk is None:
        return None, None
    user_msg = f"Market: {market_question}\n"
    if market_description:
        user_msg += f"Description: {market_description[:1000]}\n"
    try:
        resp = await sdk.messages.create(
            model=client.ANTHROPIC_MODELS["categorisation"],
            max_tokens=CATEGORISATION_MAX_TOKENS,
            system=CATEGORISATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        log.error("categoriser: Claude call failed: %s", exc)
        return None, None
    parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return ("".join(parts) if parts else None), resp


async def categorise_market(
    market_slug: str,
    market_question: str,
    market_description: str = "",
    *,
    force: bool = False,
) -> dict:
    """Return a categorisation dict for the given market. Cached 365 days."""
    if not market_slug:
        return _fallback(market_question)

    key = _cache_key(market_slug)
    if not force:
        cached = cache.get(key)
        if cached is not None:
            client.log_response(
                feature="categorisation",
                model=client.ANTHROPIC_MODELS["categorisation"],
                response=None, cached_hit=True,
            )
            return cached

    raw, resp = await _call_claude(market_question, market_description)
    if resp is not None:
        client.log_response(
            feature="categorisation",
            model=client.ANTHROPIC_MODELS["categorisation"],
            response=resp, cached_hit=False,
        )
    else:
        client.log_failure(
            feature="categorisation",
            model=client.ANTHROPIC_MODELS["categorisation"],
        )

    result = _parse(raw, market_question)
    ttl = CATEGORISATION_TTL_SECONDS if resp is not None else FAILURE_TTL_SECONDS
    cache.set(
        key, result,
        ttl_seconds=ttl,
        feature="categorisation",
        model=client.ANTHROPIC_MODELS["categorisation"],
    )
    return result
