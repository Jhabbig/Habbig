"""Per-job-class model tier routing.

The map lives in config.yaml (engine.model_tiers) and is hot-reloaded, so an
operator can move a job class between Haiku / Sonnet / Opus / a fine-tuned
model with an edit + save — no redeploy. The resolved tier is recorded on
every prediction (audit + metrics tier mix) and drives the cost model.
"""
from __future__ import annotations

from app.engine.config import get_engine_config

FALLBACK_MODEL = "claude-haiku-4-5"


def resolve_model_tier(job_class: str) -> str:
    tiers: dict = get_engine_config().get("model_tiers", {}) or {}
    return tiers.get(job_class) or tiers.get("default") or FALLBACK_MODEL
