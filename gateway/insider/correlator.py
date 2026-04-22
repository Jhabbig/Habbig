"""Claude-backed cross-reference of insider signals → active markets.

For each newly-inserted insider_signals row, the correlator asks Claude
Sonnet which currently-active Polymarket/Kalshi markets could plausibly
be affected and in which direction, with what confidence.

Input shape (one call per signal):

  {
    "signal": {source, actor, ticker, committees, action, amount_usd, ...},
    "markets": [{slug, question, category}, ...]      # up to 25
  }

Output:

  [
    {"market_slug", "correlation_type", "correlation_explanation",
     "implied_direction", "implied_confidence"}
  ]

``correlation_type`` values: direct | indirect | sector | political.

The correlator caches per (signal_id, market_slug) for 7 days — signals
don't change after disclosure, and the market set on a given day is
similar enough that re-correlating the same signal against a freshly-
shuffled market list is wasteful.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from ai import cache, client as ai_client
from insider.score import compute_insider_score


log = logging.getLogger("insider.correlator")


CORRELATION_MAX_TOKENS = 1200
CORRELATION_TTL_SECONDS = 7 * 86400

VALID_TYPES = frozenset({"direct", "indirect", "sector", "political"})
VALID_DIRECTION = frozenset({"yes", "no", "unclear"})
VALID_CONFIDENCE = frozenset({"high", "medium", "low", "speculative"})


CORRELATION_SYSTEM_PROMPT = """\
You cross-reference a single public-disclosure insider signal with a list
of currently-active prediction markets. Return a JSON array (can be empty)
of correlations the signal plausibly bears on.

Each element must be EXACTLY:

{
  "market_slug": "<must match one of the input market_slugs>",
  "correlation_type": "direct" | "indirect" | "sector" | "political",
  "correlation_explanation": "<1-2 sentences>",
  "implied_direction": "yes" | "no" | "unclear",
  "implied_confidence": "high" | "medium" | "low" | "speculative"
}

Rules:
  - Only correlate markets the signal plausibly affects. Prefer fewer,
    stronger correlations over many weak ones.
  - "direct" means the signal's ticker/actor is the market's subject.
  - "sector" means the signal's industry is the market's subject.
  - "political" means the signal is a congressional/FEC/lobbying event
    affecting a political market.
  - "indirect" is the catch-all for plausible second-order effects.
  - Return ``[]`` when no correlations apply.
  - No prose outside the JSON. No code fences.
"""


def _cache_key(signal_id: int, market_slug: str) -> str:
    return f"correlation:{signal_id}:{market_slug}"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse(raw: Optional[str], market_slugs: set[str]) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        log.warning("correlator: JSON parse failed")
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("market_slug") or "")
        if slug not in market_slugs:
            continue
        ctype = str(item.get("correlation_type") or "indirect").lower()
        if ctype not in VALID_TYPES:
            ctype = "indirect"
        direction = str(item.get("implied_direction") or "unclear").lower()
        if direction not in VALID_DIRECTION:
            direction = "unclear"
        conf = str(item.get("implied_confidence") or "speculative").lower()
        if conf not in VALID_CONFIDENCE:
            conf = "speculative"
        out.append({
            "market_slug": slug,
            "correlation_type": ctype,
            "correlation_explanation": str(item.get("correlation_explanation") or "")[:600],
            "implied_direction": direction,
            "implied_confidence": conf,
        })
    return out


async def _call_claude(signal_payload: dict, markets: list[dict]) -> tuple[Optional[str], Any]:
    user = {
        "signal": signal_payload,
        "markets": [{
            "market_slug": m.get("market_slug"),
            "question": m.get("question"),
            "category": m.get("category"),
        } for m in markets[:25]],
    }
    text = await ai_client.call_claude(
        feature="correlation",
        system=CORRELATION_SYSTEM_PROMPT,
        user=json.dumps(user)[:12000],
        model=ai_client.ANTHROPIC_MODELS["correlation"],
        max_tokens=CORRELATION_MAX_TOKENS,
    )
    return text, (True if text is not None else None)


async def correlate_signal(
    signal: dict,
    active_markets: list[dict],
    *,
    force: bool = False,
) -> list[dict]:
    """Return a list of correlation dicts for *signal* against *active_markets*.

    Each dict also includes ``insider_score`` — the weighted final score
    the UI sorts by — so the caller can write rows straight into
    ``insider_market_correlations`` without re-computing.

    ``signal`` must include ``id``, ``signal_strength``, ``disclosure_delay_days``,
    ``amount_significance``; the rest is passed through to Claude.
    """
    signal_id = int(signal.get("id") or 0)
    if not signal_id or not active_markets:
        return []

    # Short-circuit per-(signal, market) cache — full batch caching is
    # tempting but leads to inconsistent correlations when the active
    # market set rotates, so we cache at the pair level.
    market_slugs = {str(m.get("market_slug") or "") for m in active_markets if m.get("market_slug")}
    if not force:
        cached_all: list[dict] = []
        any_miss = False
        for slug in market_slugs:
            val = cache.get(_cache_key(signal_id, slug))
            if val is None:
                any_miss = True
                break
            if val.get("correlation_type"):  # cached non-empty
                cached_all.append(val)
        if not any_miss:
            ai_client.log_claude_usage_row(
                feature="correlation",
                model=ai_client.ANTHROPIC_MODELS["correlation"],
                cached_hit=True,
            )
            return _score_all(cached_all, signal)

    raw, _resp = await _call_claude(signal, active_markets)
    # call_claude already logged success, failure, or kill-switch.

    correlations = _parse(raw, market_slugs)
    # Cache each pair (including "no correlation" as a sentinel so we
    # don't re-ask Claude for the same pair within 7 days).
    correlated_slugs = {c["market_slug"] for c in correlations}
    now_payload = {"correlation_type": None, "at": int(time.time())}
    for slug in market_slugs:
        if slug in correlated_slugs:
            for c in correlations:
                if c["market_slug"] == slug:
                    cache.set(_cache_key(signal_id, slug), c,
                              ttl_seconds=CORRELATION_TTL_SECONDS,
                              feature="correlation",
                              model=ai_client.ANTHROPIC_MODELS["correlation"])
                    break
        else:
            cache.set(_cache_key(signal_id, slug), now_payload,
                      ttl_seconds=CORRELATION_TTL_SECONDS,
                      feature="correlation",
                      model=ai_client.ANTHROPIC_MODELS["correlation"])

    return _score_all(correlations, signal)


def _score_all(correlations: list[dict], signal: dict) -> list[dict]:
    out: list[dict] = []
    for c in correlations:
        score = compute_insider_score(
            signal_strength=signal.get("signal_strength"),
            disclosure_delay=_disclosure_delay_score(signal),
            amount_significance=signal.get("amount_significance"),
            correlation_confidence=c.get("implied_confidence"),
        )
        out.append(dict(c, insider_score=score))
    out.sort(key=lambda c: c.get("insider_score") or 0.0, reverse=True)
    return out


def _disclosure_delay_score(signal: dict) -> float:
    d = signal.get("disclosure_delay_days")
    if d is None:
        return 0.3
    try:
        d = float(d)
    except (TypeError, ValueError):
        return 0.3
    return max(0.0, min(1.0, 1.0 - (d / 45.0)))
