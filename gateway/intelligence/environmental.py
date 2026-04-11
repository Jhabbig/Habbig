"""Claude-generated environmental impact analysis for prediction markets.

Estimates the CO2-equivalent impact of each possible outcome (YES/NO) of a
prediction market, with confidence scoring, plain-English descriptions, and
source citations. Results are cached in the `environmental_impacts` table
for 24 hours; see migrations/008_environmental_impact.py.

Generation policy
=================
* Lazy: only called when a Pro user requests env analysis for a market.
* Cached 24 h. Returned directly on cache hit.
* Auto-regenerates if the live market's yes_price drifted ≥ 10% since the
  cached row was generated (significant new information).
* Force-refresh available at the route level, capped at 5/day/user by the
  inline rate limiter.
* Markets in non-environmental categories get a stub `is_relevant=False`
  row cached for 24 h to prevent wasting Claude calls on Taylor Swift tour
  markets and the like.

Failure modes
=============
* Missing ANTHROPIC_API_KEY → returns a "not configured" stub. Route still
  responds 200 with usable data.
* Anthropic SDK not installed → ditto.
* Claude returns invalid JSON → log error, return safe stub. Never raises.
* Claude returns valid JSON missing fields → fill defaults, log warning,
  persist what we got.

The cached row is the source of truth — even stub responses are persisted
so subsequent requests don't re-trigger Claude.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import db


log = logging.getLogger("gateway.environmental")


# ── Configuration ────────────────────────────────────────────────────────────

ENV_MODEL = os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929")
ENV_MAX_TOKENS = 1024
CACHE_TTL_SECONDS = 24 * 3600
PRICE_DRIFT_REGEN_THRESHOLD = 0.10  # ≥ 10% movement → regenerate

# Markets in these categories may produce is_relevant=True. The set matches
# the categories that backend.markets.unified_markets._guess_category() can
# emit AND that plausibly have environmental implications. Markets in any
# other category get a stub stub immediately without calling Claude.
ENV_RELEVANT_CATEGORIES = frozenset({"politics", "weather", "science", "world"})

VALID_CONFIDENCE = frozenset({"high", "medium", "low", "speculative"})
VALID_CATEGORY = frozenset({"emissions", "energy", "biodiversity", "water", "mixed"})


# ── Unit conversions ─────────────────────────────────────────────────────────
#
# Constants from the spec. All conversions are linear from megatons CO2e:
# multiply MT by the factor to get the equivalent in the target unit.

CONVERSIONS: dict[str, tuple[str, int]] = {
    "co2_mt":  ("MT CO2e",                 1),
    "trees":   ("trees planted",           45_871),
    "cars":    ("cars off road",           217_391),
    "homes":   ("homes powered",           86_957),
    "flights": ("transatlantic flights",   500_000),
}


def convert_co2(mt: Optional[float], unit: str) -> dict:
    """Convert a megatons-CO2e value into the user's preferred unit.

    Returns a dict with `value`, `unit_label`, and `unit_key`. None inputs
    pass through as None values so the frontend can render an em-dash.
    Unknown units fall back to co2_mt rather than raising — UI cells should
    never crash because the user picked an obscure preference.
    """
    if unit not in CONVERSIONS:
        unit = "co2_mt"
    label, factor = CONVERSIONS[unit]
    if mt is None:
        return {"value": None, "unit_label": label, "unit_key": unit}
    return {
        "value": round(float(mt) * factor, 4),
        "unit_label": label,
        "unit_key": unit,
    }


# ── System prompt ────────────────────────────────────────────────────────────

ENV_SYSTEM_PROMPT = """\
You are an environmental impact analyst specialising in climate science,
carbon accounting, and policy analysis. You have deep knowledge of:
- CO2 equivalent emissions by sector
- IPCC carbon budgets and climate targets
- Energy policy and transition impacts
- Industrial and agricultural emissions
- Environmental economics

For the given prediction market, analyse both possible outcomes and estimate
their environmental impact. Be quantitative where possible. Cite your
reasoning. Be honest about uncertainty.

Respond ONLY with a single JSON object matching this exact schema. Do not
include any prose, code fences, or commentary outside the JSON:
{
  "is_relevant": true,
  "irrelevance_reason": null,
  "yes_co2_impact_mt": <number or null, negative = reduction, positive = increase>,
  "no_co2_impact_mt": <number or null>,
  "yes_impact_description": "<2-3 sentences in plain English>",
  "no_impact_description": "<2-3 sentences in plain English>",
  "yes_impact_timeframe": "<e.g. 'per year', 'over 10 years', 'one-time'>",
  "no_impact_timeframe": "<same>",
  "confidence": "high" | "medium" | "low" | "speculative",
  "confidence_reason": "<one sentence why>",
  "data_sources": ["<source 1>", "<source 2>", ...],
  "category": "emissions" | "energy" | "biodiversity" | "water" | "mixed"
}

