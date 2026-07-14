"""LLM-powered prediction extractor using Claude.

Falls back from the regex extractor when no patterns match. Extracts predictions
from natural-language posts that the regex misses — multi-clause sentences,
indirect speech, hedged predictions, mixed languages, etc.

Architecture:
  - Uses ``client.messages.parse()`` with a Pydantic schema so the model output
    is always valid structured JSON (no parsing errors).
  - System prompt is prompt-cached: it's stable across every call and rendered
    first, so the cache hit rate is essentially 100% after the first call.
  - Results are cached per (content_hash, model) in the ``extraction_cache``
    table — repeated posts (re-scrapes, viral quotes copy-pasted across many
    accounts) cost nothing.
  - Async (``AsyncAnthropic``) so the scheduler's pipeline isn't blocked.

If no API key is set, ``is_available()`` returns False and the extractor
gracefully no-ops — the regex extractor + keyword fallback handle everything.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import List, Literal, Optional

from sqlmodel import select

from app.config import settings
from app.db import AsyncSession, engine
from app.models import ExtractionCache
from app.processing.extractor import ExtractionResult

logger = logging.getLogger(__name__)


# Bump when the prompt's extraction semantics change — it namespaces the
# (content_hash, model) cache so stale extractions from an older prompt
# aren't served for new posts.
_PROMPT_VERSION = 2

_SYSTEM_PROMPT = """You extract betting-relevant predictions from social-media posts.

You will receive one short post (X / TruthSocial / Reddit / RSS). Decide
whether it contains any concrete prediction about a future event that could be
cross-referenced against a prediction market (Polymarket, Kalshi, Manifold).
Return all such predictions as structured JSON. If the post contains no
prediction, return an empty list.

A "prediction" is a forward-looking, falsifiable claim about a specific event,
made or clearly endorsed BY THE POSTER.
Examples that ARE predictions:
  - "Trump will win Pennsylvania" -> outcome=Yes, prob=null, category=politics
  - "BTC hits 150k by EOY, 70% chance" -> outcome=Yes, prob=0.70, category=crypto
  - "Lakers won't make the playoffs this year" -> outcome=No, prob=null, category=sports
  - "I give Russia a 30% chance of using a tactical nuke before the ceasefire"
    -> outcome=Yes, prob=0.30, category=geopolitics
  - "I'm 90% sure this bill won't pass" -> outcome=No, prob=0.90 (the speaker
    is 90% confident in the NO side — do not flip the number to 0.10)
  - "If the Fed cuts in March, gold goes to 3000" -> outcome=Yes, prob=null,
    category=other (this is conditional, but still a falsifiable forecast)
  - "Pundits keep saying the merger dies. They're wrong — it closes by June."
    -> outcome=Yes (the poster's OWN claim is the prediction, not the pundits')

Examples that are NOT predictions (return empty list):
  - "Trump won Pennsylvania" (past-tense fact, already resolved)
  - "I love Bitcoin" (opinion, no falsifiable outcome)
  - "Will the Lakers win?" (question, not a claim)
  - "Buy now, big sale" (commercial, not a forecast)
  - "Stocks dropped 5% today" (factual reporting of past)
  - 'RT @pundit: "The senate bill passes next week"' (someone ELSE's
    prediction merely relayed — the poster did not endorse it)
  - "Imagine believing BTC hits 1M this year lol" (mockery of a prediction,
    not an endorsement — if anything it implies the poster believes the
    opposite, but sarcasm is too unreliable to score; skip it)
  - "He predicted the crash back in 2021" (report about a past prediction)

Output schema (a JSON object with one key, "predictions", which is a list):
  {
    "predictions": [
      {
        "predicted_outcome": "Yes" | "No",
        "predicted_probability": float between 0 and 1, or null if not stated,
        "category": "politics" | "sports" | "crypto" | "geopolitics" | "other",
        "raw_text": short verbatim quote from the post (max ~120 chars) showing
                    the prediction,
        "confidence": float between 0 and 1 — your confidence that this is
                      genuinely a prediction (not an opinion or a question)
      }
    ]
  }

