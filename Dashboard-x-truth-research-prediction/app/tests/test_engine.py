"""Unit tests for the Stage-3 engine building blocks: fusion math, calibrators,
normalization, dedup cache, tier routing, metrics."""
from __future__ import annotations

import time

import pytest

from app.engine.cache import DedupCache
from app.engine.config import get_engine_config, set_engine_config_override
from app.engine.fusion import (
    IsotonicCalibrator,
    PlattCalibrator,
    SignalReading,
    WeightedEnsembleFusion,
    build_fusion,
    fit_isotonic,
    fit_platt,
)
from app.engine.metrics import EngineMetrics, compute_cost_usd
from app.engine.normalize import build_signals, normalize_extracted_features, normalize_market_implied
from app.engine.schemas import EngineJob
from app.engine.tiering import resolve_model_tier


@pytest.fixture(autouse=True)
def _clear_config_override():
    yield
    set_engine_config_override(None)


# ---------------------------------------------------------------------------
# Fusion math
# ---------------------------------------------------------------------------
def test_linear_combination_is_weighted_mean():
    fusion = WeightedEnsembleFusion(combination="linear")
    result = fusion.fuse([SignalReading("a", 0.8, 0.75), SignalReading("b", 0.4, 0.25)])
    assert result.p_yes == pytest.approx(0.8 * 0.75 + 0.4 * 0.25)


def test_logistic_neutral_at_half():
    fusion = WeightedEnsembleFusion(combination="logistic", logistic_scale=4.0)
    result = fusion.fuse([SignalReading("a", 0.5, 1.0)])
    assert result.p_yes == pytest.approx(0.5)


def test_logistic_monotone_in_signal_value():
    fusion = WeightedEnsembleFusion(combination="logistic")
    low = fusion.fuse([SignalReading("a", 0.3, 1.0)]).p_yes
    high = fusion.fuse([SignalReading("a", 0.9, 1.0)]).p_yes
    assert low < 0.5 < high


def test_weights_renormalized_over_present_signals():
    fusion = WeightedEnsembleFusion(combination="linear")
    # only one signal present out of a 0.35/0.40/... weight scheme
    result = fusion.fuse([SignalReading("credibility", 0.7, 0.35)])
    assert result.p_yes == pytest.approx(0.7)
    assert result.signals[0].weight == pytest.approx(1.0)


def test_no_signals_returns_uninformative_prior():
    result = WeightedEnsembleFusion().fuse([])
    assert result.p_yes == 0.5
    assert result.agreement == 0.0


def test_agreement_drops_with_disagreeing_signals():
    fusion = WeightedEnsembleFusion(combination="linear")
    agree = fusion.fuse([SignalReading("a", 0.7, 0.5), SignalReading("b", 0.7, 0.5)])
    disagree = fusion.fuse([SignalReading("a", 0.1, 0.5), SignalReading("b", 0.9, 0.5)])
    assert agree.agreement > disagree.agreement


# ---------------------------------------------------------------------------
# Calibrators
# ---------------------------------------------------------------------------
def test_platt_identity_params_are_noop():
    cal = PlattCalibrator(a=1.0, b=0.0)
    for p in (0.1, 0.5, 0.9):
        assert cal.apply(p) == pytest.approx(p, abs=1e-6)


def test_platt_positive_bias_raises_probability():
    assert PlattCalibrator(a=1.0, b=1.0).apply(0.5) > 0.5


def test_isotonic_interpolates_between_points():
    cal = IsotonicCalibrator([(0.0, 0.0), (0.5, 0.3), (1.0, 1.0)])
    assert cal.apply(0.25) == pytest.approx(0.15)
    assert cal.apply(0.0) == 0.0
    assert cal.apply(1.0) == 1.0


def test_fit_platt_on_well_calibrated_data():
    # outcomes drawn deterministically at the stated probability boundary
    pairs = [(p, p > 0.5) for p in [0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9] * 10]
    a, _b = fit_platt(pairs)
    assert a > 0  # keeps the ordering — higher raw score, higher calibrated p


def test_fit_isotonic_output_is_monotone():
    pairs = [(0.1, False), (0.2, True), (0.3, False), (0.7, True), (0.8, True), (0.9, False)]
    points = fit_isotonic(pairs)
    ys = [y for _, y in points]
    assert ys == sorted(ys)


def test_build_fusion_unknown_strategy_falls_back():
    set_engine_config_override({"fusion": {"strategy": "does-not-exist"}})
    fusion = build_fusion(get_engine_config())
    assert fusion.name == "weighted_v0"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def _job(**overrides) -> EngineJob:
    base = {
        "job_id": "j1",
        "credibility": [{"post_id": "p1", "source": "x", "score": 0.8},
                        {"post_id": "p2", "source": "reddit", "score": 0.4}],
        "metrics": {"predicted": {"predicted_probability": 0.7},
                    "extracted": {"engagement_velocity": 500}},
        "context": {"market_implied_probability": 0.55, "market_slug": "m1"},
    }
    base.update(overrides)
    return EngineJob(**base)