If the market has no plausible environmental dimension (entertainment,
sports, celebrity gossip, etc.), instead respond:
{
  "is_relevant": false,
  "irrelevance_reason": "<one sentence why>",
  "yes_co2_impact_mt": null,
  "no_co2_impact_mt": null,
  "yes_impact_description": "",
  "no_impact_description": "",
  "yes_impact_timeframe": "",
  "no_impact_timeframe": "",
  "confidence": "speculative",
  "confidence_reason": "Not applicable",
  "data_sources": [],
  "category": "mixed"
}
"""


# ── Claude call (mockable) ───────────────────────────────────────────────────


async def _call_claude(market_question: str, market_category: str, yes_price: float) -> Optional[str]:
    """Single Claude call. Returns the raw text response, or None on error.

    Kept as a thin private wrapper so tests can monkey-patch this entrypoint
    and inject fixture responses without going near the Anthropic SDK.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("environmental: ANTHROPIC_API_KEY not set, returning stub")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("environmental: anthropic SDK not installed, returning stub")
        return None

    user_msg = (
        f"Analyse the environmental impact of this prediction market:\n"
        f"Title: {market_question}\n"
        f"Category: {market_category}\n"
        f"Current YES probability: {yes_price * 100:.0f}%\n"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model=ENV_MODEL,
            max_tokens=ENV_MAX_TOKENS,
            system=ENV_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts) if parts else None
    except Exception as exc:
        log.error("environmental: Claude call failed for %s: %s", market_question[:80], exc)
        return None


# ── Schema validation / coercion ─────────────────────────────────────────────


def _stub_payload(market_question: str, market_category: Optional[str], reason: str) -> dict:
    """Build a safe `is_relevant=False` stub. Used for non-env categories,
    Claude failures, and parser errors. Cached like a normal row so the
    next request doesn't re-trigger generation.
    """
    now = int(time.time())
    return {
        "market_question": market_question,
        "market_category": market_category,
        "generated_at": now,
        "generated_by": "stub",
        "cache_valid_until": now + CACHE_TTL_SECONDS,
        "is_relevant": False,
        "irrelevance_reason": reason,
        "yes_outcome_label": "YES",
        "no_outcome_label": "NO",
        "yes_co2_impact_mt": None,
        "no_co2_impact_mt": None,
        "yes_impact_description": "",
        "no_impact_description": "",
        "yes_impact_timeframe": "",
        "no_impact_timeframe": "",
        "confidence": "speculative",
        "confidence_reason": reason,
        "data_sources": [],
        "category": "mixed",
        "yes_market_price_at_gen": None,
    }


