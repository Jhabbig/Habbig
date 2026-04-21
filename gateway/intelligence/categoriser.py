"""Claude-powered market categorisation.

Replaces the keyword matcher in backend.markets.unified_markets._guess_category
with LLM classification cached for one year. Markets don't change category
during their life, so caching aggressively is safe; the Claude call itself
happens in a background cron (see jobs/ai_jobs.py) so the hot /api/markets
read path never waits on the LLM — uncached markets fall back to the
keyword matcher.

Output schema matches the brief:

  primary_category: politics | sports | crypto | finance | geopolitics |
                    science_tech | entertainment | weather | environment |
                    other
  sub_category:     free-form string (e.g. "us_elections", "fed_rates")
  tags:             list[str] — 5 to 10 relevant tags
  political_leaning: conservative | liberal | neutral | n_a
  sensitivity:      normal | sensitive
  relevance_signals:
    insider_trading_relevant:      bool
    environmental_impact_relevant: bool
    requires_expert_knowledge:     bool

Cached results are stored in the `market_categorisations` table — see
migrations/024_market_categorisations.py and the db.* helpers.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import db

from . import claude_usage


log = logging.getLogger("intelligence.categoriser")


CATEGORISATION_MAX_TOKENS = 512
CATEGORISATION_CACHE_TTL_SECONDS = 365 * 86400

VALID_PRIMARY = frozenset({
    "politics", "sports", "crypto", "finance",
    "geopolitics", "science_tech", "entertainment",
    "weather", "environment", "other",
})
VALID_LEANING = frozenset({"conservative", "liberal", "neutral", "n_a"})
VALID_SENSITIVITY = frozenset({"normal", "sensitive"})


CATEGORISATION_SYSTEM_PROMPT = """\
You categorise prediction markets into standard categories for a
prediction-market analytics product.

Given a market question, respond with a single JSON object matching this
exact schema. No prose, no code fences:

{
  "primary_category": "politics" | "sports" | "crypto" | "finance" |
                      "geopolitics" | "science_tech" | "entertainment" |
                      "weather" | "environment" | "other",
  "sub_category": "<short snake_case label, e.g. 'us_elections', 'fed_rates', 'btc_price'>",
  "tags": ["5 to 10 relevant tags"],
  "political_leaning": "conservative" | "liberal" | "neutral" | "n_a",
  "sensitivity": "normal" | "sensitive",
  "relevance_signals": {
    "insider_trading_relevant": true | false,
    "environmental_impact_relevant": true | false,
    "requires_expert_knowledge": true | false
  }
}

Guidance:
  - political_leaning should be "n_a" for non-political markets.
  - sensitivity = "sensitive" for death/injury markets, markets
    referring to named private individuals (not public figures), or
    anything that would be inappropriate to gamify publicly.
  - insider_trading_relevant = true when insiders (corporate officers,
    sports team staff, political aides) could plausibly have a material
    information advantage on the outcome.
  - environmental_impact_relevant = true when the resolved outcome has
    a direct environmental consequence (emissions, biodiversity, etc.).
  - requires_expert_knowledge = true for markets most retail users
    couldn't price without domain training (e.g. FDA approvals, niche
    geopolitics, crypto protocol upgrades).
