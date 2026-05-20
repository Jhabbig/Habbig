"""Multi-model ensemble for a single market.

Fires the same prompt at Haiku 4.5, Sonnet 4.6, and Opus 4.7 in
parallel, collects each model's recommendation, and computes an
agreement summary. The system prompt is byte-identical across all
three models — each one has its own cache, so after warm-up an
ensemble call reads cache on every member.

Why this exists
---------------
The default tiered call (Haiku / Sonnet) is one decisive recommendation
per request. The ensemble is the other end of the spectrum: when the
user wants to know "do the models actually agree?", we surface all
three. A unanimous high-confidence call is much stronger evidence than
any single tier; a split is itself a signal that the market is in a
genuinely ambiguous zone.

Cost is bounded — three Haiku-equivalent calls per ensemble is ~3¢
after caching warms up. We expose this only as a user-triggered
endpoint (no auto-mode firing of ensembles).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import insight as _insight

logger = logging.getLogger(__name__)

# The three tiers we ensemble across. Order is deliberate — fast to
# slow — so the UI can show partial results while Opus is still
# generating, though the current endpoint returns synchronously after
# all three complete.
ENSEMBLE_MODELS = (
    _insight.MODEL_FAST,                 # claude-haiku-4-5
    _insight.MODEL_DEEP,                 # claude-sonnet-4-6
    "claude-opus-4-7",                   # most capable
)

ENSEMBLE_TIMEOUT_SECONDS = 90.0


def _drain_member(context: dict, model: str, client=None) -> dict:
    """Run one model to completion and return the `complete` chunk
    data, or a synthetic error chunk on failure. Never raises."""
    complete = None
    error = None
    try:
        for chunk in _insight.stream_insight(context, model=model, client=client):
            if chunk.type == "complete":
                complete = chunk.data
            elif chunk.type == "error":
                error = chunk.data
    except Exception as e:
        error = {"error": str(e), "type": type(e).__name__}
    if complete is None and error is None:
        error = {"error": "stream ended without complete or error", "type": "EmptyStream"}
    return {"model": model, "complete": complete, "error": error}


def run_ensemble(context: dict, *,
                 models=None,
                 client=None,
                 timeout: float = ENSEMBLE_TIMEOUT_SECONDS) -> dict:
    """Fan out to every model in `models` and join on completion.

    Returns
    -------
    dict with:
        members      list of {model, insight, usage, model_id, error}
        agreement    {level, recommendations, confidence}
        n_complete   number of members that produced an insight
        n_failed     number of members that errored

    `level`:
        "unanimous"   — all members agree on `recommendation`
        "majority"    — 2 of 3 (or N-1 of N) agree
        "split"       — no majority

    Designed to never raise — partial results are returned when some
    members error so the frontend can show "Opus failed, here are the
    other two".
    """
    # `models is None` falls back to defaults; an explicit empty tuple
    # is treated as "no models" and surfaces the error. Lets tests pin
    # the empty-input behavior without polluting the default path.
    chosen = tuple(models) if models is not None else ENSEMBLE_MODELS
    if not chosen:
        return {"members": [], "n_complete": 0, "n_failed": 0,
                "agreement": _agreement_summary([]),
                "error": "no models specified"}

    members: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        futures = {pool.submit(_drain_member, context, m, client): m
                   for m in chosen}
        for fut in as_completed(futures, timeout=timeout):
            members.append(fut.result())

    # Reorder by ENSEMBLE_MODELS so the frontend always renders
    # left-to-right in the same order regardless of completion order.
    order = {m: i for i, m in enumerate(chosen)}
    members.sort(key=lambda m: order.get(m["model"], 99))

    flattened = []
    for m in members:
        c = m.get("complete") or {}
        insight = c.get("insight")
        usage = c.get("usage")
        flattened.append({
            "model": m["model"],
            "insight": insight,
            "usage": usage,
            "stop_reason": c.get("stop_reason"),
            "error": m.get("error"),
        })

    n_complete = sum(1 for m in flattened if m["insight"] is not None)
    n_failed = sum(1 for m in flattened if m["insight"] is None)

    return {
        "members": flattened,
        "n_complete": n_complete,
        "n_failed": n_failed,
        "agreement": _agreement_summary(flattened),
    }


def _agreement_summary(members: list[dict]) -> dict:
    """Classify ensemble agreement on the recommendation enum."""
    valid = [m for m in members if m.get("insight")]
    if not valid:
        return {"level": "no_data", "recommendations": [],
                "confidences": [], "majority": None,
                "unanimous_confidence": None}

    recs = [m["insight"].get("recommendation") for m in valid]
    confs = [m["insight"].get("confidence") for m in valid]

    counts: dict[str, int] = {}
    for r in recs:
        counts[r] = counts.get(r, 0) + 1
    top_rec, top_count = max(counts.items(), key=lambda kv: kv[1])

    n = len(valid)
    # Classify: unanimous when every member agrees, majority when
    # strictly more than half agree, split otherwise. For n=3 that's
    # 3/3 → unanimous, 2/3 → majority, 1/1/1 → split.
    if top_count == n:
        level = "unanimous"
    elif top_count > n / 2:
        level = "majority"
    else:
        level = "split"

    # Surface confidence agreement only if recommendations are unanimous.
    unanimous_confidence = None
    if level == "unanimous":
        conf_counts: dict[str, int] = {}
        for c in confs:
            conf_counts[c] = conf_counts.get(c, 0) + 1
        if len(conf_counts) == 1:
            unanimous_confidence = next(iter(conf_counts))

    return {
        "level": level,
        "recommendations": recs,
        "confidences": confs,
        "majority": top_rec if level in ("unanimous", "majority") else None,
        "majority_count": top_count if level in ("unanimous", "majority") else None,
        "n_valid": n,
        "unanimous_confidence": unanimous_confidence,
    }
