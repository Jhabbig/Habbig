# World Health Dashboard

Interactive 3D globe of world health indicators. Click any country to see its
full profile across life expectancy, disease burden, healthcare systems,
immunization, risk factors, and demographics.

Default port **7053**. Phase 1 only — see roadmap below.

## Data sources

- **WHO Global Health Observatory (GHO)** OData API
  `https://ghoapi.azureedge.net/api/<INDICATOR_CODE>`
- **World Bank Open Data**
  `https://api.worldbank.org/v2/country/all/indicator/<CODE>`

Both are free and key-less. Per-indicator JSON is cached on disk for 24h
under `cache/who_gho/` and `cache/world_bank/`.

## Run locally

```bash
cd world-health-dashboard
pip install -r requirements.txt
DEV_MODE=1 python3 server.py
# then open http://localhost:7053
```

`DEV_MODE=1` bypasses the gateway-SSO check. In production, set
`GATEWAY_SSO_SECRET` to the same secret the gateway uses.

## API

| Path | Returns |
|---|---|
| `GET /api/metrics` | Catalog of every metric (id, name, category, unit, source, direction). |
| `GET /api/countries` | ISO3 / display name / region for every country. |
| `GET /api/globe/{metric_id}` | `{iso3 → latest_value}` plus min/max/p10/p50/p90 for color scaling. |
| `GET /api/country/{iso3}` | Latest value of every metric for one country. |
| `GET /api/history/{metric_id}?country=USA` | Time series for one metric in one country. |
| `GET /api/compare?a=USA&b=DEU` | Side-by-side profiles. |
| `GET /healthz` | Liveness probe (auth-bypass). |

## Phase roadmap

- **Phase 1 (this) — Globe + core stats.** WHO + World Bank ingestion, 3D globe with metric selector, country drill-down, search, history endpoint.
- **Phase 2 — Outbreak feeds.** WHO Disease Outbreak News, CDC HAN, ProMED → live red pins on the globe. H5N1 sub-tab with human / animal cases.
- **Phase 3 — Polymarket health-edge panel.** Pull health-tagged markets via Gamma, compute model fair value vs market price.
- **Phase 4 — Compare mode + time slider.** Side-by-side country diff, scrub through 1960→latest.
- **Phase 5 — Production wiring.** Gateway subdomain (`health.narve.ai`), Stripe pricing, Ubuntu deploy, Docker compose entry.
