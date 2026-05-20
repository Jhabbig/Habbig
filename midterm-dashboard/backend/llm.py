from __future__ import annotations
"""Claude API wrapper for movement explanations.

Design priorities, in order:

1. Grounding. Claude only sees the articles we hand it; the system prompt
   tells it to never invent sources, URLs, or headlines, and we validate
   the response's citations against the article list before returning it.
2. Structured output via ``output_config.format`` with a strict JSON
   schema — no free-form prose that we'd need to regex-parse.
3. Prompt caching. The system prompt is large and stable; we cache it so
   repeat calls only pay the volatile-suffix cost.
4. Cost. Default to Claude Opus 4.7 with adaptive thinking — the best
   model for nuanced "is this article causally relevant" judgements.
   Override via LLM_MODEL env var if a cheaper model is preferred.
"""

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def llm_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


_SYSTEM_PROMPT = """You are an analyst explaining short-term price movements in US election prediction markets (Polymarket, Kalshi, PredictIt, 538 polling averages).

You will receive:
1. A race identifier (e.g. senate_GA = 2026 Georgia Senate)
2. Per-source price movements with explicit timestamps (from_pct, to_pct, delta_pp)
3. A numbered list of news articles. Each article has: index, published_at (UTC), source, headline, url, snippet.

YOUR JOB: identify which provided articles, if any, plausibly explain the movement. Cite each by its index. Quote a short fragment from the article's snippet to support the citation.

STRICT RULES — violations make the response worthless:

1. **Use ONLY the provided articles.** Never invent sources, headlines, URLs, or quotes. Never reference articles that aren't in the numbered list. Never use phrases like "according to reports" or "many analysts believe" — only what the provided articles actually say.

2. **Honor causality.** An article published AFTER the movement ended cannot have caused it. The price movement's `to_time` is the end of the causal window. Reject any article whose `published_at` is later than `to_time`.

3. **Honor relevance.** An article must plausibly affect THIS race — a national-mood story or a different state's race is not an explanation. If no articles meet the bar, return an empty `explanations` array and set `reason_if_empty` to `"no_relevant_news_found"`.

4. **Calibrate confidence honestly:**
   - "high": article is directly about a candidate in THIS race or this specific contest in the right time window
   - "medium": article is about the race's state/issues but indirect (e.g. statewide policy story affecting the incumbent's brand)
   - "low": only thematically related (national party trend, generic polling story)

5. **Be brief.** The `summary` is 1–2 sentences in plain English. Each `rationale` is one sentence. Each `quote` is ≤ 200 chars copied verbatim from the article snippet.

6. **No speculation.** Don't extrapolate beyond the article snippets. If the article says "Senator X criticized the bill", don't write "this suggests X is losing moderate support" — just cite what the article said.

7. **If unsure, say so.** Returning empty explanations with `reason_if_empty: "no_relevant_news_found"` is the correct, useful response when articles don't support a causal claim. Inventing a connection is the failure mode."""


_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-2 sentence plain-English summary. Empty string if no relevant news.",
        },
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article_index": {"type": "integer", "description": "Index of the article from the input list."},
                    "headline": {"type": "string", "description": "Article headline, verbatim from the input."},
                    "url": {"type": "string", "description": "Article URL, verbatim from the input."},
                    "quote": {"type": "string", "description": "Verbatim fragment from the article snippet, max 200 chars."},
                    "rationale": {"type": "string", "description": "One sentence on why this article connects to the movement."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["article_index", "headline", "url", "quote", "rationale", "confidence"],
            },
        },
        "reason_if_empty": {
            "type": ["string", "null"],
            "enum": [
                None,
                "no_relevant_news_found",
                "source_disagreement",
                "timing_mismatch",
                "insufficient_movement",
            ],
            "description": "Set when explanations is empty.",
        },
    },
    "required": ["summary", "explanations", "reason_if_empty"],
}


def _format_user_payload(
    race_key: str,
    race_title: str,
    race_type: str,
    state: str,
    movements: list[dict],
    articles: list[dict],
    from_ts: datetime,
    to_ts: datetime,
) -> str:
    """Render the per-request data block.

    Lives entirely AFTER the cached system prompt so the prefix doesn't
    change between requests (prompt caching invariant: prefix bytes must
    match exactly across calls).
    """
    lines = [
        f"RACE: {race_key} ({race_title})",
        f"  type: {race_type or 'unknown'}, state: {state or 'unknown'}",
        f"  window: from_time = {from_ts.isoformat()}, to_time = {to_ts.isoformat()}",
        "",
        "MOVEMENTS (per-source price change over the window):",
    ]
    for m in movements:
        lines.append(
            f"  - {m.get('source')}: from {m.get('from'):.4f} → {m.get('to'):.4f} "
            f"(Δ = {m.get('delta_pp'):+.2f} pp)"
        )
    lines.append("")
    lines.append(f"NEWS ARTICLES ({len(articles)} total, indexed 0–{len(articles) - 1}):")
    if not articles:
        lines.append("  (no articles found in window)")
    for i, a in enumerate(articles):
        lines.append(f"  [{i}] published_at: {a.get('published_at', 'unknown')}")
        lines.append(f"      source: {a.get('source', '')}")
        lines.append(f"      headline: {a.get('headline', '')}")
        lines.append(f"      url: {a.get('url', '')}")
        snippet = (a.get("snippet") or "").replace("\n", " ").strip()
        lines.append(f"      snippet: {snippet[:400]}")
    lines.append("")
    lines.append(
        "TASK: Return the JSON object as specified. If no articles plausibly "
        "explain the movement, set explanations=[] and reason_if_empty='no_relevant_news_found'. "
        "Honor the strict rules in your system prompt."
    )
    return "\n".join(lines)


