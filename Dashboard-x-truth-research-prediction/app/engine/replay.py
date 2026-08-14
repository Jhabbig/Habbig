"""Replay harness — grade logged predictions against ground truth, re-score them
with the current fusion, and fit calibrators from the outcomes.

Three jobs:
  1. grade_pending()  — backfill FusionAudit.realized_outcome from ResolvedMarket
                        once the referenced market settles.
  2. replay()         — re-run the *current* fusion on every graded audit's stored
                        inputs and report accuracy, Brier score, a reliability
                        table, and a determinism check (same inputs → same output).
  3. fit_calibration()— fit Platt/isotonic params from (raw score, outcome) pairs.

⚠ Legal constraint: calibrator fitting consumes only our own logged predictions
vs realized outcomes (the fusion_audit table). X/Reddit content is a runtime
signal, never training data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Tuple

from sqlmodel import func, select

from app.db import AsyncSession
from app.engine.config import get_engine_config
from app.engine.fusion import fit_isotonic, fit_platt
from app.engine.schemas import EngineJob
from app.engine.service import compute_prediction
from app.models import FusionAudit, ResolvedMarket


def _job_from_audit(audit: FusionAudit) -> EngineJob:
    inputs = json.loads(audit.inputs_json or "{}")
    return EngineJob(
        job_id=audit.job_id,
        user_id=audit.user_id,
        job_class=audit.job_class,
        credibility=inputs.get("credibility", []),
        metrics=inputs.get("metrics", {}),
        context=inputs.get("context", {}),
    )


async def grade_pending(session: AsyncSession, limit: int = 5000) -> int:
    """Set realized_outcome on audits whose market has since resolved."""
    stmt = (
        select(FusionAudit, ResolvedMarket)
        .join(ResolvedMarket, FusionAudit.market_slug == ResolvedMarket.market_slug)
        .where(FusionAudit.realized_outcome.is_(None), FusionAudit.market_slug.isnot(None))
        .limit(limit)
    )
    rows = (await session.exec(stmt)).all()
    graded = 0
    now = datetime.now(timezone.utc)
    for audit, resolved in rows:
        outcome = (resolved.outcome or "").strip().lower()
        audit.realized_outcome = outcome in ("yes", "true", "1")
        audit.graded_at = now
        session.add(audit)
        graded += 1
    if graded:
        await session.commit()
    return graded


async def replay(session: AsyncSession, limit: int = 1000) -> dict:
    """Re-score graded historical jobs with the current fusion config."""
    stmt = (
        select(FusionAudit)
        .where(FusionAudit.realized_outcome.isnot(None))
        .order_by(FusionAudit.id.desc())
        .limit(limit)
    )
    audits = (await session.exec(stmt)).all()
    if not audits:
        return {"n": 0, "message": "no graded predictions yet — run grade_pending first"}

    cfg = get_engine_config()
    stored_brier = replayed_brier = 0.0
    stored_correct = replayed_correct = 0
    bins = [{"lo": i / 10, "hi": (i + 1) / 10, "n": 0, "sum_p": 0.0, "sum_y": 0.0} for i in range(10)]
    deterministic = True

    for audit in audits:
        y = 1.0 if audit.realized_outcome else 0.0
        job = _job_from_audit(audit)
        first = compute_prediction(job, cfg)
        second = compute_prediction(job, cfg)
        if first.prediction.p_yes != second.prediction.p_yes:
            deterministic = False
        p_new = first.prediction.p_yes
        p_old = audit.p_yes

        stored_brier += (p_old - y) ** 2
        replayed_brier += (p_new - y) ** 2
        stored_correct += int((p_old >= 0.5) == bool(audit.realized_outcome))
        replayed_correct += int((p_new >= 0.5) == bool(audit.realized_outcome))

        b = bins[min(9, int(p_new * 10))]
        b["n"] += 1
        b["sum_p"] += p_new
        b["sum_y"] += y

    n = len(audits)
    reliability = [
        {
            "bin": f"{b['lo']:.1f}-{b['hi']:.1f}",
            "n": b["n"],
            "mean_predicted": round(b["sum_p"] / b["n"], 4) if b["n"] else None,
            "observed_frequency": round(b["sum_y"] / b["n"], 4) if b["n"] else None,
        }
        for b in bins
    ]
    return {
        "n": n,
        "deterministic": deterministic,
        "stored": {"accuracy": round(stored_correct / n, 4), "brier": round(stored_brier / n, 4)},
        "replayed": {"accuracy": round(replayed_correct / n, 4), "brier": round(replayed_brier / n, 4)},
        "reliability": reliability,
        "fusion_config": {
            "strategy": cfg["fusion"].get("strategy"),
            "combination": cfg["fusion"].get("combination"),
            "calibration": cfg["fusion"].get("calibration", {}).get("method"),
        },
    }


async def fit_calibration(session: AsyncSession, method: str = "platt", limit: int = 5000) -> dict:
    """Fit calibration params from logged outcomes. Paste the result into
    config.yaml (engine.fusion.calibration) — hot-reloaded, no redeploy."""
    stmt = (
        select(FusionAudit.p_yes, FusionAudit.realized_outcome)
        .where(FusionAudit.realized_outcome.isnot(None))
        .order_by(FusionAudit.id.desc())
        .limit(limit)
    )
    rows = (await session.exec(stmt)).all()
    pairs: List[Tuple[float, bool]] = [(p, bool(y)) for p, y in rows]
    if not pairs:
        return {"n": 0, "message": "no graded predictions to fit on"}
    if method == "isotonic":
        points = fit_isotonic(pairs)
        return {"n": len(pairs), "method": "isotonic",
                "isotonic_points": [[round(x, 6), round(y, 6)] for x, y in points]}
    a, b = fit_platt(pairs)
    return {"n": len(pairs), "method": "platt", "platt": {"a": round(a, 6), "b": round(b, 6)}}


async def cost_readout(session: AsyncSession) -> dict:
    """One-page cost summary over the full audit history (measured, not estimated)."""
    total_row = (await session.exec(
        select(
            func.count(FusionAudit.id),
            func.sum(FusionAudit.tokens_in),
            func.sum(FusionAudit.tokens_out),
            func.sum(FusionAudit.cached_tokens_in),
            func.sum(FusionAudit.cost_usd),
            func.sum(FusionAudit.cache_hit),
            func.sum(FusionAudit.degraded),
        )
    )).one()
    jobs, tokens_in, tokens_out, cached_in, cost, cache_hits, degraded = (
        total_row[0] or 0, total_row[1] or 0, total_row[2] or 0,
        total_row[3] or 0, total_row[4] or 0.0, total_row[5] or 0, total_row[6] or 0,
    )
    tier_rows = (await session.exec(
        select(FusionAudit.model_tier, func.count(FusionAudit.id)).group_by(FusionAudit.model_tier)
    )).all()
    threshold = float(get_engine_config().get("cost", {}).get("alert_usd_per_1k", 20.0))
    cost_per_1k = (cost / jobs) * 1000 if jobs else 0.0
    return {
        "jobs": jobs,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cached_tokens_in": int(cached_in),
        "avg_tokens_per_job": round((tokens_in + tokens_out) / jobs, 1) if jobs else 0.0,
        "cache_hit_rate": round(cache_hits / jobs, 4) if jobs else 0.0,
        "degraded_rate": round(degraded / jobs, 4) if jobs else 0.0,
        "tier_mix": {tier or "unknown": count for tier, count in tier_rows},
        "total_cost_usd": round(float(cost), 6),
        "cost_per_1k_predictions_usd": round(cost_per_1k, 4),
        "cost_alert": cost_per_1k > threshold,
        "cost_alert_threshold_usd_per_1k": threshold,
    }
