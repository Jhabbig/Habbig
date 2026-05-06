"""Insight rules engine for the State of Love dashboard.

Each rule scans the latest snapshot of country subscores and emits Insight
records. Rules are deliberately small and independent so we can add or tune
them without touching `server.py`.

Rules implemented in v2:
  - Outlier:      country sits > OUTLIER_THRESHOLD pct points above its income
                  tier mean on any subscore.
  - Divergence:   Partnership and Stability subscores diverge by more than
                  DIVERGENCE_THRESHOLD pct points within the same country.
  - CoverageGap:  high-income country missing a Tier-A subscore (data quality
                  flag the dashboard should report transparently).
  - PeerLeader:   country tops its income tier on the composite Love Index.

YoY "Mover" rules are scaffolded but disabled until we wire historical
snapshots — we don't fabricate change rates from a single snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from statistics import mean, pstdev
from typing import Any

OUTLIER_THRESHOLD = 20.0       # pct points above tier mean
DIVERGENCE_THRESHOLD = 25.0    # pct point gap between Partnership and Stability
MAX_INSIGHTS_PER_RULE = 4
MAX_TOTAL_INSIGHTS = 12


@dataclass
class Insight:
    kind: str            # "outlier" | "divergence" | "coverage_gap" | "peer_leader"
    severity: str        # "info" | "warn"
    iso3: str
    country: str
    title: str
    body: str
    confidence: str      # "high" | "medium" | "low"
    pointers: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _confidence_for(used: Iterable[str]) -> str:
    used = list(used)
    tier_ab = {"connection", "partnership", "stability"}
    n_ab = sum(1 for k in used if k in tier_ab)
    if n_ab >= 3:
        return "high"
    if n_ab == 2:
        return "medium"
    return "low"


def rule_peer_leader(countries: list[dict]) -> list[Insight]:
    by_tier: dict[str, list[dict]] = {}
    for c in countries:
        if c.get("composite") is None or not c.get("income_tier"):
            continue
        by_tier.setdefault(c["income_tier"], []).append(c)
    out = []
    for tier, rows in by_tier.items():
        if len(rows) < 4:
            continue
        rows.sort(key=lambda c: c["composite"], reverse=True)
        leader = rows[0]
        runner = rows[1]
        margin = leader["composite"] - runner["composite"]
        if margin < 3.0:
            continue
        out.append(Insight(
            kind="peer_leader",
            severity="info",
            iso3=leader["iso3"],
            country=leader["name"],
            title=f"{leader['name']} leads {tier} income tier",
            body=(
                f"Composite Love Index {leader['composite']:.1f} — "
                f"{margin:.1f} pts ahead of {runner['name']} ({runner['composite']:.1f}). "
                f"Index averages percentile rank within the {tier}-income peer group."
            ),
            confidence=_confidence_for(leader.get("used") or []),
            pointers=[
                {"label": "leader composite", "value": leader["composite"]},
                {"label": f"{runner['name']} composite", "value": runner["composite"]},
            ],
        ))
    return out[:MAX_INSIGHTS_PER_RULE]


def rule_outlier(countries: list[dict]) -> list[Insight]:
    by_tier: dict[str, list[dict]] = {}
    for c in countries:
        if not c.get("income_tier"):
            continue
        by_tier.setdefault(c["income_tier"], []).append(c)

    insights: list[Insight] = []
    for tier, rows in by_tier.items():
        if len(rows) < 4:
            continue
        for sub in ("connection", "partnership", "stability"):
            vals = [c["subscores"].get(sub) for c in rows if c["subscores"].get(sub) is not None]
            if len(vals) < 4:
                continue
            mu = mean(vals)
            sigma = pstdev(vals) or 1.0
            for c in rows:
                v = c["subscores"].get(sub)
                if v is None:
                    continue
                diff = v - mu
                if diff > OUTLIER_THRESHOLD:
                    insights.append(Insight(
                        kind="outlier",
                        severity="info",
                        iso3=c["iso3"],
                        country=c["name"],
                        title=f"{c['name']} stands out on {sub.title()}",
                        body=(
                            f"{sub.title()} subscore {v:.1f} sits {diff:+.1f} pts above the "
                            f"{tier}-income tier mean ({mu:.1f}, σ={sigma:.1f}). "
                            f"Z-score within tier: {(v - mu)/sigma:+.2f}."
                        ),
                        confidence=_confidence_for(c.get("used") or []),
                        pointers=[
                            {"label": f"{c['name']} {sub}", "value": round(v, 1)},
                            {"label": f"{tier} tier mean", "value": round(mu, 1)},
                            {"label": "z-score", "value": round((v - mu) / sigma, 2)},
                        ],
                    ))
    insights.sort(key=lambda i: -abs(i.pointers[2]["value"]) if len(i.pointers) >= 3 else 0)
    return insights[:MAX_INSIGHTS_PER_RULE]


def rule_divergence(countries: list[dict]) -> list[Insight]:
    insights: list[Insight] = []
    for c in countries:
        p = c["subscores"].get("partnership")
        s = c["subscores"].get("stability")
        if p is None or s is None:
            continue
        gap = abs(p - s)
        if gap < DIVERGENCE_THRESHOLD:
            continue
        if p > s:
            narrative = (
                f"High partnership prevalence ({p:.1f}) but weaker stability ({s:.1f}). "
                f"Many people pair up, fewer unions endure."
            )
        else:
            narrative = (
                f"Strong stability ({s:.1f}) but low partnership prevalence ({p:.1f}). "
                f"Unions that form tend to last; fewer people enter them in the first place."
            )
        insights.append(Insight(
            kind="divergence",
            severity="warn",
            iso3=c["iso3"],
            country=c["name"],
            title=f"{c['name']}: Partnership × Stability gap of {gap:.1f}",
            body=narrative,
            confidence=_confidence_for(c.get("used") or []),
            pointers=[
                {"label": "partnership", "value": round(p, 1)},
                {"label": "stability",   "value": round(s, 1)},
                {"label": "gap",         "value": round(gap, 1)},
            ],
        ))
    insights.sort(key=lambda i: -i.pointers[2]["value"])
    return insights[:MAX_INSIGHTS_PER_RULE]


def rule_coverage_gap(countries: list[dict], meta: dict[str, dict]) -> list[Insight]:
    """High-income countries that are unranked or missing Tier-A subscores."""
    ranked = {c["iso3"] for c in countries}
    out: list[Insight] = []
    for iso3, c in meta.items():
        if c.get("income_tier") != "H":
            continue
        if iso3 in ranked:
            row = next((r for r in countries if r["iso3"] == iso3), None)
            if not row:
                continue
            missing = [k for k in ("connection", "partnership", "stability")
                       if row["subscores"].get(k) is None]
            if not missing:
                continue
            out.append(Insight(
                kind="coverage_gap",
                severity="warn",
                iso3=iso3,
                country=c.get("name", iso3),
                title=f"{c.get('name', iso3)}: data gap on {', '.join(missing)}",
                body=(
                    f"Ranked but missing {len(missing)} of 3 Tier-A/B subscores. "
                    f"Composite uses renormalized weights; treat with caution."
                ),
                confidence="low",
                pointers=[{"label": "missing", "value": ", ".join(missing)}],
            ))
        else:
            out.append(Insight(
                kind="coverage_gap",
                severity="warn",
                iso3=iso3,
                country=c.get("name", iso3),
                title=f"{c.get('name', iso3)}: insufficient data to rank",
                body="High-income country with fewer than 2 Tier-A/B subscores present. "
                     "Backend will surface a real index once additional fetchers land.",
                confidence="low",
            ))
    return out[:MAX_INSIGHTS_PER_RULE]


def generate_insights(countries: list[dict], meta: dict[str, dict]) -> list[dict]:
    pool: list[Insight] = []
    pool.extend(rule_peer_leader(countries))
    pool.extend(rule_outlier(countries))
    pool.extend(rule_divergence(countries))
    pool.extend(rule_coverage_gap(countries, meta))

    severity_rank = {"alert": 0, "warn": 1, "info": 2}
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    pool.sort(key=lambda i: (severity_rank.get(i.severity, 3),
                              confidence_rank.get(i.confidence, 3)))
    return [i.to_dict() for i in pool[:MAX_TOTAL_INSIGHTS]]