def _validate_response(parsed: dict, articles: list[dict]) -> dict:
    """Defense-in-depth: even with structured output, double-check that every
    citation references a real article from the input list. Drop fabricated
    ones rather than returning them to the user.
    """
    if not isinstance(parsed, dict):
        return {"summary": "", "explanations": [], "reason_if_empty": "no_relevant_news_found"}
    valid_explanations = []
    article_urls = {(a.get("url") or "").strip() for a in articles}
    article_headlines = {(a.get("headline") or "").strip() for a in articles}
    for exp in (parsed.get("explanations") or []):
        if not isinstance(exp, dict):
            continue
        idx = exp.get("article_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(articles):
            logger.warning(f"LLM cited out-of-range article_index={idx}; dropping")
            continue
        # The cited URL/headline must match the article at that index.
        src = articles[idx]
        if (exp.get("url") or "").strip() != (src.get("url") or "").strip():
            logger.warning(f"LLM URL mismatch at index {idx}; dropping fabricated citation")
            continue
        if (exp.get("headline") or "").strip() != (src.get("headline") or "").strip():
            # Headline mismatch but URL matches — keep, but log it
            logger.info(f"LLM headline mismatch at index {idx}; preserving citation")
            exp["headline"] = src.get("headline") or exp["headline"]
        # Soft sanity checks
        if (exp.get("url") or "").strip() not in article_urls:
            continue
        if (exp.get("headline") or "").strip() not in article_headlines:
            exp["headline"] = src.get("headline") or exp["headline"]
        valid_explanations.append(exp)
    # If we dropped every citation as fabricated, force the empty-reason
    # field so the frontend doesn't show a confident-looking summary
    # backed by zero evidence.
    if not valid_explanations:
        empty_reason = parsed.get("reason_if_empty") or "no_relevant_news_found"
        summary = ""  # also clear the summary — the LLM's narrative is unsupported
    else:
        empty_reason = None
        summary = (parsed.get("summary") or "").strip()
    return {
        "summary": summary,
        "explanations": valid_explanations,
        "reason_if_empty": empty_reason,
    }


async def explain_movement(
    *,
    race_key: str,
    race_title: str,
    race_type: str,
    state: str,
    movements: list[dict],
    articles: list[dict],
    from_ts: datetime,
    to_ts: datetime,
) -> dict:
    """Call Claude to explain the movement using the provided articles.

    Returns ``{summary, explanations, reason_if_empty, model, usage}``. On
    any failure, returns a safe empty result rather than raising — the
    caller is a public endpoint and a missing LLM key should not 500.
    """
    if not llm_configured():
        return {
            "summary": "",
            "explanations": [],
            "reason_if_empty": "no_relevant_news_found",
            "model": None,
            "usage": None,
            "configured": False,
        }
    if not articles:
        return {
            "summary": "",
            "explanations": [],
            "reason_if_empty": "no_relevant_news_found",
            "model": None,
            "usage": None,
            "configured": True,
        }

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; explanations disabled")
        return {
            "summary": "",
            "explanations": [],
            "reason_if_empty": "no_relevant_news_found",
            "model": None,
            "usage": None,
            "configured": False,
        }

    model = os.getenv("LLM_MODEL", "claude-opus-4-7").strip()
    user_payload = _format_user_payload(
        race_key, race_title, race_type, state, movements, articles, from_ts, to_ts,
    )

    client = anthropic.AsyncAnthropic()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            },
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_payload}],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        return {
            "summary": "",
            "explanations": [],
            "reason_if_empty": "no_relevant_news_found",
            "model": model,
            "usage": None,
            "configured": True,
            "error": str(e)[:200],
        }
    except Exception as e:
        logger.error(f"Unexpected LLM error: {e}", exc_info=True)
        return {
            "summary": "",
            "explanations": [],
            "reason_if_empty": "no_relevant_news_found",
            "model": model,
            "usage": None,
            "configured": True,
            "error": str(e)[:200],
        }
    finally:
        await client.close()

    # Pull the JSON text out of the first text block.
    parsed: Optional[dict] = None
    for block in response.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            import json
            try:
                parsed = json.loads(block.text)
                break
            except json.JSONDecodeError as e:
                logger.warning(f"LLM returned invalid JSON: {e}; raw: {block.text[:200]}")

    validated = _validate_response(parsed or {}, articles)
    validated["model"] = response.model
    validated["usage"] = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
    }
    validated["configured"] = True
    return validated
