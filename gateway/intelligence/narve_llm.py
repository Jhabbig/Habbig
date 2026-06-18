"""Live Claude adapter for narve's info -> probability engine.

``intelligence/info_forecast.forecast()`` takes an injected ``llm`` callable
``(system_prompt, user_prompt) -> str`` so it stays unit-testable with no
network. This module is the production wiring that makes that callable actually
talk to Claude, routing through ``ai.client.call_claude`` so the global
kill-switch, usage log, and cost accounting all apply.

Two callables are exposed:

  - ``make_llm()`` returns the LIVE adapter. It wraps ``ai.client.call_claude``
    (async) and drives it from sync code, handling the running-event-loop case
    the same way ``intelligence/claude_client.get_intelligence_response`` does.
    On a kill-switch trip, a missing API key, or any None response it raises a
    clear ``RuntimeError`` — it NEVER silently returns 0.5. The caller decides
    what a missing forecast means; this layer refuses to fabricate one.

  - ``forecast_event(question, info_items)`` = the convenience entry point:
    ``info_forecast.forecast(question, info_items, llm=make_llm())``.

OFFLINE fallback (opt-in, clearly labelled):
    When env ``NARVE_OFFLINE=1`` is set, ``make_llm()`` returns a deterministic
    HEURISTIC llm instead of the real model. It computes a probability from a
    transparent keyword-sentiment scan over the info items and returns valid
    strict-JSON so the whole info -> % pipeline is runnable and testable with no
    API key. Its output is OBVIOUSLY marked as offline/heuristic in the digest
    and political_lean ("offline-heuristic") so it can never be mistaken for the
    real model's judgement. This exists for plumbing/CI only — it is NOT a model
    and must never be presented as one.

Model: env ``NARVE_FORECAST_MODEL`` (default ``claude-sonnet-4-5-20250929``).
"""
from __future__ import annotations

import asyncio
import os
from typing import Callable, List

from intelligence import info_forecast

# The feature tag used in the usage log / kill-switch accounting.
FORECAST_FEATURE = "narve_forecast"
DEFAULT_FORECAST_MODEL = "claude-sonnet-4-5-20250929"
FORECAST_MAX_TOKENS = 1024

# Sentinel that obviously marks heuristic output as NOT the real model.
OFFLINE_LEAN = "offline-heuristic"


def _model() -> str:
    return os.environ.get("NARVE_FORECAST_MODEL", DEFAULT_FORECAST_MODEL).strip() or DEFAULT_FORECAST_MODEL


def _offline_enabled() -> bool:
    return os.environ.get("NARVE_OFFLINE", "").strip() == "1"


# ── Live adapter ─────────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async coroutine from sync code, tolerating a running loop.

    Mirrors ``intelligence/claude_client.get_intelligence_response``: if there
    is no running loop (the normal case) use ``asyncio.run``; if a loop is
    already running (e.g. inside an async test harness) run the coroutine on a
    dedicated loop in a worker thread so we never call ``run_until_complete`` on
    an active loop.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is None:
        return asyncio.run(coro)

    # A loop is already running on this thread — offload to a fresh loop in a
    # separate thread and block for the result.
    import concurrent.futures

    def _worker():
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()


def make_live_llm() -> Callable[[str, str], str]:
    """Return the LIVE ``(system, user) -> str`` callable backed by Claude.

    Raises ``RuntimeError`` on kill-switch / missing key / None response.
    """
    from ai import client as ai_client

    model = _model()

    def llm(system_prompt: str, user_prompt: str) -> str:
        out = _run_async(
            ai_client.call_claude(
                feature=FORECAST_FEATURE,
                system=system_prompt,
                user=user_prompt,
                model=model,
                max_tokens=FORECAST_MAX_TOKENS,
            )
        )
        if out is None:
            # call_claude returns None on kill-switch, missing/invalid API key,
            # SDK-not-installed, or an API error — all already logged. We do NOT
            # invent a forecast; surface a clear error so the caller decides.
            raise RuntimeError(
                "narve forecast LLM call returned no text "
                "(kill-switch active, ANTHROPIC_API_KEY missing/invalid, or API error). "
                "Set NARVE_OFFLINE=1 to use the labelled offline heuristic instead."
            )
        return out

    return llm


