"""Historical mood-index reconstruction.

The dashboard's mood index is normally computed against the latest
observation of each FRED series. For the backtest we need it computed at
arbitrary "as-of" dates, walking back to the late 1970s (when UMCSENT
becomes monthly and the composite has enough components to be honest).

`series_as_of(series, as_of)` truncates one indicator-series payload to
all observations on-or-before `as_of` and recomputes `latest` and
`yoy_pct`. `monthly_history(rows, start)` then sweeps every distinct
month and runs the real `mood_index.compose` against the truncated rows.

We intentionally reuse the production scoring — no retuning of bands for
the backtest. The point is to ask whether the mood index *as currently
shipped* would have called past elections, not whether we can fit a
composite that does.
"""

from __future__ import annotations

from . import mood_index


def series_as_of(series: dict, as_of: str) -> dict | None:
    """Return a copy of `series` truncated to points ≤ `as_of`.

    `latest` and `yoy_pct` are recomputed against the new truncated tail.
    Returns None if no observations remain.
    """
    points = series.get("points") or []
    truncated = [p for p in points if (p.get("date") or "") <= as_of]
    if not truncated:
        return None
    latest = truncated[-1]
    latest_date = latest.get("date") or ""
    # YoY against the closest observation ≤ (latest − 1 year).
    target_year = int(latest_date[:4]) - 1
    target_iso = f"{target_year:04d}{latest_date[4:]}"
    prior_val: float | None = None
    for p in truncated[:-1]:
        if (p.get("date") or "") <= target_iso:
            prior_val = p.get("value")
        else:
            break
    yoy: float | None = None
    if prior_val is not None and prior_val != 0:
        yoy = (latest["value"] - prior_val) / prior_val * 100.0
    return {
        **series,
        "points": truncated,
        "latest": latest,
        "yoy_pct": yoy,
    }


def mood_as_of(rows: list[dict], as_of: str) -> dict:
    """Compose the mood index using only data on-or-before `as_of`."""
    sliced: list[dict] = []
    for s in rows:
        t = series_as_of(s, as_of)
        if t is not None:
            sliced.append(t)
    composed = mood_index.compose(sliced)
    composed["label"] = mood_index.label_for(composed.get("overall"))
    composed["as_of"] = as_of
    return composed


def monthly_history(rows: list[dict], start: str = "1978-01-01") -> list[dict]:
    """Sweep every month from `start` to the latest observation in `rows`
    and compute (date, mood overall, misery index) at each."""
    months: set[str] = set()
    for s in rows:
        for p in s.get("points") or []:
            d = p.get("date") or ""
            if len(d) >= 10 and d >= start:
                months.add(d[:7] + "-01")
    out: list[dict] = []
    for m in sorted(months):
        composed = mood_as_of(rows, m)
        out.append({
            "date": m,
            "overall": composed.get("overall"),
            "misery_index": composed.get("misery_index"),
        })
    return out
