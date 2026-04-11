"""Claude API wrapper for the Intelligence assistant.

Supports both blocking responses (for tests) and async streaming (for the
SSE endpoint). The Anthropic SDK is imported lazily so the module loads
even when the dependency is missing in dev environments.
"""

from __future__ import annotations

import os
from typing import AsyncIterator


INTELLIGENCE_SYSTEM_PROMPT = """\
You are the narve.ai Intelligence Assistant — an AI analyst with full
access to the narve.ai prediction market intelligence platform.

You have access to:
- Live prediction data scraped from X and TruthSocial
- Source credibility scores (Bayesian-smoothed, time-decay weighted)
- Polymarket and Kalshi market odds
- Narve expected value calculations
- The user's saved Signal Search topics and analyses
- Historical prediction accuracy data

Your role is to help the user:
1. Find high-value betting opportunities
2. Understand which sources to trust on which topics
3. Search and analyse their saved predictions
4. Interpret credibility scores and EV calculations
5. Get market intelligence on any topic

Guidelines:
- Be direct and data-driven. This is a trading tool, not a chatbot.
- Always cite your data sources (which predictions, which sources,
  what credibility scores)
- Flag uncertainty clearly ("only 2 sources on this market,
  both unrated — low confidence")
- Never give financial advice. You provide data and analysis only.
- If a user asks what to bet on, give them the data but remind them
  to make their own decision
- Keep responses concise unless depth is needed

Current context:
{context}

User tier: {tier}
"""


def _build_messages(history: list, user_message: str) -> list[dict]:
    """Convert DB rows / dicts into Anthropic message format.

    Keeps only the last 20 turns to bound prompt size.
    """
    messages: list[dict] = []
    for msg in history[-20:]:
        if hasattr(msg, "keys"):
            role = msg["role"]
            content = msg["content"]
        else:
            role = msg.get("role")
            content = msg.get("content")
        if role and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages


def _get_client():
    """Lazy import + construct an Anthropic client."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed") from e
    return anthropic.AsyncAnthropic(api_key=api_key)


def get_intelligence_response(
    user: dict,
    user_message: str,
    history: list,
    context_text: str,
) -> str:
    """Blocking variant — returns the full assistant text.

    Used by tests and as a fallback if streaming is disabled.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "Intelligence assistant not configured (missing ANTHROPIC_API_KEY)."
    try:
        import anthropic
    except ImportError:
        return "Intelligence assistant unavailable (anthropic SDK not installed)."
    client = anthropic.Anthropic(api_key=api_key)
    tier = user.get("tier") or "none"
    system = INTELLIGENCE_SYSTEM_PROMPT.format(context=context_text, tier=tier)
    messages = _build_messages(history, user_message)
    response = client.messages.create(
        model=os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=2048,
        system=system,
        messages=messages,
    )
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


async def stream_intelligence_response(
    user: dict,
    user_message: str,
    history: list,
    context_text: str,
) -> AsyncIterator[str]:
    """Yield text chunks from Claude as they arrive."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        yield "Intelligence assistant not configured (missing ANTHROPIC_API_KEY)."
        return
    try:
        import anthropic
    except ImportError:
        yield "Intelligence assistant unavailable (anthropic SDK not installed)."
        return
    client = anthropic.AsyncAnthropic(api_key=api_key)
    tier = user.get("tier") or "none"
    system = INTELLIGENCE_SYSTEM_PROMPT.format(context=context_text, tier=tier)
    messages = _build_messages(history, user_message)
    async with client.messages.stream(
        model=os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=2048,
        system=system,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text
