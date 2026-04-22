"""Shared helpers for Anthropic API cost accounting + model selection.

Every Claude-backed feature (extractor, categoriser, summariser, etc.)
imports `PRICES`, `cost_for`, and `log_response` from here so we have a
single source of truth for model IDs and per-token pricing. New models
are added by appending to `PRICES` — no other file changes needed.

Prices below are USD per million tokens and mirror the public Anthropic
rate card at the time this module was written. They are used solely for
the in-admin spend tracker and the daily alert threshold; the SDK never
sees them. Keeping them as plain floats lets tests override without
mocking the SDK.

All logging goes through `db.log_claude_usage`, which swallows its own
failures — a broken log write can never break a Claude call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import db


log = logging.getLogger("intelligence.usage")


# ── Model IDs (one knob per feature so ops can swap without code changes) ───

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
CATEGORISATION_MODEL = os.environ.get("CATEGORISATION_MODEL", "claude-haiku-4-5-20251001")
SUMMARISATION_MODEL = os.environ.get("SUMMARISATION_MODEL", "claude-sonnet-4-5-20250929")


# ── Pricing table (USD per 1M tokens) ───────────────────────────────────────
#
# Keyed by the exact model id the SDK was called with. Unknown model ids
# return (0.0, 0.0) from `cost_for`, which means an unfamiliar model
# shows up on the admin page as "calls made, $0 logged" — obvious enough
# to notice without crashing anything.

PRICES: dict[str, tuple[float, float]] = {
    # Haiku 4.5 — cheap + fast, used for extraction & categorisation.
    "claude-haiku-4-5-20251001": (0.25, 1.25),
    "claude-haiku-4-5-20250929": (0.25, 1.25),
    "claude-haiku-4-5": (0.25, 1.25),
    # Sonnet 4.5 — nuanced writing for summaries + the intelligence assistant.
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    # Opus 4.x — not used by any of the automated features but listed so
    # the admin page does not drop to $0 if someone triggers it manually.
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-5": (15.0, 75.0),
}


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of *input_tokens* + *output_tokens* at *model*'s published rate."""
    rates = PRICES.get(model) or (0.0, 0.0)
    in_rate, out_rate = rates
    return round(
        (float(input_tokens or 0) * in_rate + float(output_tokens or 0) * out_rate) / 1_000_000.0,
        6,
    )


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) off an Anthropic response.

    The SDK returns a `Usage` object with attributes; tests pass plain dicts
    or SimpleNamespace. Accept both. Missing usage returns (0, 0) so the
    logger still records a row — just with zero tokens/cost, which is fine
    for an anomaly investigation.
    """
    if response is None:
        return 0, 0
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0

    def _field(obj: Any, key: str) -> int:
        val = None
        if hasattr(obj, key):
            val = getattr(obj, key, None)
        elif isinstance(obj, dict):
            val = obj.get(key)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    return _field(usage, "input_tokens"), _field(usage, "output_tokens")


def log_response(
    *,
    feature: str,
    model: str,
    response: Any,
    cached_hit: bool = False,
) -> Optional[int]:
    """Record the usage + cost of one Claude response.

    Pass `response=None` with `cached_hit=True` to log a cache hit: one
    row is still written with zero tokens so cache-hit-rate math works.
    Returns the inserted row id, or None if logging failed (never raises).
    """
    try:
        if cached_hit:
            return db.log_claude_usage(
                feature=feature,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                cached_hit=True,
            )
        it, ot = _extract_usage(response)
        return db.log_claude_usage(
            feature=feature,
            model=model,
            input_tokens=it,
            output_tokens=ot,
            cost_usd=cost_for(model, it, ot),
            cached_hit=False,
        )
    except Exception:
        log.exception("log_response failed (feature=%s, model=%s)", feature, model)
        return None


def log_failure(*, feature: str, model: str) -> Optional[int]:
    """Record a zero-token row for a call that didn't return a usable response.

    Distinct from a cache hit: we want these in the log so admins see
    Claude-side incidents (invalid key, SDK exception) as a spike of
    zero-cost, non-cached calls in the dashboard.
    """
    try:
        return db.log_claude_usage(
            feature=feature, model=model,
            input_tokens=0, output_tokens=0,
            cost_usd=0.0, cached_hit=False,
        )
    except Exception:
        return None


# ── Lazy SDK factory ────────────────────────────────────────────────────────


def get_async_client() -> Any:
    """Legacy shim — delegates to ai.client.get_async_client so there's
    one SDK initialisation path. Kept so older tests importing this
    symbol keep working without touching ai.client directly.
    """
    from ai import client as _ai_client
    return _ai_client.get_async_client()
