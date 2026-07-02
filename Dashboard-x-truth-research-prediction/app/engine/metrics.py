"""In-process observability for the Prediction Engine.

Tracks exactly what the spec demands: per-stage latency percentiles, cache hit
rate, cost per 1k predictions, model-tier mix, and degraded rate — plus token
throughput so the cost model runs on measured numbers, not estimates. The
`cost_alert` flag flips when $/1k exceeds engine.cost.alert_usd_per_1k.

Everything here runs on the asyncio event loop (single thread), so plain
counters and deques are race-free without locks.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Deque, Dict, List

from app.engine.config import get_engine_config

_LATENCY_WINDOW = 5000  # per-stage ring buffer size


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def compute_cost_usd(model: str, tokens_in: int, tokens_out: int,
                     cached_tokens_in: int = 0, batch: bool = False) -> float:
    """Cost of one job's upstream token usage under the configured price table."""
    cost_cfg = get_engine_config().get("cost", {})
    prices = (cost_cfg.get("prices_per_mtok", {}) or {}).get(model)
    if not prices:
        return 0.0
    in_price = float(prices.get("input", 0.0)) / 1_000_000
    out_price = float(prices.get("output", 0.0)) / 1_000_000
    cached_discount = float(cost_cfg.get("cached_input_discount", 0.1))
    cost = (
        max(tokens_in - cached_tokens_in, 0) * in_price
        + cached_tokens_in * in_price * cached_discount
        + tokens_out * out_price
    )
    if batch:
        cost *= float(cost_cfg.get("batch_discount", 0.5))
    return cost


class EngineMetrics:
    def __init__(self) -> None:
        self.jobs = 0
        self.degraded_jobs = 0
        self.cache_hits = 0
        self.tier_mix: Counter = Counter()
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self._stage_latencies: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=_LATENCY_WINDOW))

    def observe_stage(self, stage: str, ms: float) -> None:
        self._stage_latencies[stage].append(ms)

    def record_job(self, *, degraded: bool, cache_hit: bool, tier: str,
                   tokens_in: int, tokens_out: int, cost_usd: float, latency_ms: float) -> None:
        self.jobs += 1
        if degraded:
            self.degraded_jobs += 1
        if cache_hit:
            self.cache_hits += 1
        self.tier_mix[tier] += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd
        self.observe_stage("total", latency_ms)

    def cost_per_1k(self) -> float:
        return (self.cost_usd / self.jobs) * 1000 if self.jobs else 0.0

    def snapshot(self) -> dict:
        threshold = float(get_engine_config().get("cost", {}).get("alert_usd_per_1k", 20.0))
        cost_per_1k = self.cost_per_1k()
        stages = {}
        for stage, window in self._stage_latencies.items():
            values = list(window)
            stages[stage] = {
                "p50_ms": round(_percentile(values, 0.50), 3),
                "p95_ms": round(_percentile(values, 0.95), 3),
                "p99_ms": round(_percentile(values, 0.99), 3),
                "n": len(values),
            }
        return {
            "jobs": self.jobs,
            "degraded_rate": round(self.degraded_jobs / self.jobs, 4) if self.jobs else 0.0,
            "cache_hit_rate": round(self.cache_hits / self.jobs, 4) if self.jobs else 0.0,
            "tier_mix": dict(self.tier_mix),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_tokens_per_job": round((self.tokens_in + self.tokens_out) / self.jobs, 1) if self.jobs else 0.0,
            "cost_usd": round(self.cost_usd, 6),
            "cost_per_1k_predictions_usd": round(cost_per_1k, 4),
            "cost_alert": cost_per_1k > threshold,
            "cost_alert_threshold_usd_per_1k": threshold,
            "stage_latency": stages,
        }

    def reset(self) -> None:
        self.__init__()
