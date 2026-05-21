"""Plain-English description of every prediction model.

Served at /api/methodology so the dashboard can build a transparent
"how it works" page from a single source of truth, and so users can audit
exactly what each card claims to predict.
"""
from __future__ import annotations

MODELS = [
    {
        "id": "temperature_year_end_projection",
        "name": "Year-end global temperature anomaly",
        "summary": "Project this year's annual mean from the year-to-date mean plus the historical drift from same-month-of-year to year-end.",
        "inputs": ["NASA GISTEMP v4 monthly anomaly (°C, vs 1951-1980)"],
        "outputs": {
            "projected_annual_anomaly_c": "Year-end annual-mean anomaly forecast",
            "p_breaks_record": "P(annual mean > current record) under N(projection, drift_std)",
            "drift_std_c": "Std of historical drift across all complete past years",
        },
        "code": "app/models/temperature.py",
    },
    {
        "id": "co2_year_end_projection",
        "name": "Year-end atmospheric CO₂",
        "summary": "Linear regression of the last 24 months of NOAA Mauna Loa monthly means, evaluated at decimal year + 1.0.",
        "inputs": ["NOAA GML Mauna Loa monthly CO₂ (ppm)"],
        "outputs": {
            "projected_year_end_ppm": "Year-end CO₂ projection",
            "ppm_per_year": "Fitted slope",
            "residual_std_ppm": "RMS of in-sample residuals; floored at 0.3 ppm for forecast bands",
        },
        "code": "app/models/co2.py",
    },
    {
        "id": "methane_year_end_projection",
        "name": "Year-end atmospheric CH₄",
        "summary": "24-month linear regression on NOAA GML globally-averaged methane. Same shape as CO₂; σ floor is 2.0 ppb because methane is noisier.",
        "inputs": ["NOAA GML globally-averaged monthly CH₄ (ppb)"],
        "outputs": {
            "projected_year_end_ppb": "Year-end methane projection",
            "ppb_per_year": "Fitted slope",
            "residual_std_ppb": "Floored at 2.0 ppb",
        },
        "code": "app/models/methane.py",
    },
    {
        "id": "n2o_year_end_projection",
        "name": "Year-end atmospheric N₂O",
        "summary": "Same 24-month linear regression as CO₂/CH₄, applied to NOAA GML globally-averaged nitrous oxide. N₂O rises ~1 ppb/yr and is very smooth, so the σ floor is the tightest (0.3 ppb).",
        "inputs": ["NOAA GML globally-averaged monthly N₂O (ppb)"],
        "outputs": {
            "projected_year_end_ppb": "Year-end N₂O projection",
            "ppb_per_year": "Fitted slope",
            "residual_std_ppb": "Floored at 0.3 ppb",
        },
        "code": "app/models/n2o.py",
    },
    {
        "id": "arctic_min_projection",
        "name": "Arctic annual-minimum sea ice extent",
        "summary": "Linear regression of the last 25 years' annual minimum extents against year. Current year is excluded from the fit until its minimum is reached (mid-September).",
        "inputs": ["NSIDC Sea Ice Index G02135 v4.0 (north, daily extent)"],
        "outputs": {
            "projected_min_mkm2": "This summer's projected annual minimum",
            "trend_mkm2_per_year": "Fitted slope of annual minima",
            "residual_std_mkm2": "Floored at 0.1 Mkm²",
        },
        "code": "app/models/sea_ice.py",
    },
    {
        "id": "antarctic_min_projection",
        "name": "Antarctic annual-minimum sea ice extent",
        "summary": "Same regression as Arctic. Antarctic minimum typically falls Feb 14 – early March; current year is excluded until mid-March.",
        "inputs": ["NSIDC Sea Ice Index G02135 v4.0 (south, daily extent)"],
        "outputs": {
            "projected_min_mkm2": "Projected annual minimum",
            "trend_mkm2_per_year": "Fitted slope",
            "residual_std_mkm2": "Floored at 0.15 Mkm²",
        },
        "code": "app/models/sea_ice.py",
    },
    {
        "id": "market_scoring",
        "name": "Polymarket edge attribution",
        "summary": "For each climate market, parse the question text against a fixed regex set, derive a model probability from the relevant projection, and compute edge = (model_p − implied_p) in percentage points.",
        "inputs": [
            "Polymarket Gamma /events with climate tag slugs",
            "Outputs of the projection models above",
        ],
        "outputs": {
            "_model_p": "Model-derived probability of YES",
            "_implied_p": "Last trade price (or best bid)",
            "_edge_pp": "(model_p − implied_p) × 100, in percentage points",
            "_rationale": "Short string showing the model inputs used for this market",
        },
        "code": "app/models/markets.py",
    },
    {
        "id": "highlights",
        "name": "Today's highlights",
        "summary": "Pure-derivative one-liners surfaced at the top of the dashboard. Examines the cached upstream data for: (a) whether the last completed year was a new annual temperature record; (b) streaks of years above +1.0°C / +1.5°C anomaly; (c) the 12-month change in CO₂ / CH₄ / N₂O; (d) the Arctic sea-ice rank for today's day-of-year; (e) the current ENSO state and how long it's held. Nothing inferred — every chip can be derived directly from the data the dashboard already shows.",
        "inputs": ["All of the upstream fetchers — no new HTTP requests"],
        "outputs": {
            "items": "List of {kind, text} chips. Kinds: record / trend / alert / regime / milestone.",
        },
        "code": "app/models/highlights.py",
    },
    {
        "id": "kelly_position_sizing",
        "name": "Kelly position sizing",
        "summary": "Standard Kelly criterion applied to each binary market: f* = (p·b − q) / b, where p is the model probability of YES, q=1−p, and b=(1−implied)/implied. We compute f* for both YES and NO sides and surface the larger positive value. Output is rendered as a percentage of bankroll, or — if a bankroll is set — as a $ amount. Display defaults to Half-Kelly (¼–½ Kelly are the standard real-world multipliers; full Kelly's drawdowns are brutal). The bankroll is stored only in the browser's localStorage and never leaves the device.",
        "inputs": ["Model probability + implied probability from each market"],
        "outputs": {
            "side": "YES or NO — whichever side has positive expected log-growth",
            "fraction": "Recommended Kelly fraction of bankroll, before the multiplier",
        },
        "code": "static/index.html (kellyBet)",
    },
]

BACKTESTS = [
    {
        "id": "gistemp_backtest",
        "summary": "Replays the YTD-anomaly + historical-drift model 'as of June' for each of the last 5 completed years and reports projected vs actual J-D mean.",
    },
    {
        "id": "co2_backtest",
        "summary": "Refits the 24-month linear regression at June of each year and scores against the actual December reading.",
    },
    {
        "id": "methane_backtest",
        "summary": "Same June-cutoff 24-month regression as CO₂, scored against the actual December reading.",
    },
    {
        "id": "n2o_backtest",
        "summary": "Same June-cutoff 24-month regression as CO₂/CH₄, scored against the actual December N₂O reading.",
    },
]


def payload(commit: str | None = None) -> dict:
    return {
        "models": MODELS,
        "backtests": BACKTESTS,
        "commit": commit,
        "license": "Free public sources; see /api/health and dashboard footer for attribution.",
    }