def test_build_signals_full_job():
    set_engine_config_override({"fusion": {"metric_ranges": {"engagement_velocity": [0, 1000]}}})
    signals, reasons = build_signals(_job(), get_engine_config())
    by_name = {s.name: s.value for s in signals}
    assert by_name["credibility"] == pytest.approx(0.6)
    assert by_name["predicted_probability"] == pytest.approx(0.7)
    assert by_name["market_implied"] == pytest.approx(0.55)
    assert by_name["extracted_features"] == pytest.approx(0.5)
    assert reasons == []


def test_build_signals_flags_missing_components():
    signals, reasons = build_signals(
        _job(credibility=[], metrics={"predicted": {}, "extracted": {}}), get_engine_config()
    )
    assert "credibility_unavailable" in reasons
    assert "metrics_unavailable" in reasons
    assert {s.name for s in signals} == {"market_implied"}


def test_extreme_market_price_carries_no_signal():
    assert normalize_market_implied(_job(context={"market_implied_probability": 1.0})) is None
    assert normalize_market_implied(_job(context={"market_implied_probability": 0.0})) is None
    assert normalize_market_implied(_job(context={})) is None


def test_unbounded_metric_without_range_is_skipped():
    set_engine_config_override({"fusion": {"metric_ranges": {}}})
    job = _job(metrics={"predicted": {}, "extracted": {"engagement_velocity": 500, "ratio": 0.25}})
    # 500 has no range and isn't a proportion → skipped; 0.25 is a proportion → kept
    assert normalize_extracted_features(job, get_engine_config()) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_miss_then_hit():
    cache = DedupCache(ttl_seconds=60, redis_url="")
    assert await cache.get("k") is None
    await cache.set("k", {"p": 0.7})
    assert (await cache.get("k")) == {"p": 0.7}
    assert cache.hits == 1 and cache.misses == 1
    assert cache.hit_rate() == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_cache_expiry():
    cache = DedupCache(ttl_seconds=60, redis_url="")
    await cache.set("k", {"p": 1})
    cache._store["k"] = (time.monotonic() - 1, cache._store["k"][1])  # force expiry
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_cache_eviction_bounds_size():
    cache = DedupCache(ttl_seconds=60, max_entries=10, redis_url="")
    for i in range(30):
        await cache.set(f"k{i}", {"i": i})
    assert len(cache._store) <= 11  # eviction keeps it near the budget


# ---------------------------------------------------------------------------
# Tier routing + cost model
# ---------------------------------------------------------------------------
def test_tier_routing_per_job_class_and_default():
    set_engine_config_override({"model_tiers": {
        "interactive": "claude-opus-4-8", "batch": "claude-haiku-4-5", "default": "claude-haiku-4-5",
    }})
    assert resolve_model_tier("interactive") == "claude-opus-4-8"
    assert resolve_model_tier("batch") == "claude-haiku-4-5"
    assert resolve_model_tier("nonsense-class") == "claude-haiku-4-5"


def test_tier_switch_is_config_driven():
    set_engine_config_override({"model_tiers": {"interactive": "claude-haiku-4-5"}})
    assert resolve_model_tier("interactive") == "claude-haiku-4-5"
    set_engine_config_override({"model_tiers": {"interactive": "my-fine-tune-v2"}})
    assert resolve_model_tier("interactive") == "my-fine-tune-v2"  # no redeploy needed


def test_cost_model_applies_cache_and_batch_discounts():
    set_engine_config_override(None)
    full = compute_cost_usd("claude-haiku-4-5", 1000, 100)
    cached = compute_cost_usd("claude-haiku-4-5", 1000, 100, cached_tokens_in=1000)
    batch = compute_cost_usd("claude-haiku-4-5", 1000, 100, batch=True)
    assert full == pytest.approx(1000 * 1e-6 + 100 * 5e-6)
    assert cached < full
    assert batch == pytest.approx(full * 0.5)
    assert compute_cost_usd("unknown-model", 1000, 100) == 0.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def test_metrics_snapshot_rates_and_alert():
    set_engine_config_override({"cost": {"alert_usd_per_1k": 0.001}})
    m = EngineMetrics()
    m.record_job(degraded=True, cache_hit=False, tier="claude-opus-4-8",
                 tokens_in=1000, tokens_out=100, cost_usd=0.01, latency_ms=5.0)
    m.record_job(degraded=False, cache_hit=True, tier="claude-haiku-4-5",
                 tokens_in=0, tokens_out=0, cost_usd=0.0, latency_ms=1.0)
    snap = m.snapshot()
    assert snap["jobs"] == 2
    assert snap["degraded_rate"] == pytest.approx(0.5)
    assert snap["cache_hit_rate"] == pytest.approx(0.5)
    assert snap["tier_mix"] == {"claude-opus-4-8": 1, "claude-haiku-4-5": 1}
    assert snap["cost_alert"] is True
    assert snap["stage_latency"]["total"]["n"] == 2
