"""Claude API wrapper for the Intelligence assistant.

Supports both blocking responses (for tests) and async streaming (for the
SSE endpoint). The Anthropic SDK is imported lazily so the module loads
even when the dependency is missing in dev environments.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator


# Re-check the kill switch every N chunks during streaming so an
# operator-tripped switch terminates an in-flight stream within a
# bounded window rather than running to completion.
_KILL_SWITCH_CHECK_EVERY_N_CHUNKS = 5


class _KillSwitchTripped(Exception):
    """Raised mid-stream when the operator kill-switch flips on.

    Caught locally so the streaming generator can shut down the SDK
    context cleanly and still log the partial usage row.
    """


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
    """Legacy shim — delegates to ai.client.get_async_client so we don't
    instantiate the SDK twice. Kept so older tests that import this
    symbol keep working.
    """
    from ai import client as _ai_client
    sdk = _ai_client.get_async_client()
    if sdk is None:
        raise RuntimeError("Anthropic SDK not available (key missing or import failed)")
    return sdk


def get_intelligence_response(
    user: dict,
    user_message: str,
    history: list,
    context_text: str,
) -> str:
    """Blocking variant — returns the full assistant text.

    Used by tests and as a fallback if streaming is disabled. Routes
    through ai.client so the global kill-switch, usage log and cost
    accounting all apply; multi-turn history is folded into the user
    prompt so it fits the single-turn call_claude shape.
    """
    from ai import client as _ai_client
    if _ai_client.is_kill_switch_active():
        return "Intelligence assistant paused by operator (cost kill-switch). Try again later."
    tier = user.get("tier") or "none"
    system = INTELLIGENCE_SYSTEM_PROMPT.format(context=context_text, tier=tier)

    # Flatten the last-20 exchanges into a plain text prefix — simpler
    # than passing messages through and losing the streaming pathway's
    # consistency. Multi-turn users should prefer the streaming endpoint.
    history_text = "\n\n".join(
        f"{(m.get('role') or 'user').upper()}: {m.get('content') or ''}"
        for m in history[-20:]
        if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) and
        (m.get("content") if isinstance(m, dict) else getattr(m, "content", None))
    )
    full_user = f"{history_text}\n\nUSER: {user_message}" if history_text else user_message

    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Nested-loop case (tests). call_claude is async; use a new loop.
            raise RuntimeError("already running")
        out = loop.run_until_complete(_ai_client.call_claude(
            feature="intelligence",
            system=system,
            user=full_user,
            model=os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929"),
            max_tokens=2048,
            user_id=user.get("user_id"),
        ))
    except RuntimeError:
        out = asyncio.run(_ai_client.call_claude(
            feature="intelligence",
            system=system,
            user=full_user,
            model=os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929"),
            max_tokens=2048,
            user_id=user.get("user_id"),
        ))

    if out is None:
        return "Intelligence assistant temporarily unavailable."
    return out


async def stream_intelligence_response(
    user: dict,
    user_message: str,
    history: list,
    context_text: str,
) -> AsyncIterator[str]:
    """Yield text chunks from Claude as they arrive.

    Streaming is the one call path we cannot route through
    ``ai.client.call_claude`` — the SDK's stream context isn't
    non-blocking-compatible with the cache/short-circuit shape of that
    helper. We still honour the cost kill-switch and log final usage
    through ``ai.client.log_response`` after the stream completes, so
    the dashboards stay accurate.
    """
    from ai import client as _ai_client

    if _ai_client.is_kill_switch_active():
        yield "Intelligence assistant paused by operator (cost kill-switch). Try again later."
        return

    sdk = _ai_client.get_async_client()
    if sdk is None:
        yield "Intelligence assistant not configured (ANTHROPIC_API_KEY missing or SDK not installed)."
        _ai_client.log_failure(
            feature="intelligence",
            model=os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929"),
            user_id=user.get("user_id"),
        )
        return
    tier = user.get("tier") or "none"
    system = INTELLIGENCE_SYSTEM_PROMPT.format(context=context_text, tier=tier)
    messages = _build_messages(history, user_message)
    model = os.environ.get("INTELLIGENCE_MODEL", "claude-sonnet-4-5-20250929")

    # Track usage logging so a client disconnect (asyncio.CancelledError)
    # or operator-tripped kill switch still produces a row before we
    # propagate. The audit (MED 3) called out the gap where a long
    # stream begun before the switch trips runs to completion and any
    # mid-stream cancel skipped the bare ``except Exception`` log path.
    usage_logged = False
    kill_switch_tripped_mid_stream = False
    final_msg: object = None

    try:
        async with sdk.messages.stream(
            model=model,
            max_tokens=2048,
            system=system,
            messages=messages,
        ) as stream:
            chunk_count = 0
            async for text in stream.text_stream:
                chunk_count += 1
                # Re-check the kill switch every N chunks so an operator
                # flipping the switch mid-stream stops the stream within
                # a bounded number of chunks instead of running to the
                # end. _KillSwitchTripped is caught below so we still
                # log the partial-usage row.
                if chunk_count % _KILL_SWITCH_CHECK_EVERY_N_CHUNKS == 0:
                    if _ai_client.is_kill_switch_active():
                        kill_switch_tripped_mid_stream = True
                        raise _KillSwitchTripped()
                yield text
            # Normal completion — log final usage with the SDK's
            # aggregated counters from the final message object.
            try:
                final_msg = await stream.get_final_message()
                _ai_client.log_response(
                    feature="intelligence",
                    model=model,
                    response=final_msg,
                    cached_hit=False,
                    user_id=user.get("user_id"),
                )
                usage_logged = True
            except Exception:
                _ai_client.log_failure(
                    feature="intelligence", model=model,
                    user_id=user.get("user_id"),
                )
                usage_logged = True
    except _KillSwitchTripped:
        # Best-effort partial usage: try the SDK's final-message
        # aggregator; if the stream was torn down too early, fall back
        # to a failure row so the dashboard still shows a record.
        try:
            partial = await stream.get_final_message()
            _ai_client.log_response(
                feature="intelligence",
                model=model,
                response=partial,
                cached_hit=False,
                user_id=user.get("user_id"),
            )
        except Exception:
            _ai_client.log_failure(
                feature="intelligence", model=model,
                user_id=user.get("user_id"),
            )
        usage_logged = True
        yield "\n\n[Stream stopped: cost kill-switch tripped by operator.]"
    except (Exception, asyncio.CancelledError) as exc:
        # Always write a usage row before propagating — a client
        # disconnect raises asyncio.CancelledError, which the previous
        # bare ``except Exception`` missed entirely (audit MED 3).
        if not usage_logged:
            _ai_client.log_failure(
                feature="intelligence", model=model,
                user_id=user.get("user_id"),
            )
            usage_logged = True
        if isinstance(exc, asyncio.CancelledError):
            # Re-raise so the FastAPI task layer sees the cancellation
            # and unwinds normally instead of swallowing it.
            raise
        yield "Intelligence assistant failed mid-stream. Please retry."