Rules:
  - "predicted_outcome" is always "Yes" or "No" — the YES/NO of the underlying
    event happening. A NO prediction means the speaker thinks the event won't
    happen (e.g. "Lakers won't make the playoffs" -> outcome=No).
  - "predicted_probability" is the speaker's stated confidence in their
    predicted_outcome. Include it only if the post states an explicit number,
    an odds fraction ("1 in 5 chance" -> 0.20), or a commonly-understood
    verbal probability. Calibrate verbal probabilities consistently:
      "certain" / "guaranteed" / "lock" -> 0.97   "almost certain" -> 0.92
      "very likely" / "highly likely"   -> 0.85   "likely" / "probably" -> 0.70
      "more likely than not"            -> 0.60   "coin flip" / "toss-up" -> 0.50
      "unlikely" / "doubt it"           -> 0.30   "very unlikely" -> 0.15
      "long shot" / "slim chance"       -> 0.12   "no chance" / "impossible" -> 0.03
    Otherwise return null. Never invent a number the speaker didn't imply.
  - Only extract the POSTER's own predictions. Quoted text, retweets,
    screenshots described, and "X says/predicts Y" reports are not the
    poster's claim unless the poster explicitly agrees ("this", "he's right",
    "exactly"). Mockery or disagreement is not an endorsement of either side.
  - Predictions about events that have clearly already resolved are not
    predictions — skip them.
  - "raw_text" must be a verbatim quote and should include the deadline or
    condition when the post states one ("by EOY", "before March", "if the
    Fed cuts") — downstream market matching depends on those qualifiers.
  - Be conservative on confidence. Hedged, sarcastic, or joke posts score low
    (<0.5). Only direct, falsifiable claims by the poster score >0.7.
  - Skip rhetorical questions, jokes, song lyrics, and commercial promotions.
  - One post may yield multiple predictions if it makes several distinct
    claims. Most posts yield 0 or 1.
  - Categories: politics (elections, legislation, polls), sports (games,
    seasons, championships), crypto (coin prices, protocols, ETFs),
    geopolitics (wars, sanctions, treaties), other (everything else, including
    weather, climate, economics, awards, science).

Respond with the JSON object only — no preamble, no markdown fences."""


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def is_available() -> bool:
    """True iff an Anthropic API key is set and LLM extraction isn't disabled."""
    if not settings.get("LLM_EXTRACTION_ENABLED", True):
        return False
    return bool(settings.get("ANTHROPIC_API_KEY"))


# Lazily-initialized client. Built inside extract() so import-time failures
# (e.g. missing API key in test environments) don't break the test harness.
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; LLM extraction disabled")
        return None
    api_key = settings.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    _client = AsyncAnthropic(api_key=api_key)
    return _client


def reset_client_for_tests() -> None:
    """Test hook — clear the cached client so a freshly-mocked client is picked up."""
    global _client
    _client = None


# Pydantic schema for messages.parse(). Defined inside a try-block so the module
# imports cleanly even if pydantic isn't installed yet (it ships with anthropic
# but the regex-only path shouldn't fail).
try:
    from pydantic import BaseModel, Field as PydField

    class _LLMPrediction(BaseModel):
        predicted_outcome: Literal["Yes", "No"]
        predicted_probability: Optional[float] = None
        category: Literal["politics", "sports", "crypto", "geopolitics", "other"] = "other"
        raw_text: str = ""
        confidence: float = PydField(default=0.5, ge=0.0, le=1.0)

    class _LLMExtractionResponse(BaseModel):
        predictions: List[_LLMPrediction] = []

    _PYDANTIC_OK = True
except ImportError:
    _PYDANTIC_OK = False
    _LLMExtractionResponse = None  # type: ignore[assignment]


async def _read_cache(content_hash: str, model: str) -> Optional[List[ExtractionResult]]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(ExtractionCache).where(
                ExtractionCache.content_hash == content_hash,
                ExtractionCache.model == model,
            )
        )
        row = result.first()
        if row is None:
            return None
        try:
            payload = json.loads(row.predictions_json)
        except (ValueError, TypeError):
            return None
        return [
            ExtractionResult(
                predicted_outcome=p.get("predicted_outcome", "Yes"),
                predicted_probability=p.get("predicted_probability"),
                raw_text=p.get("raw_text", "")[:200],
                extraction_method="llm_cached",
                category=p.get("category", "other"),
            )
            for p in payload
        ]


