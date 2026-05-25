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
        "id": "sf6_year_end_projection",
        "name": "Year-end atmospheric SF₆",
        "summary": "Same 24-month linear regression as the other GHGs, on NOAA GML globally-averaged sulfur hexafluoride. SF₆ rises ~0.3 ppt/yr almost monotonically (electrical-industry leaks; ~25,000× CO₂'s 100-yr GWP) so the σ floor is the tightest at 0.05 ppt.",
        "inputs": ["NOAA GML globally-averaged monthly SF₆ (ppt)"],
        "outputs": {
            "projected_year_end_ppt": "Year-end SF₆ projection",
            "ppt_per_year": "Fitted slope",
            "residual_std_ppt": "Floored at 0.05 ppt",
        },
        "code": "app/models/sf6.py",
    },
    {
        "id": "sea_level",
        "name": "Global mean sea level",
        "summary": "NOAA STAR Laboratory for Satellite Altimetry — global mean sea level rise in millimeters since the start of the satellite record (1993). Best-effort URL: if NESDIS restructures we render an explicit unavailable state. Parser sniffs the date and value columns from the CSV header so format drift in either direction is tolerated.",
        "inputs": ["NOAA STAR LSA_SLR_timeseries_global.csv"],
        "outputs": {
            "series": "List of {decimal_year, sea_level_mm}",
            "latest": "Most recent point",
        },
        "code": "app/fetchers/sea_level.py",
    },
    {
        "id": "ocean_heat_content",
        "name": "Ocean heat content (0-2000 m)",
        "summary": "NOAA NCEI yearly anomaly in 10^22 J. Ocean heat content is the integrator climate scientists trust most — atmospheric noise averages out and the underlying energy accumulation shows up cleanly. URL is the canonical /data/oceans/woa/DATA_ANALYSIS/3M_HEAT_CONTENT/... path; if NCEI restructures their data hosting the dashboard's card will explicitly show 'data unavailable' rather than disappearing silently.",
        "inputs": ["NOAA NCEI heat_content_anomaly_0-2000_yearly.csv"],
        "outputs": {
            "yearly": "List of {year, ohc_1e22_J}",
            "latest": "Most recent year's anomaly",
        },
        "code": "app/fetchers/ocean_heat.py",
    },
    {
        "id": "snow_cover",
        "name": "Northern Hemisphere snow cover extent",
        "summary": "Rutgers Global Snow Lab monthly NH land snow cover in million km². Parser handles both long-format (year month extent) and wide-format (year + 12 monthly columns), and auto-converts raw km² values to million km² if the upstream switches units.",
        "inputs": ["Rutgers Global Snow Lab — moncov.nhland.txt"],
        "outputs": {
            "monthly": "List of {year, month, extent_mkm2}",
            "latest": "Most recent month's extent",
        },
        "code": "app/fetchers/snow_cover.py",
    },
    {
        "id": "ssp_scenarios",
        "name": "IPCC SSP scenario matching",
        "summary": "Compares the dashboard's current temperature anomaly and CO₂ concentration against the IPCC AR6 SSP1-2.6 / SSP2-4.5 / SSP3-7.0 / SSP5-8.5 trajectories and reports the closest match for each metric. Trajectories are linearly interpolated between IPCC anchor years (2020, 2030, 2050, 2075, 2100). Temperature uses the IPCC 1850-1900 baseline; dashboard readings (GISTEMP 1951-1980 baseline) are offset by +0.2°C before comparison.",
        "inputs": [
            "Latest GISTEMP annual anomaly + current CO₂ from NOAA Mauna Loa",
            "Hard-coded IPCC AR6 WG1 Table SPM.1 + SSP-database anchor points",
        ],
        "outputs": {
            "current_match": "{temperature: {scenario, distance_c}, co2: {scenario, distance_ppm}} — which scenario is the dashboard's current pace closest to",
            "trajectories": "Per-scenario year × value series for plotting",
        },
        "code": "app/models/scenarios.py",
    },
    {
        "id": "country_emissions",
        "name": "Country-level CO₂ emissions",
        "summary": "Top emitters by total annual CO₂ (million tonnes) for the latest year on record, plus per-capita and share-of-global breakdowns. Filters out regional aggregates (codes starting with 'OWID_') so the leaderboard is real countries only. Includes a global summary showing the 10-year change in worldwide emissions.",
        "inputs": ["Our World in Data owid-co2-data.csv (mirror of CDIAC + EDGAR + national inventories)"],
        "outputs": {
            "top_emitters": "Sorted list of {iso, country, co2_mt, co2_per_capita_t, share_global}",
            "global": "World total CO₂ + 10-year change",
        },
        "code": "app/models/emissions.py",
    },
    {
        "id": "radiative_forcing",
        "name": "Total anthropogenic GHG radiative forcing",
        "summary": "Combines CO₂ + CH₄ + N₂O + SF₆ atmospheric concentrations into a single W/m² number — the actual climate-relevant metric. Uses the simplified IPCC AR5 / Myhre 1998 formulas, with the CH₄ ↔ N₂O absorption-band overlap term. Reports per-gas breakdown plus 'effective CO₂ ppm' — the CO₂ concentration that alone would produce the same total forcing.",
        "inputs": [
            "Current monthly means from NOAA GML for CO₂, CH₄, N₂O, SF₆",
            "Pre-industrial (1750) reference values: CO₂=278 ppm, CH₄=722 ppb, N₂O=270 ppb, SF₆=0 ppt",
        ],
        "outputs": {
            "co2_wm2": "CO₂ contribution to radiative forcing",
            "ch4_wm2": "CH₄ contribution (with N₂O overlap correction)",
            "n2o_wm2": "N₂O contribution (with CH₄ overlap correction)",
            "sf6_wm2": "SF₆ contribution",
            "total_wm2": "Sum across gases",
            "effective_co2_ppm": "CO₂ concentration that would produce the same total forcing alone",
        },
        "code": "app/models/forcing.py",
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
        "id": "upstream_status",
        "name": "Upstream status dashboard",
        "summary": "Per-source health snapshot — runs every fetcher (through the cache, so it's cheap on repeat) and classifies each as OK / down / error. Surfaces the actual URL the dashboard is hitting and the last data value, so an operator can swap broken URLs without poking at individual endpoints. Served at /status; raw payload at /api/status.",
        "inputs": ["All registered upstream fetchers"],
        "outputs": {
            "sources": "List of {name, status, url, summary, fetched_at}",
            "counts": "{ok, down, error} totals for the summary pills",
        },
        "code": "app/status.py + server.py _STATUS_SOURCES",
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
    {
        "id": "sf6_backtest",
        "summary": "Same June-cutoff 24-month regression, scored against the actual December SF₆ reading.",
    },
]


def payload(commit: str | None = None) -> dict:
    return {
        "models": MODELS,
        "backtests": BACKTESTS,
        "commit": commit,
        "license": "Free public sources; see /api/health and dashboard footer for attribution.",
    }
