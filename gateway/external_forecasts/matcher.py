"""Market equivalence matcher — pick which external candidate (if any)
asks the same question as our market.

Cross-platform market identity is genuinely hard. Two questions may be
semantically equivalent but worded completely differently, or superficially
similar but scoped differently (state-level vs national, next election vs
"by 2028"). A rule-based matcher drowns in false positives, so we hand the
decision to Claude Haiku: cheap, deterministic enough for our volume, and
good at catching subtle scope mismatches.

Contract:
  ``find_equivalent(our_market, candidates, provider)`` returns
  ``(best_candidate | None, confidence)``. Results are cached in
  ``market_equivalences`` for 90 days. Admin override pins the row
  forever.

Failure modes:
  - call_claude returns None (kill switch, SDK missing, timeout):
    we fall back to the single most plausible candidate (highest
    volume) at confidence 0.3 so admin review surfaces it. If there
    are no candidates, we return (None, 0.0).
  - Claude returns an ID we didn't offer: treated as "no match".
  - Claude returns "NONE": accepted, returned as (None, confidence).

All paths write SOMETHING to the equivalence cache — even a "NONE"
decision. That way re-running the sync job within the TTL window
doesn't pay for the matcher again just to reach the same conclusion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

import db_forecasts
from external_forecasts.base import Candidate


log = logging.getLogger("forecasts.matcher")


async def find_equivalent(
    our_market: dict,
    candidates: list[Candidate],
    *,
    provider: str,
    use_cache: bool = True,
) -> tuple[Optional[Candidate], float]:
    """Pick the best candidate (or None) from a fresh fetcher result.

    ``our_market`` must include ``market_slug`` (or ``slug``) and the
    question text. Everything else is optional but a ``close_at`` in
    unix seconds helps the LLM spot scope mismatches.
    """
    slug = str(our_market.get("market_slug") or our_market.get("slug") or "").strip()
    if not slug:
        return (None, 0.0)

    # Cache hit?
    if use_cache:
        existing = db_forecasts.get_equivalence(slug, provider)
        if existing is not None and db_forecasts.equivalence_is_fresh(existing):
            # Find the candidate in this fetch's list that matches the
            # cached ID; fall back to None (cache recorded a no-match).
            cached_id = existing["provider_market_id"]
            for c in candidates:
                if c.provider_market_id == cached_id:
                    return (c, float(existing["confidence"]))
            # Cached says "match id X" but X isn't in today's fetch — could
            # be a transient search ranking difference. Trust the cache; the
            # sync job will re-fetch the specific market by ID in a future
            # pass if we add that codepath.
            return (None, float(existing["confidence"]))

    if not candidates:
        return (None, 0.0)

    pick_id, confidence = await _pick_with_claude(our_market, candidates, provider)

    # Resolve pick_id → Candidate. Unknown IDs collapse to "NONE".
    chosen: Optional[Candidate] = None
    if pick_id and pick_id != "NONE":
        chosen = next((c for c in candidates if c.provider_market_id == pick_id), None)

    # Persist whichever way we landed — even "NONE" — so we don't keep
    # asking Claude the same question.
    if chosen is not None:
        db_forecasts.upsert_equivalence(
            market_slug=slug,
            provider=provider,
            provider_market_id=chosen.provider_market_id,
            provider_question=chosen.question,
            confidence=confidence,
            mapped_by="auto",
        )
    else:
        # Record a sentinel row so we skip this market on the next run.
        # Confidence of 0 marks "matcher said no match" — admin review
        # queue filters on confidence < 0.70 so the sentinel shows up
        # alongside weak matches for triage.
        db_forecasts.upsert_equivalence(
            market_slug=slug,
            provider=provider,
            provider_market_id="__no_match__",
            provider_question=None,
            confidence=0.0,
            mapped_by="auto",
        )

    return (chosen, confidence)


# ── Claude prompt ────────────────────────────────────────────────────


_SYSTEM = """You decide whether two prediction markets are asking the same question.
You will be given one market from narve.ai, and up to 8 candidate markets
from another prediction platform. Pick the candidate that asks the same
question, or say NONE if no candidate is close enough.

