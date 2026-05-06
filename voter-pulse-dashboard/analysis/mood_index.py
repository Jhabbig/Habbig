"""Composite "national mood" score.

Blend of three sub-indices, each scaled to [0, 100] (higher = better):

  Pocketbook    — inverse of YoY headline + food CPI, gas pump price, and
                  the mortgage rate. Cost-of-living component.
  Jobs          — inverse of unemployment rate, plus YoY change in real
                  median weekly earnings.
  Sentiment     — University of Michigan Consumer Sentiment Index, rescaled
                  to [0, 100] using its historical 1978-present range.

The composite is a simple equal-weight mean. We deliberately avoid
overfitting weights to recent data — the goal is an honest, legible read,
not a forecasting model.

The misery index (UNRATE + CPI YoY) is also returned for context. It's
backwards-looking but it's the single number that captures "how it feels"
better than any other.
"""

from __future__ import annotations

from dataclasses import dataclass

# Long-run UMich consumer sentiment range used to rescale to 0-100. Picked
# from the well-known historical extremes (~50 in mid-2022 and 1980, ~110
# in 2000); this gives us a [0, 100] scale that maps recent observations
# into a reasonable place without time-series anchoring.
UMICH_LOW = 50.0
UMICH_HIGH = 110.0


@dataclass
class MoodSubScore:
    name: str
    value: float | None  # 0-100, higher = better
    components: list[dict]


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _yoy(series: dict) -> float | None:
    return series.get("yoy_pct")


def _latest(series: dict) -> float | None:
    latest = series.get("latest")
    return latest["value"] if latest else None


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["series_id"]: r for r in rows}


def pocketbook_score(rows: list[dict]) -> MoodSubScore:
    """Cost-of-living component: lower CPI YoY, gas, mortgage = higher score."""
    by = _by_id(rows)
    components: list[dict] = []
    score_parts: list[float] = []

    # CPI YoY: 0% → 100, 4% → 50, 8%+ → 0  (linear)
    cpi_yoy = _yoy(by.get("CPIAUCSL", {}))
    if cpi_yoy is not None:
        s = _clip(100 - (cpi_yoy * 12.5))
        components.append({"label": "Headline CPI YoY", "value": cpi_yoy, "units": "%", "score": s})
        score_parts.append(s)

    # Food CPI YoY: 0 → 100, 5 → 50, 10+ → 0
    food_yoy = _yoy(by.get("CPIUFDSL", {}))
    if food_yoy is not None:
        s = _clip(100 - (food_yoy * 10))
        components.append({"label": "Food CPI YoY", "value": food_yoy, "units": "%", "score": s})
        score_parts.append(s)

    # Gas price: $2.50 → 100, $4.00 → 50, $5.50+ → 0
    gas = _latest(by.get("GASREGW", {}))
    if gas is not None:
        s = _clip(100 - ((gas - 2.5) * (50.0 / 1.5)))
        components.append({"label": "Gas (regular)", "value": gas, "units": "$/gal", "score": s})
        score_parts.append(s)

    # 30-yr mortgage: 3% → 100, 6% → 50, 9%+ → 0
    mort = _latest(by.get("MORTGAGE30US", {}))
    if mort is not None:
        s = _clip(100 - ((mort - 3) * (50.0 / 3.0)))
        components.append({"label": "30-yr mortgage", "value": mort, "units": "%", "score": s})
        score_parts.append(s)

    score = sum(score_parts) / len(score_parts) if score_parts else None
    return MoodSubScore("pocketbook", score, components)


def jobs_score(rows: list[dict]) -> MoodSubScore:
    """Labour-market component: lower unemployment + rising real wages."""
    by = _by_id(rows)
    components: list[dict] = []
    score_parts: list[float] = []

    # UNRATE: 3% → 100, 6% → 50, 9%+ → 0
    unrate = _latest(by.get("UNRATE", {}))
    if unrate is not None:
        s = _clip(100 - ((unrate - 3) * (50.0 / 3.0)))
        components.append({"label": "Unemployment rate", "value": unrate, "units": "%", "score": s})
        score_parts.append(s)

    # Real median weekly earnings YoY: -2% → 0, 0% → 50, +2% → 100, +4%+ → 100
    wage_yoy = _yoy(by.get("LES1252881600Q", {}))
    if wage_yoy is not None:
        s = _clip(50 + (wage_yoy * 25))
        components.append({"label": "Real wages YoY", "value": wage_yoy, "units": "%", "score": s})
        score_parts.append(s)

    score = sum(score_parts) / len(score_parts) if score_parts else None
    return MoodSubScore("jobs", score, components)


def sentiment_score(rows: list[dict]) -> MoodSubScore:
    by = _by_id(rows)
    umich = _latest(by.get("UMCSENT", {}))
    components: list[dict] = []
    if umich is None:
        return MoodSubScore("sentiment", None, components)
    s = _clip((umich - UMICH_LOW) / (UMICH_HIGH - UMICH_LOW) * 100.0)
    components.append({"label": "UMich consumer sentiment", "value": umich, "units": "index", "score": s})
    return MoodSubScore("sentiment", s, components)


def misery_index(rows: list[dict]) -> float | None:
    """UNRATE + CPI YoY. The classic Okun number — high is bad."""
    by = _by_id(rows)
    unrate = _latest(by.get("UNRATE", {}))
    cpi_yoy = _yoy(by.get("CPIAUCSL", {}))
    if unrate is None or cpi_yoy is None:
        return None
    return unrate + cpi_yoy


def compose(rows: list[dict]) -> dict:
    pb = pocketbook_score(rows)
    jb = jobs_score(rows)
    st = sentiment_score(rows)
    parts = [s.value for s in (pb, jb, st) if s.value is not None]
    overall = sum(parts) / len(parts) if parts else None
    return {
        "overall": overall,
        "subscores": {
            "pocketbook": {"score": pb.value, "components": pb.components},
            "jobs":       {"score": jb.value, "components": jb.components},
            "sentiment":  {"score": st.value, "components": st.components},
        },
        "misery_index": misery_index(rows),
    }


def label_for(score: float | None) -> str:
    if score is None:
        return "n/a"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Okay"
    if score >= 40:
        return "Strained"
    if score >= 25:
        return "Sour"
    return "Bleak"
