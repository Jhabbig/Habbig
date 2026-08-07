# climate-dashboard/ — Climate Change Dashboard

Long-horizon climate prediction markets with model-derived edges. Port **7052**, subdomain `climate.narve.ai`. See [README.md](README.md) for data sources and model details.

## Scope (important — don't drift)

This dashboard covers **long-horizon climate** — temperature anomaly, CO₂, CH₄, sea ice, SST, ENSO. It intentionally does **NOT** cover:
- Short-term weather → that's `polymarket_weather_dashboard/`
- Natural disasters → that's the (planned) Major Disasters dashboard

The Polymarket fetcher already filters by climate keywords to avoid sweeping in tornado/hurricane markets. Don't loosen that filter without thinking through the dedup with the other dashboards.

## Stack

- FastAPI + uvicorn — small server (`server.py`)
- No DB — all data fetched fresh from upstream (NOAA, NASA, NSIDC, Polymarket Gamma) with TTL caching in memory
- All upstream sources are free + no API key required

## Models (the core IP)

- **Year-end record-pace projection (temperature):** YTD anomaly + average historical drift to year-end. P(new record) via normal CDF using historical drift std.
- **CO₂ trajectory:** linear regression of last 24 months → year-end ppm. Returns residual std for proper threshold scoring.
- **CH₄ trajectory:** same shape as CO₂.
- **Sea-ice rank-on-DOY:** today's extent ranked against same calendar day across full record.
- **Annual-anomaly threshold model:** P(annual mean ≥ Nº C) under N(projection, drift_std).

When a Polymarket market has a discoverable target (e.g. "warmest year on record", "CO₂ above N ppm"), the model probability is attached and an edge in pp is computed against market price.

## Endpoints

- `GET /api/summary` — single page-load payload (temp + CO₂ + ice + ENSO)
- `GET /api/markets` — climate markets with model edges
- `GET /api/temperature` — full GISTEMP series + projection
- `GET /api/co2` — Mauna Loa series + projection (with residual std)
- `GET /api/methane` — NOAA GML CH₄ series + projection + threshold probabilities
- `GET /api/sea-ice` — Arctic + Antarctic recent series + record check
- `GET /api/sst` — Climate Reanalyzer JSON
- `GET /api/regime` — ONI + ENSO state
- `GET /api/backtest` — last 5 completed years, projection-vs-actual
- `GET /api/health` — liveness (this is the canonical healthz; gateway/docker-compose expect `/healthz`, double-check the route name)

## Common gotchas

- **TTL mismatch:** monthly series cache 12h, daily 3-6h, markets 5min. Don't unify them blindly — short cache on monthly data wastes upstream calls; long cache on markets makes the dashboard go stale.
- **NSIDC Sea Ice Index G02135 v3.0** has changed format before. If sea-ice fetch breaks, check column headers first.
- **NOAA Mauna Loa data** has occasional missing months (-99.99 sentinel). Filter before regression.
- **Climate Reanalyzer SST** returns a multi-year nested JSON; parsing is fragile to upstream shape changes.
- **ENSO state (ONI banner)** is the single most useful short-term covariate for both temperature and SST market scoring. Don't remove the banner.

## Conventions

- All upstream HTTP via `httpx.AsyncClient`. No global session.
- Each fetcher owns its own TTL and parsing — don't try to abstract them prematurely; their upstream formats differ.
- Match-and-score logic for markets lives separately from the fetchers. Threshold pills live in the market matcher.
