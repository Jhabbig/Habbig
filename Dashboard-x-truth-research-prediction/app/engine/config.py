"""Hot-reloading engine configuration.

The `engine:` section of app/config.yaml is re-read whenever the file's mtime
changes, so operators can flip model tiers, fusion weights, cache TTLs or the
batch switch with an edit + save — no redeploy. Missing keys fall back to the
DEFAULTS tree, so a partial (or absent) yaml section is always safe.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

DEFAULTS: dict = {
    "fusion": {
        "strategy": "weighted_v0",
        "combination": "logistic",
        "logistic_scale": 4.0,
        "logistic_bias": 0.0,
        "weights": {
            "credibility": 0.35,
            "predicted_probability": 0.40,
            "market_implied": 0.15,
            "extracted_features": 0.10,
        },
        "predicted_probability_key": "predicted_probability",
        "metric_ranges": {},
        "calibration": {"method": "none", "platt": {"a": 1.0, "b": 0.0}, "isotonic_points": []},
    },
    "degradation": {
        "confidence_floor": 0.05,
        "missing_credibility_penalty": 0.25,
        "missing_metrics_penalty": 0.35,
    },
    "cache": {"ttl_seconds": 900, "max_entries": 50000},
    "model_tiers": {
        "interactive": "claude-opus-4-8",
        "batch": "claude-haiku-4-5",
        "replay": "claude-haiku-4-5",
        "default": "claude-haiku-4-5",
    },
    "batch": {"enabled": False, "max_batch_size": 500, "flush_interval_seconds": 300},
    "cost": {
        "alert_usd_per_1k": 20.0,
        "batch_discount": 0.5,
        "cached_input_discount": 0.1,
        "prices_per_mtok": {
            "claude-opus-4-8": {"input": 5.0, "output": 25.0},
            "claude-opus-4-7": {"input": 5.0, "output": 25.0},
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
            "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
        },
    },
    "model_versions": {"credibility": "credibility-v1"},
}

_cached: Optional[dict] = None
_cached_mtime: Optional[float] = None
_override: Optional[dict] = None


def _deep_merge(base: dict, extra: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_engine_config() -> dict:
    """Return the merged engine config, re-reading config.yaml if it changed."""
    global _cached, _cached_mtime
    if _override is not None:
        return _override
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        return copy.deepcopy(DEFAULTS)
    if _cached is None or mtime != _cached_mtime:
        try:
            with open(_CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
            _cached = _deep_merge(DEFAULTS, raw.get("engine", {}) or {})
            _cached_mtime = mtime
            logger.info("Engine config (re)loaded from %s", _CONFIG_PATH)
        except Exception as exc:
            logger.error("Engine config reload failed (%s); keeping previous", exc)
            if _cached is None:
                _cached = copy.deepcopy(DEFAULTS)
    return _cached


def set_engine_config_override(cfg: Optional[dict]) -> None:
    """Test hook — pin the engine config (merged over DEFAULTS). Pass None to clear."""
    global _override
    _override = _deep_merge(DEFAULTS, cfg) if cfg is not None else None
