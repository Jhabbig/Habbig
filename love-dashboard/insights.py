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
TRIPLE_THRESHOLD = 90.0        # all 3 Tier-A/B subscores >= 90 -> "triple threat"
WEAKNESS_COMPOSITE = 75.0      # composite >= 75 (top quartile)
WEAKNESS_SUBSCORE = 20.0       # any subscore <= 20 (bottom quintile)
CAP_IMPACT_DELTA = 2.0         # partnership cap reduced score by >= 2 pct points
CLOSEST_PEER_DIST = 12.0       # max Euclidean distance to count as "lookalike"
MOVER_MIN_DAYS = 30            # earliest comparison snapshot >= N days old
MOVER_MIN_DELTA = 5.0          # composite must have shifted >= 5 pct pts
MAX_INSIGHTS_PER_RULE = 4
MAX_TOTAL_INSIGHTS = 16


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
            sigma = pstdev(vals)
            if sigma == 0:
                # No spread within tier on this subscore — every "outlier" would
                # be the same value as the mean; report nothing rather than emit
                # meaningless z-scores divided by a 1.0 fallback.
                continue
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


def rule_triple_threat(countries: list[dict]) -> list[Insight]:
    """Top decile on all three Tier-A/B subscores within the country's tier."""
    qualifiers: list[tuple[dict, dict, float]] = []
    for c in countries:
        subs = c.get("subscores") or {}
        vals = {k: subs.get(k) for k in ("connection", "partnership", "stability")}
        if any(v is None or v < TRIPLE_THRESHOLD for v in vals.values()):
            continue
        qualifiers.append((c, vals, min(vals.values())))
    qualifiers.sort(key=lambda t: t[2], reverse=True)
    out: list[Insight] = []
    for c, vals, floor in qualifiers[:MAX_INSIGHTS_PER_RULE]:
        out.append(Insight(
            kind="triple_threat",
            severity="info",
            iso3=c["iso3"],
            country=c["name"],
            title=f"{c['name']} hits the top decile across the board",
            body=(
                f"Connection {vals['connection']:.0f}, Partnership "
                f"{vals['partnership']:.0f}, Stability {vals['stability']:.0f} — all in "
                f"the top 10% of their income tier. Floor of {floor:.0f} on the "
                f"weakest subscore."
            ),
            confidence=_confidence_for(c.get("used") or []),
            pointers=[
                {"label": "connection",  "value": round(vals["connection"], 1)},
                {"label": "partnership", "value": round(vals["partnership"], 1)},
                {"label": "stability",   "value": round(vals["stability"], 1)},
            ],
        ))
    return out


def rule_weakness_flag(countries: list[dict]) -> list[Insight]:
    """Strong composite hiding one bottom-quintile subscore — the 'one big
    asterisk' story."""
    candidates: list[tuple[dict, list[tuple[str, float]]]] = []
    for c in countries:
        comp = c.get("composite")
        if comp is None or comp < WEAKNESS_COMPOSITE:
            continue
        weak: list[tuple[str, float]] = []
        for k in ("connection", "partnership", "stability"):
            v = (c.get("subscores") or {}).get(k)
            if v is not None and v <= WEAKNESS_SUBSCORE:
                weak.append((k, v))
        if weak:
            candidates.append((c, weak))
    candidates.sort(key=lambda t: -t[0]["composite"])
    out: list[Insight] = []
    for c, weak in candidates[:MAX_INSIGHTS_PER_RULE]:
        weak.sort(key=lambda kv: kv[1])  # weakest first
        worst_k, worst_v = weak[0]
        out.append(Insight(
            kind="weakness_flag",
            severity="warn",
            iso3=c["iso3"],
            country=c["name"],
            title=f"{c['name']}: strong overall, weak on {worst_k.title()}",
            body=(
                f"Composite {c['composite']:.1f} sits in the top quartile, but the "
                f"{worst_k} subscore is {worst_v:.0f} — bottom quintile within the "
                f"income tier. Read the headline with that asterisk."
            ),
            confidence=_confidence_for(c.get("used") or []),
            pointers=[
                {"label": "composite", "value": round(c["composite"], 1)},
                {"label": worst_k,     "value": round(worst_v, 1)},
            ],
        ))
    return out