async def _write_cache(content_hash: str, model: str, results: List[ExtractionResult]) -> None:
    payload = [
        {
            "predicted_outcome": r.predicted_outcome,
            "predicted_probability": r.predicted_probability,
            "category": r.category,
            "raw_text": r.raw_text,
        }
        for r in results
    ]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            ExtractionCache(
                content_hash=content_hash,
                model=model,
                predictions_json=json.dumps(payload),
            )
        )
        try:
            await session.commit()
        except Exception as exc:
            # Two concurrent extracts of the same content can race past the
            # read-miss check and both try to insert. The unique constraint
            # on (content_hash, model) means the loser gets an IntegrityError.
            # That's the desired outcome — the cache has one valid row.
            await session.rollback()
            logger.debug("LLM cache insert race (expected on concurrent extracts): %s", exc)


async def extract(content: str) -> List[ExtractionResult]:
    """Extract predictions from `content` via Claude. Returns [] on any failure.

    Cached by (sha256(content), model) so duplicate posts cost nothing.
    """
    if not is_available() or not _PYDANTIC_OK:
        return []
    if not content or len(content.strip()) < 5:
        return []

    client = _get_client()
    if client is None:
        return []

    model = settings.get("LLM_EXTRACTOR_MODEL", "claude-opus-4-8")
    content_hash = _hash_content(content)
    # Cache key carries the prompt version: a prompt change alters extraction
    # semantics, so entries produced under an older prompt must not be reused.
    cache_key_model = f"{model}#p{_PROMPT_VERSION}"

    try:
        cached = await _read_cache(content_hash, cache_key_model)
        if cached is not None:
            return cached
    except Exception as exc:
        logger.warning("LLM extraction cache read failed: %s", exc)

    try:
        # Prompt caching on the system block. Render order is tools → system →
        # messages, so a stable system block is the easiest cache anchor. Once
        # the first request warms the cache, subsequent calls within the TTL
        # read it back at ~0.1× cost.
        #
        # Adaptive thinking: off-by-default on Opus 4.8 unless set explicitly.
        # The model decides per post whether to think — trivial posts stay
        # cheap, while hedged/sarcastic/quoted posts (exactly where extraction
        # errors live) get reasoning. max_tokens covers thinking + JSON.
        response = await client.messages.parse(
            model=model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content[:4000]}],
            output_format=_LLMExtractionResponse,
        )
    except Exception as exc:
        logger.warning("LLM extraction call failed: %s", exc)
        return []

    parsed = response.parsed_output
    if parsed is None:
        return []

    results: List[ExtractionResult] = []
    for p in parsed.predictions:
        # Drop low-confidence false positives — the model uses these for
        # hedged or sarcastic posts that aren't tradeable signals.
        if p.confidence < 0.5:
            continue
        results.append(
            ExtractionResult(
                predicted_outcome=p.predicted_outcome,
                predicted_probability=p.predicted_probability,
                raw_text=(p.raw_text or content[:120])[:200],
                extraction_method="llm",
                category=p.category,
            )
        )

    try:
        await _write_cache(content_hash, cache_key_model, results)
    except Exception as exc:
        logger.warning("LLM extraction cache write failed: %s", exc)

    return results
