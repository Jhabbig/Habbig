"""Per-market CO2 impact estimator — Claude Sonnet.

Called on-demand when a Pro user first views a market detail panel. The
analyser's Sonnet prompt is grounded in climate science + carbon
accounting and emits strict JSON so the UI can render without parsing
prose.

Caching — cache key ``env:<slug>`` with 24 h TTL. Additionally, if the
market price has moved ≥10% since the last generation, we force a
re-generation on the next view even within the window (the analysis
usually changes when the market's implied probability swings).

This module intentionally does not import backend.markets — callers
pass the fields they have in hand (``slug``, ``question``, ``category``,
``yes_price``). That keeps the hot path (Intelligence context builder,
markets filter) from having to spin up the market fetcher.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from ai import cache, client


log = logging.getLogger("ai.environmental")


ENV_TTL_SECONDS = 24 * 3600
PRICE_DRIFT_THRESHOLD = 0.10
ENV_MAX_TOKENS = 900

VALID_CONFIDENCE = frozenset({"high", "medium", "low", "speculative"})

UNIT_CONVERSIONS = {
    "co2_mt":   ("MT CO2e",          1.0),
    "trees":    ("trees planted",    45_871.0),
    "cars":     ("cars off road",    217_391.0),
    "homes":    ("homes powered",    86_957.0),
    "flights":  ("transatlantic flights", 500_000.0),
}


ENV_SYSTEM_PROMPT = """\
You estimate the climate impact of prediction-market outcomes. Your
audience is a trader deciding whether the market's outcome has material
carbon consequences. Ground every estimate in published climate science
and carbon accounting; do not invent numbers.

Given a market question and an optional category, respond with ONE JSON
object matching this exact schema:

{
  "is_relevant": true | false,
  "irrelevance_reason": "<short sentence, or null if relevant>",
  "yes_outcome_label": "YES",
  "no_outcome_label":  "NO",
  "yes_co2_impact_mt": <number — estimated CO2e impact in MT if YES resolves>,
  "no_co2_impact_mt":  <number — same for NO>,
  "yes_impact_description": "<1 sentence>",
  "no_impact_description":  "<1 sentence>",
  "yes_impact_timeframe": "<e.g. 'per year', 'over 10 years'>",
  "no_impact_timeframe":  "<same shape>",
  "confidence": "high" | "medium" | "low" | "speculative",
  "confidence_reason": "<short>",
  "data_sources": ["<citation 1>", "<citation 2>"]
}

If the market has no material environmental consequence, set
is_relevant=false and irrelevance_reason; leave numeric fields null.

No prose outside the JSON. No code fences.
"""


def _cache_key(market_slug: str) -> str:
    return f"env:{market_slug}"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        log.warning("environmental: JSON parse failed")
        return None
    if not isinstance(data, dict):
        return None

    conf = str(data.get("confidence") or "speculative").lower()
    if conf not in VALID_CONFIDENCE:
        conf = "speculative"

    def _num(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    sources = data.get("data_sources") or []
    if not isinstance(sources, list):
        sources = []

    return {
        "is_relevant": bool(data.get("is_relevant")),
        "irrelevance_reason": data.get("irrelevance_reason"),
        "yes_outcome_label": str(data.get("yes_outcome_label") or "YES"),
        "no_outcome_label": str(data.get("no_outcome_label") or "NO"),
        "yes_co2_impact_mt": _num(data.get("yes_co2_impact_mt")),
        "no_co2_impact_mt": _num(data.get("no_co2_impact_mt")),
        "yes_impact_description": data.get("yes_impact_description"),
        "no_impact_description": data.get("no_impact_description"),
        "yes_impact_timeframe": data.get("yes_impact_timeframe"),
        "no_impact_timeframe": data.get("no_impact_timeframe"),
        "confidence": conf,
        "confidence_reason": data.get("confidence_reason"),
        "data_sources": [str(s)[:300] for s in sources[:10]],
    }


def convert_co2(value: Optional[float], unit: str) -> dict:
    """Translate a CO2 megatons value into the user's preferred unit.

    Negative values (reductions) keep their sign for UI colour coding.
    Unknown units fall back to MT CO2e.
    """
    label, factor = UNIT_CONVERSIONS.get(unit, UNIT_CONVERSIONS["co2_mt"])
    if value is None:
        return {"value": None, "unit_key": unit if unit in UNIT_CONVERSIONS else "co2_mt",
                "unit_label": label}
    return {
        "value": round(float(value) * factor, 4),
        "unit_key": unit if unit in UNIT_CONVERSIONS else "co2_mt",
        "unit_label": label,
    }


async def _call_claude(market_question: str, category: str) -> tuple[Optional[str], Any]:
    user_msg = f"Market: {market_question}\nCategory: {category or 'unknown'}"
    text = await client.call_claude(
        feature="environmental",
        system=ENV_SYSTEM_PROMPT,
        user=user_msg,
        model=client.ANTHROPIC_MODELS["environmental"],
        max_tokens=ENV_MAX_TOKENS,
    )
    return text, (True if text is not None else None)


def _stub(reason: str) -> dict:
    return {
        "is_relevant": False,
        "irrelevance_reason": reason,
        "yes_outcome_label": "YES",
        "no_outcome_label": "NO",
        "yes_co2_impact_mt": None,
        "no_co2_impact_mt": None,
        "yes_impact_description": None,
        "no_impact_description": None,
        "yes_impact_timeframe": None,
        "no_impact_timeframe": None,
        "confidence": "speculative",
        "confidence_reason": reason,
        "data_sources": [],
    }


async def generate_environmental_impact(
    market_slug: str,
    market_question: str,
    *,
    category: str = "",
    yes_price: Optional[float] = None,
    force: bool = False,
) -> dict:
    """Return a climate-impact dict for the market. Cached 24h.

    ``force=True`` bypasses cache (used by Pro manual-refresh). Price
    drift ≥10% since the last generation triggers an automatic re-gen
    even when the cache is still valid.
    """
    if not market_slug:
        return _stub("missing market slug")

    key = _cache_key(market_slug)
    if not force:
        cached = cache.get(key)
        if cached is not None:
            # Price drift check — only the numeric impact values depend on the
            # market; descriptions are stable. We store the price we saw.
            last_price = cached.get("_yes_price_at_gen")
            if (yes_price is not None and last_price is not None
                    and abs(float(yes_price) - float(last_price)) >= PRICE_DRIFT_THRESHOLD):
                force = True
            else:
                client.log_claude_usage_row(
                    feature="environmental",
                    model=client.ANTHROPIC_MODELS["environmental"],
                    cached_hit=True,
                )
                return cached

    raw, _resp = await _call_claude(market_question, category)
    # call_claude already logged success, failure, or kill-switch.

    parsed = _parse(raw) or _stub("Claude unavailable or response invalid")
    parsed["_generated_at"] = int(time.time())
    parsed["_yes_price_at_gen"] = yes_price
    cache.set(
        key, parsed,
        ttl_seconds=ENV_TTL_SECONDS,
        feature="environmental",
        model=client.ANTHROPIC_MODELS["environmental"],
    )
    return parsed


def apply_user_unit_preference(payload: dict, unit: str) -> dict:
    """Augment a payload with unit-converted fields for the user's preferred unit."""
    yes = payload.get("yes_co2_impact_mt")
    no = payload.get("no_co2_impact_mt")
    return {
        **payload,
        "preferred_unit": unit,
        "yes_co2_impact_converted": convert_co2(yes, unit),
        "no_co2_impact_converted": convert_co2(no, unit),
    }