def rule_cap_impact(countries: list[dict], partnership_uncapped: dict[str, float]) -> list[Insight]:
    """Countries whose Partnership was meaningfully reduced by the 80th-pctile
    cap. Makes the methodology audible: 'this score is held back on purpose'."""
    candidates: list[tuple[dict, float, float, float]] = []
    for c in countries:
        p_capped = (c.get("subscores") or {}).get("partnership")
        p_unc = partnership_uncapped.get(c["iso3"])
        if p_capped is None or p_unc is None:
            continue
        delta = p_unc - p_capped
        if delta < CAP_IMPACT_DELTA:
            continue
        candidates.append((c, p_capped, p_unc, delta))
    candidates.sort(key=lambda t: -t[3])
    out: list[Insight] = []
    for c, p_capped, p_unc, delta in candidates[:MAX_INSIGHTS_PER_RULE]:
        out.append(Insight(
            kind="cap_impact",
            severity="info",
            iso3=c["iso3"],
            country=c["name"],
            title=f"{c['name']}: Partnership held back by the 80% cap",
            body=(
                f"Marriage rate sits in the very top of the {c.get('income_tier','?')}-income "
                f"tier. The methodology caps Partnership at the 80th percentile (so "
                f"coercion-driven highs aren't rewarded), trimming it from "
                f"{p_unc:.0f} to {p_capped:.0f} — a {delta:.0f}-point haircut visible "
                f"on the index but invisible on the front cards."
            ),
            confidence=_confidence_for(c.get("used") or []),
            pointers=[
                {"label": "uncapped", "value": round(p_unc, 1)},
                {"label": "capped",   "value": round(p_capped, 1)},
                {"label": "haircut",  "value": round(delta, 1)},
            ],
        ))
    return out


def rule_closest_peer(countries: list[dict]) -> list[Insight]:
    """Surprising twins: pairs of countries with near-identical subscore
    profiles despite different income tiers or regions."""
    triples: list[tuple[dict, tuple[float, float, float]]] = []
    for c in countries:
        if c.get("composite") is None:
            continue
        s = c.get("subscores") or {}
        if all(s.get(k) is not None for k in ("connection", "partnership", "stability")):
            triples.append((c, (s["connection"], s["partnership"], s["stability"])))

    pairs: list[tuple[float, dict, dict]] = []
    for i, (a, va) in enumerate(triples):
        for b, vb in triples[i + 1:]:
            # Only surprising pairs: different income tier OR different region.
            if a.get("income_tier") == b.get("income_tier") and a.get("region") == b.get("region"):
                continue
            d = sum((va[k] - vb[k]) ** 2 for k in range(3)) ** 0.5
            if d <= CLOSEST_PEER_DIST:
                pairs.append((d, a, b))
    pairs.sort(key=lambda t: t[0])

    out: list[Insight] = []
    used_isos: set[str] = set()
    for d, a, b in pairs:
        if a["iso3"] in used_isos or b["iso3"] in used_isos:
            continue  # don't burn one country across multiple lookalike cards
        used_isos.update((a["iso3"], b["iso3"]))
        out.append(Insight(
            kind="closest_peer",
            severity="info",
            iso3=a["iso3"],
            country=a["name"],
            title=f"{a['name']} looks like {b['name']}",
            body=(
                f"Across Connection, Partnership and Stability, {a['name']} "
                f"({a.get('income_tier','?')} income) and {b['name']} "
                f"({b.get('income_tier','?')} income) sit within {d:.1f} subscore-points "
                f"of each other. Different tier or region; similar relational outcomes."
            ),
            confidence=_confidence_for(a.get("used") or []),
            pointers=[
                {"label": a["name"],  "value": f"({va[0]:.0f}, {va[1]:.0f}, {va[2]:.0f})"},
                {"label": b["name"],  "value": f"({vb[0]:.0f}, {vb[1]:.0f}, {vb[2]:.0f})"},
                {"label": "distance", "value": round(d, 1)},
            ],
        ))
        if len(out) >= MAX_INSIGHTS_PER_RULE:
            break
    return out