"""


# ── Claude call (mockable) ───────────────────────────────────────────────────


async def _call_claude(market_title: str) -> tuple[Optional[str], Any]:
    """Thin Claude wrapper; tests monkey-patch this."""
    client = claude_usage.get_async_client()
    if client is None:
        return None, None
    try:
        resp = await client.messages.create(
            model=claude_usage.CATEGORISATION_MODEL,
            max_tokens=CATEGORISATION_MAX_TOKENS,
            system=CATEGORISATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Market question: {market_title}"}],
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


# ── Payload shaping ──────────────────────────────────────────────────────────


def _stub_payload(market_id: str, market_title: str) -> dict:
    """Safe fallback when Claude is unavailable or returns garbage. Uses
    the keyword matcher as best-effort guess so the product doesn't
    silently slide every market into 'other'.
    """
    from backend.markets.unified_markets import _guess_category as keyword_guess
    keyword = keyword_guess(market_title)
    # The keyword matcher's output domain overlaps but isn't identical to
    # ours (no 'science_tech', 'geopolitics', 'environment'); translate.
    remap = {
        "science": "science_tech",
        "world": "geopolitics",
    }
    primary = remap.get(keyword, keyword)
    if primary not in VALID_PRIMARY:
        primary = "other"

    now = int(time.time())
    return {
        "market_id": market_id,
        "market_title": market_title,
        "generated_at": now,
        "generated_by": "keyword_fallback",
        "cache_valid_until": now + CATEGORISATION_CACHE_TTL_SECONDS,
        "primary_category": primary,
        "sub_category": None,
        "tags": [],
        "political_leaning": "n_a",
        "sensitivity": "normal",
        "insider_trading_relevant": False,
        "environmental_relevant": False,
        "requires_expert_knowledge": False,
    }


def _parse_claude_response(raw: Optional[str], market_id: str, market_title: str) -> dict:
    if not raw:
        return _stub_payload(market_id, market_title)

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("categoriser: JSON parse failed for %s", market_id)
        return _stub_payload(market_id, market_title)

    if not isinstance(data, dict):
        return _stub_payload(market_id, market_title)

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

    sub_raw = data.get("sub_category")
    sub = str(sub_raw)[:80] if sub_raw else None

    now = int(time.time())
    return {
        "market_id": market_id,
        "market_title": market_title,
        "generated_at": now,
        "generated_by": claude_usage.CATEGORISATION_MODEL,
        "cache_valid_until": now + CATEGORISATION_CACHE_TTL_SECONDS,
        "primary_category": primary,
        "sub_category": sub,
        "tags": tags,
        "political_leaning": leaning,
        "sensitivity": sensitivity,
        "insider_trading_relevant": bool(signals.get("insider_trading_relevant")),
        "environmental_relevant": bool(signals.get("environmental_impact_relevant")),
        "requires_expert_knowledge": bool(signals.get("requires_expert_knowledge")),
    }


def _row_to_dict(row) -> dict:
    try:
        tags = json.loads(row["tags"] or "[]")
    except (TypeError, json.JSONDecodeError):
        tags = []
    return {
        "market_id": row["market_id"],
        "market_title": row["market_title"],
        "generated_at": row["generated_at"],
        "generated_by": row["generated_by"],
        "cache_valid_until": row["cache_valid_until"],
        "primary_category": row["primary_category"],
        "sub_category": row["sub_category"],
        "tags": tags,
        "political_leaning": row["political_leaning"],
        "sensitivity": row["sensitivity"],
        "insider_trading_relevant": bool(row["insider_trading_relevant"]),
        "environmental_relevant": bool(row["environmental_relevant"]),
        "requires_expert_knowledge": bool(row["requires_expert_knowledge"]),
    }


# ── Public entry points ──────────────────────────────────────────────────────


async def categorise_market(market: Any, *, force: bool = False) -> dict:
    """Return a categorisation dict for *market*. Cached for 1 year.

    *market* is duck-typed: we read `id` and `title` via getattr so
    UnifiedMarket dataclasses, SimpleNamespaces, and plain dicts all work.
    """
    market_id = getattr(market, "id", None) or (market.get("id") if isinstance(market, dict) else None)
    market_title = getattr(market, "title", None) or (market.get("title") if isinstance(market, dict) else None) or ""
    if not market_id:
        return _stub_payload("unknown", market_title)

    if not force:
        cached = db.get_market_categorisation(market_id)
        if cached:
            claude_usage.log_response(
                feature="categorisation",
                model=claude_usage.CATEGORISATION_MODEL,
                response=None,
                cached_hit=True,
            )
            return _row_to_dict(cached)

    raw, resp = await _call_claude(market_title)

    if resp is not None:
        claude_usage.log_response(
            feature="categorisation",
            model=claude_usage.CATEGORISATION_MODEL,
            response=resp,
            cached_hit=False,
        )
    else:
        claude_usage.log_failure(
            feature="categorisation",
            model=claude_usage.CATEGORISATION_MODEL,
        )

    payload = _parse_claude_response(raw, market_id, market_title)
    db.upsert_market_categorisation(market_id, payload)
    return payload


def lookup_cached_category(market_id: str) -> Optional[str]:
    """Fast, synchronous read of the primary_category only. Used by the
    unified_markets normaliser to tag a market on read if we happen to
    already have a categorisation cached — no Claude call ever happens
    here; on miss the caller keeps the keyword-matcher output until the
    cron fills the cache.
    """
    row = db.get_market_categorisation(market_id)
    if row:
        return row["primary_category"]
    return None
