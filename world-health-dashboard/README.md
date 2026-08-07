# World Health Dashboard

10-tab interactive dashboard covering **508 diseases**, **live disease
outbreaks**, **antimicrobial-resistance globe**, **drug supply chains**, and
**cross-venue health prediction markets**. Default port **7053**, served
via the central gateway at `health.narve.ai` in production.

## Tabs

| Tab | What |
|---|---|
| **Globe** | 59 metrics + composite treatment-vulnerability layer over a 3D globe |
| **Outbreaks** | WHO Disease Outbreak News, severity-coded pins |
| **H5N1** | Cumulative human cases by country + recent H5* DONs |
| **Excess Mortality** | OWID monthly P-scores, 110 countries |
| **PHEICs** | All 8 PHEICs ever declared (2009-) |
| **Markets** | 659 health prediction markets across Polymarket / Kalshi / Manifold + cross-venue spreads |
| **Atlas** | 508 diseases (211 with WHO factsheets), click any drug → supply-chain weak-point profile |
| **HAI / AMR** | 6 resistance indicators (MRSA, E. coli 3GC, TB-RR, gonorrhoea cipro/cef/azith) + C. auris US states |
| **Shortages** | 1,140 active FDA drug shortages + per-disease vulnerability rollup |
| **Trade** | HS-30 pharma trade flows · top-30 exporters / importers · per-country deep-dive |

## Data sources (all free, no keys)

| Source | Used for |
|---|---|
| WHO Global Health Observatory (OData) | 59 health indicators, AMR rates |
| World Bank Open Data | Health spending, demographics, hi-tech-exports proxy |
| WHO Disease Outbreak News (OData JSON API) | Live outbreak feed |
| WHO Fact Sheets (HTML) | 211 rich disease records |
| Our World in Data | Excess mortality |
| openFDA — Drug Labels, Shortages, Enforcement | Drug supply-chain analysis |
| NLM RxNav (RxNorm) | Brand ↔ generic resolution |
| CDC ARPSP (Socrata) | Candida auris US-state resistance |
| Polymarket Gamma API | Health prediction markets |
| Kalshi API (events with nested markets) | Health prediction markets |
| Manifold Markets API | Health prediction markets |

## Run locally

```bash
cd world-health-dashboard
pip install -r requirements.txt
DEV_MODE=1 python3 server.py
# open http://localhost:7053
```

`DEV_MODE=1` bypasses the gateway-SSO check. In production, set
`GATEWAY_SSO_SECRET` and the gateway will inject `x-gateway-secret` on every
proxied request.

## API surface

```
GET  /                            index.html
GET  /healthz                     liveness probe
GET  /api/metrics                 metric catalog
GET  /api/countries               country index
GET  /api/globe/{metric_id}       globe layer for any indicator
GET  /api/country/{iso3}          country profile (all metrics)
GET  /api/history/{metric_id}?country=USA
GET  /api/compare?a=USA&b=DEU
GET  /api/outbreaks               WHO DON radar + globe pins
GET  /api/outbreaks/by_country/{iso3}
GET  /api/pheic                   active + history
GET  /api/h5n1                    surveillance summary
GET  /api/excess_mortality
GET  /api/excess_mortality?country=USA
GET  /api/hai                     6 AMR indicators + C. auris
GET  /api/hai/globe/{indicator}
GET  /api/hai/country/{iso3}
GET  /api/hai/c_auris
GET  /api/diseases                508-disease catalog
GET  /api/disease/{slug}
GET  /api/markets                 cross-venue health markets
GET  /api/drug/{name}             per-drug supply-chain profile
GET  /api/shortages               national overview
GET  /api/shortages/active        all 1,140 with weak-point scores
GET  /api/vulnerability           leaderboards
GET  /api/vulnerability/index     {slug → score} for atlas badges
GET  /api/vulnerability/disease/{slug}
GET  /api/country_vulnerability/globe
GET  /api/country_vulnerability/{iso3}
GET  /api/pharma_trade            top-30 + concentration
GET  /api/pharma_trade/{iso3}
```

## Production deployment

The dashboard is wired into the project's central gateway and Docker setup:

- **Gateway**: `gateway/config.json` has a `world_health` entry that maps the
  `health` subdomain to local port `7053`.
- **Docker Compose**: top-level `docker-compose.yml` includes a `world_health`
  service with a persistent `world_health-data` volume mapped to `/app/cache`
  (so disease/RxNorm/openFDA caches survive restarts). The gateway service's
  `depends_on` includes `world_health`.
- **Deploy script**: `deploy.sh` syncs `world-health-dashboard/` to the Ubuntu
  server alongside other dashboards.

### Deploy

```bash
# Push code
./deploy.sh world-health-dashboard

# On the server, restart the stack
ssh "$DEPLOY_SERVER" "cd ~/Polymarket && docker compose up -d --build world_health"

# Smoke test
curl -fsS https://health.narve.ai/healthz
```

### Stripe wiring (TODO)

`gateway/config.json` has placeholder Stripe price IDs:

```json
"world_health": {
  "monthly_cents": 999,
  "annual_cents": 9900,
  "stripe_price_monthly": "TODO_WORLD_HEALTH_STRIPE_MONTHLY",
  "stripe_price_annual": "TODO_WORLD_HEALTH_STRIPE_ANNUAL"
}
```

Replace these once you create the products in the Stripe dashboard. The
gateway reads them at boot to render the `/preview/world_health` checkout
page.

## Caching

Per-source disk caches under `cache/`:

```
cache/
  who_gho/<INDICATOR>.json       24h TTL
  world_bank/<CODE>.json          24h TTL
  who_factsheets/<slug>.json     24h TTL
  excess_mortality/...           12h TTL
  outbreaks/who_don.json          1h TTL
  markets/<source>_health.json    5min TTL
  fda_shortages/shortages.json    6h TTL
  openfda_drugs/<drug>.json       7d TTL
  fda_recalls/<drug>.json        24h TTL
  rxnorm/<drug>.json             30d TTL
  cdc_arpsp/c_auris.json         24h TTL
```

In Docker, the whole `cache/` dir is on a named volume so caches survive
container restarts.

## Phase roadmap (all done)

- **Phase 1** — Globe + 59 core metrics + country drill-down ✅
- **Phase 2** — Outbreak feeds + H5N1 + PHEICs + excess mortality ✅
- **Phase 3** — Polymarket + Kalshi + Manifold cross-venue markets ✅
- **Phase 4a** — HAI / AMR globe (WHO GLASS + GASP + CDC ARPSP) ✅
- **Phase 4b** — 508-disease atlas + WHO EML + RxNorm brand resolution ✅
- **Phase 4c** — Drug supply chains + weak-point heuristic ✅
- **Phase 4d** — Atlas-wide vulnerability rollup + Shortages dashboard ✅
- **Phase 4d-ext** — Country vulnerability composite + pharma trade flows ✅
- **Phase 5 (this)** — Gateway / Docker / deploy.sh wiring + Stripe placeholders ✅