def _parse_claude_response(
    raw: str,
    market_question: str,
    market_category: Optional[str],
    yes_price: float,
) -> dict:
    """Parse Claude's JSON response into a fully-populated payload dict
    suitable for db.upsert_environmental_impact. Falls back to a stub if
    parsing or validation fails.
    """
    if not raw:
        return _stub_payload(market_question, market_category, "Empty response from analyser")

    # Strip code fences if Claude added them despite the system prompt.
    text = raw.strip()
    if text.startswith("```"):
        # Trim opening fence (with optional language tag) and trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("environmental: JSON parse failed for %s: %s", market_question[:80], exc)
        return _stub_payload(market_question, market_category, "Analyser returned invalid JSON")

    if not isinstance(data, dict):
        return _stub_payload(market_question, market_category, "Analyser returned non-object")

    is_relevant = bool(data.get("is_relevant"))
    if not is_relevant:
        # Honor the model's own irrelevance verdict but log it.
        return _stub_payload(
            market_question,
            market_category,
            data.get("irrelevance_reason") or "Analyser determined market is not environmental",
        )

    confidence = data.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        log.warning("environmental: invalid confidence %r for %s, defaulting to 'speculative'",
                    confidence, market_question[:80])
        confidence = "speculative"

    category = data.get("category")
    if category not in VALID_CATEGORY:
        log.warning("environmental: invalid category %r for %s, defaulting to 'mixed'",
                    category, market_question[:80])
        category = "mixed"

    sources = data.get("data_sources") or []
    if not isinstance(sources, list):
        sources = []
    sources = [str(s)[:200] for s in sources[:10] if s]

    def _coerce_mt(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    now = int(time.time())
    return {
        "market_question": market_question,
        "market_category": market_category,
        "generated_at": now,
        "generated_by": ENV_MODEL,
        "cache_valid_until": now + CACHE_TTL_SECONDS,
        "is_relevant": True,
        "irrelevance_reason": None,
        "yes_outcome_label": str(data.get("yes_outcome_label") or "YES")[:50],
        "no_outcome_label": str(data.get("no_outcome_label") or "NO")[:50],
        "yes_co2_impact_mt": _coerce_mt(data.get("yes_co2_impact_mt")),
        "no_co2_impact_mt": _coerce_mt(data.get("no_co2_impact_mt")),
        "yes_impact_description": str(data.get("yes_impact_description") or "")[:2000],
        "no_impact_description": str(data.get("no_impact_description") or "")[:2000],
        "yes_impact_timeframe": str(data.get("yes_impact_timeframe") or "")[:100],
        "no_impact_timeframe": str(data.get("no_impact_timeframe") or "")[:100],
        "confidence": confidence,
        "confidence_reason": str(data.get("confidence_reason") or "")[:500],
        "data_sources": sources,
        "category": category,
        "yes_market_price_at_gen": float(yes_price) if yes_price is not None else None,
    }


# ── Cache + generation orchestration ─────────────────────────────────────────


def _row_to_payload(row) -> dict:
    """Reshape a sqlite3.Row into the same dict shape the analyser produces.
    Used so cache hits and fresh generations return identical structures.
    """
    sources_raw = row["data_sources"] or "[]"
    try:
        sources = json.loads(sources_raw)
    except (json.JSONDecodeError, TypeError):
        sources = []
    return {
        "market_id": row["market_id"],
        "market_question": row["market_question"],
        "market_category": row["market_category"],
        "generated_at": row["generated_at"],
        "generated_by": row["generated_by"],
        "cache_valid_until": row["cache_valid_until"],
        "is_relevant": bool(row["is_relevant"]),
        "irrelevance_reason": row["irrelevance_reason"],
        "yes_outcome_label": row["yes_outcome_label"],
        "no_outcome_label": row["no_outcome_label"],
        "yes_co2_impact_mt": row["yes_co2_impact_mt"],
        "no_co2_impact_mt": row["no_co2_impact_mt"],
        "yes_impact_description": row["yes_impact_description"],
        "no_impact_description": row["no_impact_description"],
        "yes_impact_timeframe": row["yes_impact_timeframe"],
        "no_impact_timeframe": row["no_impact_timeframe"],
        "confidence": row["confidence"],
        "confidence_reason": row["confidence_reason"],
        "data_sources": sources,
        "category": row["category"],
        "yes_market_price_at_gen": row["yes_market_price_at_gen"],
    }


def _has_significant_drift(cached_row, current_yes_price: Optional[float]) -> bool:
    """True if the market's yes_price has moved ≥ PRICE_DRIFT_REGEN_THRESHOLD
    since *cached_row* was generated. Used to invalidate stale analyses
    after a major news event without requiring manual refresh.
    """
    if current_yes_price is None:
        return False
    prior = cached_row["yes_market_price_at_gen"]
    if prior is None:
        return False
    return abs(float(current_yes_price) - float(prior)) >= PRICE_DRIFT_REGEN_THRESHOLD


async def generate_environmental_impact(market: Any, *, force: bool = False) -> dict:
    """Return an environmental analysis for *market*. Cached for 24h.

    *market* is a UnifiedMarket-like object with attributes `id`, `title`,
    `category`, `yes_price`. Duck-typed so tests can pass a SimpleNamespace.

    Order of operations:
      1. If force=False and a fresh cached row exists for this market_id,
         and the price has not drifted significantly, return the cache.
      2. If the market's category is not in ENV_RELEVANT_CATEGORIES, write
         and return a stub immediately (no Claude call).
      3. Otherwise call Claude, parse the response, persist it, return it.

    Never raises — Claude failures fall back to stub responses.
    """
    market_id = getattr(market, "id", None)
    if not market_id:
        return _stub_payload("(unknown market)", None, "Missing market id")

    market_question = getattr(market, "title", "") or ""
    market_category = getattr(market, "category", None)
    yes_price = getattr(market, "yes_price", None)

    if not force:
        cached_fresh = db.get_environmental_impact(market_id)
        if cached_fresh and not _has_significant_drift(cached_fresh, yes_price):
            return _row_to_payload(cached_fresh)
        # Even if expired, check drift on the most recent row to log why
        # we're regenerating (helps debugging unexpected Claude calls).
        cached_any = db.get_environmental_impact_any_age(market_id)
        if cached_any and _has_significant_drift(cached_any, yes_price):
            log.info("environmental: regenerating %s due to ≥10%% price drift", market_id)

    # Skip Claude entirely for categories we know aren't environmental.
    if market_category not in ENV_RELEVANT_CATEGORIES:
        payload = _stub_payload(
            market_question,
            market_category,
            f"Markets in category '{market_category}' are not analysed for environmental impact.",
        )
        db.upsert_environmental_impact(market_id, payload)
        payload["market_id"] = market_id
        return payload

    raw = await _call_claude(market_question, market_category or "general", float(yes_price or 0.5))
    if raw is None:
        payload = _stub_payload(
            market_question,
            market_category,
            "Analyser temporarily unavailable",
        )
    else:
        payload = _parse_claude_response(raw, market_question, market_category, float(yes_price or 0.0))

    db.upsert_environmental_impact(market_id, payload)
    payload["market_id"] = market_id
    return payload


def apply_user_unit_preference(payload: dict, unit: str) -> dict:
    """Augment a payload dict with `yes_co2_impact_converted` and
    `no_co2_impact_converted` fields rendered in the user's preferred unit.
    The original MT values stay in place so clients can switch units
    client-side without a refetch.
    """
    out = dict(payload)
    out["yes_co2_impact_converted"] = convert_co2(payload.get("yes_co2_impact_mt"), unit)
    out["no_co2_impact_converted"] = convert_co2(payload.get("no_co2_impact_mt"), unit)
    out["preferred_unit"] = unit
    return out