# ── Offline heuristic (opt-in, clearly labelled) ─────────────────────────────

# Transparent, fixed keyword lists. This is a toy sentiment proxy, not a model.
_POSITIVE_WORDS = (
    "win", "wins", "winning", "won", "lead", "leads", "leading", "ahead",
    "surge", "strong", "rising", "gain", "gains", "favorite", "favourite",
    "likely", "confirmed", "passes", "passed", "approve", "approved", "up",
    "boost", "support", "momentum",
)
_NEGATIVE_WORDS = (
    "lose", "loses", "losing", "lost", "behind", "trail", "trails", "trailing",
    "drop", "drops", "weak", "falling", "decline", "underdog", "unlikely",
    "fails", "failed", "reject", "rejected", "down", "collapse", "doubt",
    "scandal", "loss",
)


def _heuristic_probability(info_items: List[dict]) -> float:
    """Probability proxy from keyword sentiment. Deterministic, in [0.05, 0.95]."""
    pos = neg = 0
    for it in info_items:
        text = str(it.get("text", "")).lower()
        for w in _POSITIVE_WORDS:
            pos += text.count(w)
        for w in _NEGATIVE_WORDS:
            neg += text.count(w)
    total = pos + neg
    if total == 0:
        return 0.5
    raw = pos / total
    # Clamp away from the 0/1 extremes — the heuristic is weakly informative.
    return round(max(0.05, min(0.95, raw)), 4)


def make_offline_llm() -> Callable[[str, str], str]:
    """Return a deterministic HEURISTIC ``(system, user) -> str`` callable.

    Returns valid strict-JSON that ``info_forecast._coerce`` accepts. The digest
    and political_lean are explicitly stamped as offline/heuristic so the output
    is never mistaken for the real model.
    """
    import json
    import re

    def llm(system_prompt: str, user_prompt: str) -> str:
        # Reconstruct the per-item lines info_forecast built so the heuristic
        # scans the same text. Lines look like: "[1] (reddit) some text".
        items: List[dict] = []
        for line in user_prompt.splitlines():
            m = re.match(r"^\[\d+\]\s*\(([^)]*)\)\s*(.*)$", line)
            if m:
                items.append({"platform": m.group(1), "text": m.group(2)})

        prob = _heuristic_probability(items)
        n = len(items)
        confidence = round(min(0.5, 0.1 + 0.05 * n), 4)  # weak, grows slowly, capped 0.5
        digest = (
            "[OFFLINE HEURISTIC — NOT the real model] Probability derived from a "
            "transparent positive/negative keyword scan over {n} info item(s); "
            "p={p}. For pipeline plumbing and tests only.".format(n=n, p=prob)
        )
        return json.dumps(
            {
                "probability_yes": prob,
                "confidence": confidence,
                "digest": digest,
                "drivers": ["offline keyword-sentiment heuristic (NARVE_OFFLINE=1)"],
                "political_lean": OFFLINE_LEAN,
            }
        )

    return llm


# ── Public factory + convenience entry point ─────────────────────────────────


def make_llm() -> Callable[[str, str], str]:
    """Return the forecasting llm callable.

    Live Claude adapter by default; the labelled offline heuristic when env
    ``NARVE_OFFLINE=1`` is set.
    """
    if _offline_enabled():
        return make_offline_llm()
    return make_live_llm()


def forecast_event(question: str, info_items: List[dict]) -> dict:
    """Generate narve's own probability for ``question`` from ``info_items``.

    Thin wrapper: ``info_forecast.forecast(question, info_items, llm=make_llm())``.
    Never blends a market price (that contract lives in info_forecast). Raises
    ValueError on empty/unparseable input and RuntimeError if the live LLM is
    unavailable (unless NARVE_OFFLINE=1).
    """
    return info_forecast.forecast(question, info_items, llm=make_llm())
