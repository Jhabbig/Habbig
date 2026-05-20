"""Sensitivity analysis: how stable is each country's rank under methodology
perturbations?

The README promises a sensitivity pass so readers don't over-index on a
single weighting. This module re-runs `compute_subscores` under a fixed
panel of weight perturbations and reports per-country rank ranges. A
country with a tight range is "stably ranked"; one that shuffles across
many positions is flagged "unstable".

Perturbations covered (matches README §Methodology):
  - Each subscore weight perturbed +/- 10 percentage points
  - Each subscore dropped one at a time (leave-one-out)
  - Default weights (anchor for comparison)
"""

from __future__ import annotations

from typing import Callable


def _w(adj: dict[str, float]) -> Callable[[dict[str, float]], dict[str, float]]:
    """Return a transform that applies `adj` to the base weights (clamped at 0)."""
    def t(base: dict[str, float]) -> dict[str, float]:
        return {k: max(0.0, base[k] + adj.get(k, 0.0)) for k in base}
    return t


def _drop(sub: str) -> Callable[[dict[str, float]], dict[str, float]]:
    def t(base: dict[str, float]) -> dict[str, float]:
        return {k: (0.0 if k == sub else base[k]) for k in base}
    return t


PERTURBATIONS: list[tuple[str, str, Callable[[dict[str, float]], dict[str, float]]]] = [
    ("baseline",  "Default weights",         lambda w: dict(w)),
    ("conn+10",   "Connection  +10pp",       _w({"connection":  +0.10})),
    ("conn-10",   "Connection  −10pp",       _w({"connection":  -0.10})),
    ("part+10",   "Partnership +10pp",       _w({"partnership": +0.10})),
    ("part-10",   "Partnership −10pp",       _w({"partnership": -0.10})),
    ("stab+10",   "Stability   +10pp",       _w({"stability":   +0.10})),
    ("stab-10",   "Stability   −10pp",       _w({"stability":   -0.10})),
    ("act+10",    "Activity    +10pp",       _w({"activity":    +0.10})),
    ("act-10",    "Activity    −10pp",       _w({"activity":    -0.10})),
    ("drop_conn", "Without Connection",      _drop("connection")),
    ("drop_part", "Without Partnership",     _drop("partnership")),
    ("drop_stab", "Without Stability",       _drop("stability")),
    ("drop_act",  "Without Activity",        _drop("activity")),
]

# rank range thresholds (positions on the global ranked list)
STABLE_RANGE_HIGH = 3
STABLE_RANGE_MEDIUM = 10


def stability_label(rank_range: int) -> str:
    if rank_range <= STABLE_RANGE_HIGH:
        return "high"
    if rank_range <= STABLE_RANGE_MEDIUM:
        return "medium"
    return "low"


def compute_sensitivity(
    compute_subscores: Callable[[dict[str, float] | None], dict[str, dict]],
    base_weights: dict[str, float],
) -> dict:
    """Return per-country rank ranges + stability labels across all perturbations.

    compute_subscores must accept a weights dict (already absolute, will be
    renormalized inside) and return {iso3: country_record} with a `composite`
    key. Countries with composite=None are ignored in ranking.
    """
    perturbations_meta: list[dict] = []
    per_iso_ranks: dict[str, dict[str, int]] = {}
    per_iso_meta: dict[str, dict] = {}

    for p_id, label, transform in PERTURBATIONS:
        weights = transform(base_weights)
        countries = compute_subscores(weights)
        ranked = sorted(
            (c for c in countries.values() if c.get("composite") is not None),
            key=lambda c: c["composite"],
            reverse=True,
        )
        perturbations_meta.append({"id": p_id, "label": label, "n_ranked": len(ranked)})
        for rank, c in enumerate(ranked, start=1):
            per_iso_ranks.setdefault(c["iso3"], {})[p_id] = rank
            per_iso_meta.setdefault(c["iso3"], {
                "iso3": c["iso3"],
                "name": c.get("name"),
                "iso2": c.get("iso2"),
                "income_tier": c.get("income_tier"),
            })

    countries_out: dict[str, dict] = {}
    for iso3, ranks in per_iso_ranks.items():
        rs = list(ranks.values())
        rng = max(rs) - min(rs)
        countries_out[iso3] = {
            **per_iso_meta[iso3],
            "ranks": ranks,
            "rank_min": min(rs),
            "rank_max": max(rs),
            "rank_baseline": ranks.get("baseline"),
            "rank_range": rng,
            "stability": stability_label(rng),
        }

    by_range = sorted(countries_out.values(), key=lambda s: s["rank_range"], reverse=True)
    most_unstable = [s for s in by_range if s["stability"] == "low"][:10]
    most_stable = [s for s in by_range if s["stability"] == "high" and s.get("rank_baseline") is not None][-10:]

    distribution = {
        "high":   sum(1 for s in countries_out.values() if s["stability"] == "high"),
        "medium": sum(1 for s in countries_out.values() if s["stability"] == "medium"),
        "low":    sum(1 for s in countries_out.values() if s["stability"] == "low"),
    }

    return {
        "perturbations":          perturbations_meta,
        "countries":              countries_out,
        "most_unstable":          most_unstable,
        "most_stable":            most_stable,
        "stability_distribution": distribution,
        "thresholds": {
            "high_max_range":   STABLE_RANGE_HIGH,
            "medium_max_range": STABLE_RANGE_MEDIUM,
        },
    }