def rule_mover(countries: list[dict], history_accessor) -> list[Insight]:
    """Biggest composite shift vs the oldest snapshot at least MOVER_MIN_DAYS ago.

    Quietly returns [] until the snapshot store has enough history; the
    server bootstraps the store the first time data flows through, so the
    rule lights up automatically once a few days have accumulated.
    """
    from datetime import date as _date

    today = _date.today()
    candidates: list[tuple[dict, float, float, int]] = []
    for c in countries:
        comp = c.get("composite")
        if comp is None:
            continue
        history = history_accessor(c["iso3"])
        if not history:
            continue
        # find the earliest point that's at least MOVER_MIN_DAYS old
        baseline = None
        for pt in history:
            try:
                pt_date = _date.fromisoformat(pt["date"])
            except (KeyError, TypeError, ValueError):
                continue
            if pt.get("composite") is None:
                continue
            if (today - pt_date).days >= MOVER_MIN_DAYS:
                baseline = pt
                break
        if baseline is None:
            continue
        delta = comp - baseline["composite"]
        if abs(delta) < MOVER_MIN_DELTA:
            continue
        days_ago = (today - _date.fromisoformat(baseline["date"])).days
        candidates.append((c, comp, baseline["composite"], delta, days_ago))
    candidates.sort(key=lambda t: -abs(t[3]))

    out: list[Insight] = []
    for c, now, before, delta, days_ago in candidates[:MAX_INSIGHTS_PER_RULE]:
        direction = "↑" if delta > 0 else "↓"
        out.append(Insight(
            kind="mover",
            severity="info" if abs(delta) < 10 else "warn",
            iso3=c["iso3"],
            country=c["name"],
            title=f"{c['name']} {direction} {abs(delta):.1f} pts in {days_ago} days",
            body=(
                f"Composite moved from {before:.1f} to {now:.1f} since "
                f"{days_ago} days ago — a {delta:+.1f} pt shift. Driven by the "
                f"subscore changes visible in the country drill-down."
            ),
            confidence=_confidence_for(c.get("used") or []),
            pointers=[
                {"label": "today",       "value": round(now, 1)},
                {"label": f"−{days_ago}d","value": round(before, 1)},
                {"label": "Δ",            "value": round(delta, 1)},
            ],
        ))
    return out


def generate_insights(
    countries: list[dict],
    meta: dict[str, dict],
    *,
    partnership_uncapped: dict[str, float] | None = None,
    history_accessor=None,
) -> list[dict]:
    pool: list[Insight] = []
    pool.extend(rule_peer_leader(countries))
    pool.extend(rule_outlier(countries))
    pool.extend(rule_divergence(countries))
    pool.extend(rule_triple_threat(countries))
    pool.extend(rule_weakness_flag(countries))
    pool.extend(rule_closest_peer(countries))
    pool.extend(rule_coverage_gap(countries, meta))
    if partnership_uncapped is not None:
        pool.extend(rule_cap_impact(countries, partnership_uncapped))
    if history_accessor is not None:
        pool.extend(rule_mover(countries, history_accessor))

    severity_rank = {"alert": 0, "warn": 1, "info": 2}
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    pool.sort(key=lambda i: (severity_rank.get(i.severity, 3),
                              confidence_rank.get(i.confidence, 3)))
    return [i.to_dict() for i in pool[:MAX_TOTAL_INSIGHTS]]
