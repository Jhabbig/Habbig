"""narve.ai house forecast.

Produces a single per-race probability by ensembling the individual sources
(Polymarket, Kalshi, PredictIt, polling, Manifold, Metaculus). The ensemble
weight for each source is the inverse of its observed Brier score on resolved
historical races — sources that have been better-calibrated count more.

Cold-start: until we have ``MIN_RESOLVED_FOR_BRIER`` resolved races per
source, we use static prior weights derived from public-aggregate research
(real-money markets > play-money / forecasting platforms > polls).

Output schema returned by ``forecast_for_race``::

    {
      "race_key": "senate_TX",
      "forecast_d": 0.42,
      "confidence": 0.78,                    # 0-1, based on agreement + coverage
      "sources_used": ["polymarket", "kalshi", "manifold"],
      "source_probs": {"polymarket": 0.41, "kalshi": 0.43, "manifold": 0.40},
      "weights":      {"polymarket": 0.42, "kalshi": 0.42, "manifold": 0.16},
      "spread":   0.03,
      "n_sources": 3,
      "method":   "brier_weighted" | "default_weights",
    }
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Cold-start prior weights. Real-money markets get the highest weight (skin
# in the game tends to discipline pricing); play-money markets / forecasting
# platforms get a slightly lower weight; polls are anchored but noisy. These
# are starting values — once we have enough resolved races per source the
# Brier-derived weights take over completely.
DEFAULT_WEIGHTS: dict[str, float] = {
    "polymarket": 1.0,
    "kalshi":     1.0,
    "manifold":   0.7,
    "metaculus":  0.7,
    "polling":    0.5,
    "predictit":  0.3,
}

# How many resolved races per source we need before trusting the Brier
# weight for that source. Below the cutoff we keep the default weight.
MIN_RESOLVED_FOR_BRIER = 5

# Real-world Brier scores for prediction-market sources cluster in the
# 0.05-0.20 range. Inverse weights are 5-20. Cap above that range so a
# single anomalously-small Brier (e.g. one lucky resolved race) can't fully
# dominate the ensemble, but don't cap so tightly that all sources collapse
# to the same normalized weight.
MAX_WEIGHT = 50.0
MIN_WEIGHT = 0.05


def derive_weights(brier: dict[str, float], coverage: dict[str, dict]) -> dict[str, float]:
    """Convert per-source Brier scores into ensemble weights.

    Sources without enough resolved-race coverage fall back to ``DEFAULT_WEIGHTS``.
    Brier scores are inverted (lower Brier → higher weight) and capped.
    """
    out: dict[str, float] = {}
    for src, default in DEFAULT_WEIGHTS.items():
        b = brier.get(src) if brier else None
        cov = coverage.get(src, {}) if coverage else {}
        resolved = cov.get("resolved_races", 0) if isinstance(cov, dict) else 0
        if b is None or resolved < MIN_RESOLVED_FOR_BRIER:
            out[src] = default
            continue
        # Brier is in [0, 1]; 0 is perfect, 0.25 is the random-binary baseline.
        # Inverse-Brier weighting with a tiny epsilon to avoid divide-by-zero.
        w = 1.0 / max(float(b), 0.01)
        out[src] = max(MIN_WEIGHT, min(MAX_WEIGHT, w))
    return out


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in weights.items()}


def _confidence(probs: list[float], used_weight: float, total_default_weight: float) -> float:
    """Heuristic confidence in [0, 1].

    Combines two signals:
      * **Coverage**: fraction of available source-weight that produced a
        probability. A race covered only by Manifold gets a low ceiling.
      * **Agreement**: 1 − spread. When sources cluster tightly we are more
        confident than when they disagree.

    Both are bounded to [0, 1] and multiplied so missing-coverage *or* large
    disagreement pulls confidence down.
    """
    if not probs:
        return 0.0
    coverage = max(0.0, min(1.0, used_weight / total_default_weight if total_default_weight else 0.0))
    spread = max(probs) - min(probs)
    agreement = max(0.0, 1.0 - spread * 2.0)  # spread of 0.5 → agreement 0
    return round(coverage * agreement, 3)


def forecast_for_race(
    *,
    race_key: str,
    source_probs: dict[str, float],
    brier: Optional[dict[str, float]] = None,
    coverage: Optional[dict[str, dict]] = None,
) -> dict:
    """Produce the narve.ai forecast for a single race.

    Args:
      race_key: Canonical race key (used for output only).
      source_probs: ``{source: P(D wins)}`` from the latest divergence snapshot.
        Keys outside ``DEFAULT_WEIGHTS`` are ignored.
      brier: Per-source mean Brier from ``/data/backtest``. Optional.
      coverage: Per-source resolved-race counts from the same backtest endpoint.

    Returns the schema documented at the top of this module.
    """
    weights_full = derive_weights(brier or {}, coverage or {})
    method = "brier_weighted" if (brier and any(
        coverage.get(s, {}).get("resolved_races", 0) >= MIN_RESOLVED_FOR_BRIER
        for s in DEFAULT_WEIGHTS
        if isinstance(coverage, dict)
    )) else "default_weights"

    # Filter to sources that actually reported a probability.
    used_weights: dict[str, float] = {}
    used_probs: dict[str, float] = {}
    for src, w in weights_full.items():
        p = source_probs.get(src)
        if p is None:
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0):
            continue
        used_weights[src] = w
        used_probs[src] = p

    if not used_probs:
        return {
            "race_key": race_key,
            "forecast_d": None,
            "confidence": 0.0,
            "sources_used": [],
            "source_probs": {},
            "weights": {},
            "spread": None,
            "n_sources": 0,
            "method": method,
        }

    normalized = _normalize(used_weights)
    forecast = sum(normalized[s] * used_probs[s] for s in used_probs)
    forecast = max(0.0, min(1.0, forecast))

    spread = max(used_probs.values()) - min(used_probs.values())
    confidence = _confidence(
        list(used_probs.values()),
        used_weight=sum(used_weights.values()),
        total_default_weight=sum(weights_full.values()),
    )

    return {
        "race_key": race_key,
        "forecast_d": round(forecast, 4),
        "confidence": confidence,
        "sources_used": sorted(used_probs.keys()),
        "source_probs": {s: round(p, 4) for s, p in used_probs.items()},
        "weights": {s: round(w, 4) for s, w in normalized.items()},
        "spread": round(spread, 4),
        "n_sources": len(used_probs),
        "method": method,
    }


def forecast_many(
    snapshots: Iterable[dict],
    *,
    brier: Optional[dict[str, float]] = None,
    coverage: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """Produce forecasts for a batch of latest-divergence snapshots.

    Each snapshot is the row shape returned by ``Database.get_latest_divergence``
    / ``get_latest_divergence_per_race`` — i.e. has ``race_key``, ``state``,
    ``race_type``, the major-source columns, and a parsed ``divergence_details``
    dict containing secondary sources (manifold, metaculus).
    """
    out: list[dict] = []
    for snap in snapshots:
        rk = snap.get("race_key")
        if not rk:
            continue
        details = snap.get("divergence_details") or {}
        if isinstance(details, str):
            try:
                import json
                details = json.loads(details)
            except Exception:
                details = {}

        probs: dict[str, float] = {}
        col_map = {
            "polymarket": "polymarket_prob",
            "kalshi": "kalshi_prob",
            "predictit": "predictit_prob",
            "polling": "polling_avg",
        }
        for src, col in col_map.items():
            v = snap.get(col)
            if v is not None:
                try:
                    probs[src] = float(v)
                except (TypeError, ValueError):
                    pass
        for src in ("manifold", "metaculus"):
            v = details.get(src) if isinstance(details, dict) else None
            if v is not None:
                try:
                    probs[src] = float(v)
                except (TypeError, ValueError):
                    pass

        f = forecast_for_race(
            race_key=rk,
            source_probs=probs,
            brier=brier,
            coverage=coverage,
        )
        # Pass through a couple of useful fields so the summary endpoint
        # doesn't have to re-join.
        f["race_type"] = snap.get("race_type")
        f["state"] = snap.get("state")
        f["snapshot_time"] = snap.get("snapshot_time")
        out.append(f)
    return out
