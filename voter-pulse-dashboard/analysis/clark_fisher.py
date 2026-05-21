"""Clark-Fisher stage classification + ternary-plot coordinates.

Colin Clark (1940) and Allan Fisher (1935) proposed that an economy
develops through three sector-dominance stages:

  Stage 1 - Pre-industrial   : agriculture dominates employment
  Stage 2 - Industrial       : manufacturing / construction dominate
  Stage 3 - Post-industrial  : services dominate

Modern extensions add a fourth and fifth stage (information / knowledge
economy, and the "quinary" or personal-services tier). We surface a
five-bucket classification on top of the three classical stages so
post-industrial economies don't all collapse into one bucket.

A country's position on the development arc is also returned as
ternary-plot Cartesian coordinates so the front-end can render the
familiar Clark-Fisher triangle without doing the trig itself.

Why these specific thresholds: the cutoffs below are loosely calibrated
to the IMF / World Bank classification of low-income vs upper-middle vs
high-income economies. They are NOT fitted to any particular outcome -
this is a descriptive categorisation, not a forecast.
"""

from __future__ import annotations

import math


# Vertex layout for the ternary triangle.
# Top vertex   = pure agriculture        (a=1, i=0, s=0)
# Bottom-left  = pure industry           (a=0, i=1, s=0)
# Bottom-right = pure services           (a=0, i=0, s=1)
_TOP    = (0.5, math.sqrt(3) / 2)
_BLEFT  = (0.0, 0.0)
_BRIGHT = (1.0, 0.0)


def ternary_xy(agri_pct: float, industry_pct: float, services_pct: float) -> dict:
    """Convert sector shares (out of 100) to ternary Cartesian (x, y).

    The triangle spans x in [0, 1] and y in [0, sqrt(3)/2]. The point is
    a barycentric combination of the three vertices weighted by sector
    share."""
    total = agri_pct + industry_pct + services_pct
    if total <= 0:
        return {"x": None, "y": None}
    a = agri_pct / total
    i = industry_pct / total
    s = services_pct / total
    x = a * _TOP[0] + i * _BLEFT[0] + s * _BRIGHT[0]
    y = a * _TOP[1] + i * _BLEFT[1] + s * _BRIGHT[1]
    return {"x": x, "y": y}


def classify(agri_pct: float, industry_pct: float, services_pct: float) -> dict:
    """Five-bucket Clark-Fisher stage for one country."""
    a, i, s = agri_pct, industry_pct, services_pct

    if a >= 40:
        bucket = "pre_industrial"
        label = "Pre-industrial"
        stage = 1
    elif a >= 20:
        bucket = "industrialising"
        label = "Industrialising"
        stage = 2
    elif i >= 30 and s < 60:
        bucket = "industrial"
        label = "Industrial"
        stage = 3
    elif s >= 75:
        bucket = "information"
        label = "Information / knowledge"
        stage = 5
    else:
        bucket = "post_industrial"
        label = "Post-industrial"
        stage = 4

    # Composite "development position" in [0, 100] - useful for ranking and
    # for plotting countries along the arc. Heuristic: services minus
    # agriculture, rescaled. Doesn't replace classification, just orders.
    development = max(0.0, min(100.0, (s - a) + 50.0))

    return {
        "bucket": bucket,
        "label": label,
        "stage": stage,
        "development_index": development,
    }


def annotate(country: dict) -> dict:
    a = country.get("agriculture_pct") or 0.0
    i = country.get("industry_pct") or 0.0
    s = country.get("services_pct") or 0.0
    return {
        **country,
        **classify(a, i, s),
        "ternary": ternary_xy(a, i, s),
    }


def annotate_trajectory(trajectory: list[dict]) -> list[dict]:
    """Stamp each annual point with stage + ternary coords for trail rendering."""
    out: list[dict] = []
    for p in trajectory:
        a = p.get("agriculture_pct") or 0.0
        i = p.get("industry_pct") or 0.0
        s = p.get("services_pct") or 0.0
        out.append({
            **p,
            **classify(a, i, s),
            "ternary": ternary_xy(a, i, s),
        })
    return out


# Stage metadata for the legend / UI. Order matters - it's the natural arc.
STAGE_META: list[dict] = [
    {"bucket": "pre_industrial",   "label": "Pre-industrial",         "stage": 1, "color": "#a78bfa", "blurb": "Agriculture ≥ 40% of employment"},
    {"bucket": "industrialising",  "label": "Industrialising",        "stage": 2, "color": "#f59e0b", "blurb": "Agriculture 20-40%, industry rising"},
    {"bucket": "industrial",       "label": "Industrial",             "stage": 3, "color": "#ef4444", "blurb": "Industry ≥ 30%, services < 60%"},
    {"bucket": "post_industrial",  "label": "Post-industrial",        "stage": 4, "color": "#22d3ee", "blurb": "Services 60-75%, agriculture < 20%"},
    {"bucket": "information",      "label": "Information / knowledge","stage": 5, "color": "#34d399", "blurb": "Services ≥ 75%"},
]


def summarise(countries: list[dict]) -> dict:
    annotated = [annotate(c) for c in countries]
    # Count per bucket
    counts: dict[str, int] = {m["bucket"]: 0 for m in STAGE_META}
    for c in annotated:
        counts[c["bucket"]] = counts.get(c["bucket"], 0) + 1
    # Median sector shares
    n = len(annotated)
    def median(key: str) -> float | None:
        vals = sorted(c[key] for c in annotated if c.get(key) is not None)
        if not vals:
            return None
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2
    return {
        "countries": annotated,
        "stages": STAGE_META,
        "stage_counts": counts,
        "n_countries": n,
        "global_medians": {
            "agriculture_pct": median("agriculture_pct"),
            "industry_pct":    median("industry_pct"),
            "services_pct":    median("services_pct"),
        },
        "ternary_vertices": {
            "agriculture": {"x": _TOP[0],    "y": _TOP[1]},
            "industry":    {"x": _BLEFT[0],  "y": _BLEFT[1]},
            "services":    {"x": _BRIGHT[0], "y": _BRIGHT[1]},
        },
    }
