# Climate Change Dashboard (v3)

Long-horizon climate prediction markets with model-derived edges.

Listens on `:7052`. Subdomain: `climate.narve.ai` (registered in `gateway/config.json`).

## What's new in v3

- **Atmospheric methane (CH₄)** — adds NOAA GML globally-averaged monthly methane (`ch4_mm_gl.csv`, ppb). Backend follows the same shape as CO₂: 24-month linear regression with residual std, threshold probability table, year-end backtest. New endpoints `/api/methane` and methane block in `/api/summary` + `/api/backtest`. Methane card on the front page, threshold pills (1930 / 1940 / 1950 / … ppb), and a methane row in the model-performance table.
- **Methane market matcher** — handles "above N ppb / below N ppb" plus "above 1.95 ppm" (CH₄ markets are sometimes priced in either unit).

## What was added in v2

- **Antarctic sea-ice min projection** — same 25-year linear-trend / residual-std normal-CDF approach as Arctic; scores antarctic-min markets when they appear.
- **Proper CO₂ threshold model** — the v1 hack returned a flat 85/15. v2 returns the regression's residual std and uses a normal CDF to score "above N ppm" markets.
- **Annual-anomaly threshold model** — P(annual mean ≥ 1.5°C / 1.6°C / 1.7°C) under N(projection, drift_std), exposed as pills in the temperature card and used to score "above N°C" markets.
- **Backtest panel** — replays both models 'as of June' for the last 5 completed years; shown at the bottom of the page and at `/api/backtest`.
- **Broader Polymarket tag coverage** — adds `global-warming`, `sea-level`, `extreme-weather` to the previous three; markets that the dashboard can't model still appear in the table with a blank model column.
- **Tighter market matchers** — handle "below / above / over / under / exceed" + ranges and dropped the over-broad `vs.` reject keyword.

## Data sources

| What | Source | Cadence |
| --- | --- | --- |
| Global temperature anomaly | NASA GISTEMP v4 (`GLB.Ts+dSST.csv`) | monthly |
| Atmospheric CO₂ | NOAA GML Mauna Loa (`co2_mm_mlo.csv`) | monthly |
| Atmospheric CH₄ | NOAA GML globally-averaged (`ch4_mm_gl.csv`) | monthly |
| Arctic + Antarctic sea ice extent | NSIDC Sea Ice Index G02135 v3.0 | daily |
| Global SST 60°S–60°N | NOAA OISST 2.1 via Climate Reanalyzer JSON | daily |
| ENSO state | NOAA CPC Oceanic Niño Index (`oni.data`) | monthly |
| Markets | Polymarket Gamma `tag_slug=climate-change` (+ siblings) | live |

All upstream sources are free and require no API key. Each fetcher has its
own TTL (12h for monthly series, 3–6h for daily, 5min for markets).

## Models

- **Year-end record-pace projection (temperature):** YTD anomaly + average historical drift from same-month-of-year to year-end. P(new record) is a normal CDF using the historical std of that drift.
- **CO₂ trajectory:** linear regression of last 24 months → year-end ppm.
- **Sea ice rank-on-DOY:** today's extent ranked against the same calendar day across the full record.

When a Polymarket market has a discoverable target (e.g. "warmest year on record", "CO₂ above N ppm"), the model probability is attached and an edge in percentage points is computed against the market-implied price.

## Run locally

```bash
pip install -r requirements.txt
python3 server.py
# → http://localhost:7052
```

## Endpoints

- `GET /api/summary` — single page-load payload: temperature + CO₂ + ice projections, threshold probabilities, ENSO state
- `GET /api/markets` — climate markets with model edges (Arctic + Antarctic sea ice, year-end record, anomaly threshold, CO₂ threshold)
- `GET /api/temperature` — full GISTEMP series + projection
- `GET /api/co2` — full Mauna Loa series + projection (with residual std)
- `GET /api/methane` — full NOAA GML CH₄ series + projection + threshold probabilities
- `GET /api/sea-ice` — Arctic + Antarctic recent series + record check
- `GET /api/sst` — Climate Reanalyzer JSON (whole multi-year structure)
- `GET /api/regime` — ONI + ENSO state
- `GET /api/backtest` — last 5 completed years, projection-vs-actual for both temperature and CO₂ models
- `GET /api/health` — liveness

## Container

```bash
docker build -t climate-dashboard .
docker run --rm -p 7052:7052 climate-dashboard
```

## Notes

- This dashboard intentionally does **not** cover natural disasters or short-term weather — those live in `polymarket_weather_dashboard` and (planned) the Major Disasters dashboard.
- The Polymarket fetcher reuses the same gamma pattern as the weather dashboard, but applies a stricter climate-keyword filter so it doesn't sweep in tornado/hurricane markets.
- ENSO state (ONI) is shown as a banner because it's the single most useful short-term covariate for both temperature and SST markets.