Rules:
  - Same question = same underlying real-world outcome and same scope.
  - "Will X happen by 2028" and "Will X happen in the next year" are NOT
    the same question.
  - State-level vs national scope = different question.
  - "Will company X announce product Y" and "Will product Y ship" = different.

Output format (strict, one line, no markdown):
  PICK: <candidate_id or NONE>
  CONFIDENCE: <0.00 to 1.00>
"""

_PROMPT_USER_TEMPLATE = """Our market (narve.ai):
  question: {question}
  close_at: {close_at}
  category: {category}

Candidates from {provider}:
{candidates_block}

Reply with PICK + CONFIDENCE only."""


_PICK_RE = re.compile(r"PICK:\s*([^\s\n]+)", re.IGNORECASE)
_CONF_RE = re.compile(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


async def _pick_with_claude(
    our_market: dict, candidates: list[Candidate], provider: str
) -> tuple[Optional[str], float]:
    user_text = _build_user_prompt(our_market, candidates, provider)
    cache_key = _cache_key(our_market, candidates, provider)

    try:
        from ai.client import call_claude
        reply = await call_claude(
            feature="forecast_matching",
            system=_SYSTEM,
            user=user_text,
            cache_key=cache_key,
            cache_ttl_seconds=86400,  # daily cache on top of the 90d DB TTL
            max_tokens=64,
        )
    except Exception as exc:  # noqa: BLE001 — never break the sync job
        log.warning("matcher: call_claude threw %r — falling back", exc)
        reply = None

    if not reply:
        return _fallback_pick(candidates)

    pick_match = _PICK_RE.search(reply)
    conf_match = _CONF_RE.search(reply)
    pick = pick_match.group(1).strip() if pick_match else None
    try:
        conf = float(conf_match.group(1)) if conf_match else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    if pick and pick.upper() == "NONE":
        return ("NONE", conf)
    return (pick, conf)


def _fallback_pick(candidates: list[Candidate]) -> tuple[Optional[str], float]:
    """No Claude available — pick the highest-volume candidate at low
    confidence so admin review surfaces it, or None if nothing has
    volume data."""
    with_volume = [c for c in candidates if c.volume]
    if not with_volume:
        return (None, 0.0)
    best = max(with_volume, key=lambda c: c.volume or 0)
    return (best.provider_market_id, 0.30)


def _build_user_prompt(
    our_market: dict, candidates: list[Candidate], provider: str
) -> str:
    close_at = our_market.get("close_at") or our_market.get("close_time") or "unknown"
    category = our_market.get("category") or "unknown"
    question = str(
        our_market.get("market_question") or our_market.get("question") or ""
    ).strip()
    lines: list[str] = []
    for c in candidates:
        lines.append(
            f"- id: {c.provider_market_id}"
            f" | q: {c.question[:160]}"
            f" | p: {c.probability:.3f}"
            f" | close: {c.close_at or 'unknown'}"
            f" | resolved: {c.resolved}"
        )
    return _PROMPT_USER_TEMPLATE.format(
        question=question,
        close_at=close_at,
        category=category,
        provider=provider,
        candidates_block="\n".join(lines) or "(none)",
    )


def _cache_key(
    our_market: dict, candidates: list[Candidate], provider: str
) -> str:
    """Stable key that changes when either our question or the candidate
    set changes — so a re-fetch with the same candidates hits cache,
    but a different shortlist forces a re-pick."""
    slug = str(our_market.get("market_slug") or our_market.get("slug") or "")
    question = str(our_market.get("market_question") or our_market.get("question") or "")
    cand_ids = ",".join(sorted(c.provider_market_id for c in candidates))
    h = hashlib.sha256(f"{slug}|{question}|{provider}|{cand_ids}".encode()).hexdigest()[:24]
    return f"fc-match:{h}"


# Re-exported for admin UI / tests.
__all__ = ["find_equivalent"]
