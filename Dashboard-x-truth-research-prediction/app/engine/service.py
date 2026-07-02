"""The Prediction Engine orchestrator.

Flow per job: dedup-cache lookup → normalize → fuse → calibrate → degrade
handling → audit (async) → metrics. The fusion hot path makes no LLM calls —
Components 1 and 2 already ran upstream — so a request is pure CPU + one cache
round-trip, which is what lets a single worker sustain hundreds of concurrent
users with no per-request global locks. Horizontal scale = more stateless
workers sharing the Redis cache and the audit store.

Graceful degradation: a missing Component 1 or 2 never fails the request —
the engine fuses whatever signals are present, lowers the confidence by the
configured penalty, and sets degraded=true with machine-readable reasons.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Optional

from app.engine.audit import AuditWriter
from app.engine.cache import DedupCache
from app.engine.config import get_engine_config
from app.engine.fusion import FUSION_VERSION, build_fusion
from app.engine.metrics import EngineMetrics, compute_cost_usd
from app.engine.normalize import build_signals
from app.engine.schemas import ContributingSignal, EngineJob, EnginePrediction, PredictionPayload
from app.engine.tiering import resolve_model_tier
from app.models import FusionAudit


def _prompt_hash() -> str:
    """Hash of the upstream metric-extraction system prompt — part of the
    reproducibility fingerprint stored with every prediction."""
    try:
        from app.processing.llm_extractor import _SYSTEM_PROMPT
        return hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def compute_prediction(job: EngineJob, cfg: Optional[dict] = None) -> EnginePrediction:
    """Pure fusion computation — no cache, no audit, no I/O.

    Deterministic: the same job content always yields the same output (this is
    what the replay harness re-runs against stored inputs).
    """
    cfg = cfg or get_engine_config()
    signals, reasons = build_signals(job, cfg)
    fusion = build_fusion(cfg)
    result = fusion.fuse(signals)

    degradation = cfg["degradation"]
    total_weight = sum(cfg["fusion"]["weights"].values()) or 1.0
    coverage = sum(s.weight for s in signals) / total_weight

    confidence = result.agreement * coverage
    if "credibility_unavailable" in reasons:
        confidence *= 1.0 - float(degradation.get("missing_credibility_penalty", 0.25))
    if "metrics_unavailable" in reasons:
        confidence *= 1.0 - float(degradation.get("missing_metrics_penalty", 0.35))
    floor = float(degradation.get("confidence_floor", 0.05))
    confidence = min(0.99, max(floor, confidence))

    return EnginePrediction(
        job_id=job.job_id,
        prediction=PredictionPayload(p_yes=round(result.p_yes, 6), side="YES" if result.p_yes >= 0.5 else "NO"),
        confidence=round(confidence, 4),
        contributing_signals=[
            ContributingSignal(signal=s.name, weight=round(s.weight, 4), value=round(s.value, 4))
            for s in result.signals
        ],
        model_versions={
            "credibility": str(cfg.get("model_versions", {}).get("credibility", "credibility-v1")),
            "metric": job.metrics.model_version or "unknown",
            "fusion": f"{FUSION_VERSION}:{cfg['fusion'].get('strategy', 'weighted_v0')}",
        },
        degraded=bool(reasons),
        degraded_reasons=reasons,
        model_tier=resolve_model_tier(job.job_class),
    )


class PredictionEngine:
    def __init__(self, cache: Optional[DedupCache] = None,
                 metrics: Optional[EngineMetrics] = None,
                 audit: Optional[AuditWriter] = None) -> None:
        cfg = get_engine_config()
        cache_cfg = cfg.get("cache", {})
        self.cache = cache or DedupCache(
            ttl_seconds=int(cache_cfg.get("ttl_seconds", 900)),
            max_entries=int(cache_cfg.get("max_entries", 50000)),
        )
        self.metrics = metrics or EngineMetrics()
        self.audit = audit or AuditWriter()

    async def predict(self, job: EngineJob, *, write_audit: bool = True) -> EnginePrediction:
        t_start = time.perf_counter()
        cfg = get_engine_config()
        input_hash = job.content_hash()

        t0 = time.perf_counter()
        cached = await self.cache.get(input_hash)
        self.metrics.observe_stage("cache_lookup", (time.perf_counter() - t0) * 1000)

        if cached is not None:
            output = EnginePrediction(job_id=job.job_id, **cached)
            output.cache_hit = True
            # a cache hit consumed no new upstream tokens — that's the saving
            output.latency_ms = round((time.perf_counter() - t_start) * 1000, 3)
            self._finalize(job, output, input_hash, tokens=(0, 0, 0), cost=0.0, write_audit=write_audit)
            return output

        t1 = time.perf_counter()
        output = compute_prediction(job, cfg)
        self.metrics.observe_stage("fusion", (time.perf_counter() - t1) * 1000)

        tokens_in = int(job.metrics.usage.get("input_tokens", 0))
        tokens_out = int(job.metrics.usage.get("output_tokens", 0))
        cached_in = int(job.metrics.usage.get("cache_read_input_tokens", 0))
        cost = compute_cost_usd(
            output.model_tier, tokens_in, tokens_out, cached_tokens_in=cached_in,
            batch=job.job_class == "batch",
        )

        t2 = time.perf_counter()
        await self.cache.set(input_hash, output.model_dump(exclude={"job_id", "latency_ms", "cache_hit"}))
        self.metrics.observe_stage("cache_store", (time.perf_counter() - t2) * 1000)

        output.latency_ms = round((time.perf_counter() - t_start) * 1000, 3)
        self._finalize(job, output, input_hash, tokens=(tokens_in, tokens_out, cached_in), cost=cost, write_audit=write_audit)
        return output

    def _finalize(self, job: EngineJob, output: EnginePrediction, input_hash: str,
                  tokens: tuple[int, int, int], cost: float, write_audit: bool) -> None:
        tokens_in, tokens_out, cached_in = tokens
        if write_audit:
            self.audit.enqueue(FusionAudit(
                job_id=job.job_id or str(uuid.uuid4()),
                user_id=job.user_id,
                job_class=job.job_class,
                input_hash=input_hash,
                prompt_hash=_prompt_hash(),
                inputs_json=json.dumps({
                    "credibility": [c.model_dump() for c in job.credibility],
                    "metrics": job.metrics.model_dump(),
                    "context": job.context,
                }, default=str),
                signals_json=json.dumps([s.model_dump() for s in output.contributing_signals]),
                model_versions_json=json.dumps(output.model_versions),
                fusion_version=output.model_versions.get("fusion", FUSION_VERSION),
                model_tier=output.model_tier,
                p_yes=output.prediction.p_yes,
                confidence=output.confidence,
                bet_side=output.prediction.side,
                degraded=output.degraded,
                degraded_reasons_json=json.dumps(output.degraded_reasons),
                cache_hit=output.cache_hit,
                latency_ms=output.latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens_in=cached_in,
                cost_usd=cost,
                market_slug=job.context.get("market_slug"),
            ))
        self.metrics.record_job(
            degraded=output.degraded, cache_hit=output.cache_hit, tier=output.model_tier,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, latency_ms=output.latency_ms,
        )


_engine: Optional[PredictionEngine] = None


def get_engine() -> PredictionEngine:
    global _engine
    if _engine is None:
        _engine = PredictionEngine()
    return _engine


def reset_engine_for_tests() -> None:
    global _engine
    _engine = None
