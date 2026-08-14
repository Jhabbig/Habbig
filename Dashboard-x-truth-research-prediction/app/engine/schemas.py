"""Request/response contracts for the Prediction Engine.

These are the two public interfaces from the Stage 3 spec. Input arrives from
an internal queue or the /api/v1/engine/predict endpoint; output is returned to
the caller and logged (in full, with inputs) to the fusion_audit table.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class CredibilityItem(BaseModel):
    """One scored social post from Component 1 (credibility ranker)."""
    post_id: str = ""
    source: str = ""  # "x" | "reddit" | ...
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    features: Dict[str, Any] = Field(default_factory=dict)


class MetricsPayload(BaseModel):
    """Component 2 output: LLM-predicted metrics + extracted features."""
    model_config = ConfigDict(protected_namespaces=())

    predicted: Dict[str, Any] = Field(default_factory=dict)
    extracted: Dict[str, Any] = Field(default_factory=dict)
    model_version: str = ""
    # Optional upstream token accounting ({"input_tokens", "output_tokens",
    # "cache_read_input_tokens"}) — flows into the cost model + audit trail.
    usage: Dict[str, int] = Field(default_factory=dict)


class EngineJob(BaseModel):
    """Fusion request. `context` carries domain fields — for this deployment:
    market_slug, market_implied_probability, category."""
    job_id: str
    user_id: str = ""
    job_class: str = "interactive"  # selects the model tier (config-driven)
    credibility: List[CredibilityItem] = Field(default_factory=list)
    metrics: MetricsPayload = Field(default_factory=MetricsPayload)
    context: Dict[str, Any] = Field(default_factory=dict)

    def content_hash(self) -> str:
        """Dedup key: canonical hash of the *content* (not job_id/user_id), so
        many concurrent users asking about the same posts share one result."""
        payload = json.dumps(
            {
                "credibility": [c.model_dump() for c in self.credibility],
                "metrics": self.metrics.model_dump(exclude={"usage"}),
                "context": self.context,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ContributingSignal(BaseModel):
    signal: str
    weight: float  # normalized over the signals present
    value: float   # the normalized [0,1] reading that entered the fusion


class PredictionPayload(BaseModel):
    """Typed output for this deployment: P(YES) of the market outcome."""
    p_yes: float
    side: str  # "YES" | "NO" — the side the probability favors


class EnginePrediction(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    job_id: str
    prediction: PredictionPayload
    confidence: float
    contributing_signals: List[ContributingSignal] = Field(default_factory=list)
    model_versions: Dict[str, str] = Field(default_factory=dict)
    degraded: bool = False
    degraded_reasons: List[str] = Field(default_factory=list)
    cache_hit: bool = False
    model_tier: str = ""
    latency_ms: float = 0.0
