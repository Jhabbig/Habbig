# Major Disasters Dashboard

Live tracker for natural disasters + Polymarket prediction-market edges,
with a built-in map view, overdispersion-aware year-end projections, and
1/4-Kelly position sizing on every matched market.

Pairs with `climate-dashboard` (long-horizon climate trends) and
`polymarket_weather_dashboard` (short-horizon weather). This dashboard
handles the **discrete-event** middle ground: hurricanes, earthquakes,
wildfires, volcanic eruptions, severe-weather warnings, drought, FEMA
declarations, tsunami warnings, humanitarian impact.

Port: **7053**. Lives behind the gateway at `disasters.narve.ai` in production.

## What's built (v0.3)

**18 upstream feeds**, all free / no API key required (except optional AirNow).

| Panel | Source | Refresh |
|---|---|---|
| **Critical events strip** (12 severity-coded tiles) | GDACS + USGS PAGER + tsunami + NHC + NRL + NWS + NIFC + ReliefWeb | every 5 min |
| **Live world map** (SVG, equirectangular, click marker for details) | aggregated `/api/map_features` GeoJSON | every 5 min |
| **Active named storms** (Atlantic + East Pac) | NHC `CurrentStorms.json` | 10 min |
| **All-basin tropical cyclones** (W. Pac / Indian / S. Pac) | NRL Marine Met ATCF | 15 min |
| **NWS severe-weather alerts** | `api.weather.gov/alerts/active` | 3 min |
| **Flood alerts** (subset incl. flash-flood + storm-surge) | NWS flood-typed alerts | 3 min |
| **Active US wildfires + acres** | NIFC WFIGS Current GeoJSON | 15 min |
| **Today's storm reports (tornadoes/hail/wind)** | SPC daily preliminary CSV | 15 min |
| **SPC convective outlook (D1/D2/D3)** | SPC categorical-risk GeoJSON | 30 min |
| **Open EONET events** (wildfires/severeStorms/volcanoes/floods) | NASA EONET v3 | 10 min |
| **Smithsonian active volcanoes** | GVP weekly bulletin RSS | 12 h |
| **Recent significant earthquakes (M5+)** | USGS FDSN event/1 | 5 min |
| **USGS PAGER alerts** | USGS `significant_month.geojson` | 10 min |
| **GDACS red/orange events globally** | GDACS RSS | 15 min |
| **NOAA tsunami warnings** | tsunami.gov unified Atom | 5 min |
| **ReliefWeb humanitarian disasters** | ReliefWeb v1 disasters API | 1 h |
| **AirNow metro AQI** (key-gated) | AirNow zipCode/current observation | 10 min |
| **US Drought Monitor** | UNL CategoricalPercent service | 12 h |
| **Year-end projections** (Atlantic storms/hurricanes/major hurricanes · M5/M6/M7+ quakes · NIFC wildfire acres · EONET wildfire counts · US tornadoes · FEMA DR declarations) | YTD + **NB(mu, alpha)** overdispersion model + Normal(mu,sigma) for acres + 80% credible intervals | 15-60 min |
| **Polymarket disaster markets** | Polymarket Gamma joined with model edges + 1/4-Kelly sizing + trade-out deep-link | 5 min |
| **Backtest** (10 yrs of projection-vs-actual for Atlantic storms + NIFC acres) | Hand-curated annual truth tables, leave-one-out climo | 1 h |
| **Per-source health monitor** | In-process freshness/latency/success-rate scoring | 30 s |

## What's new in v0.3

- **Live world map** — inline SVG equirectangular projection with hand-built
  graticule, color-coded marker per category, click-to-open external link
  per event. No external JS / tile / CDN dependency (sandbox-safe).
- **Negative-binomial year-end count tails.** Empirical dispersion `alpha`
  per domain (storms 0.04, tornadoes 0.039, M6+ quakes 0.006, etc.) computed
  from 1980-2024 annual series. Plain Poisson **understates tail mass** for
  every overdispersed series; the matcher now uses `nb_cdf_at_least` /
  `nb_between` with the right `alpha` per market type. Closed-form via
  regularised incomplete beta (continued fraction, no scipy).
- **80% / 95% credible intervals** on every count projection, displayed
  inline on each card (`80% CI: 12 - 21 · α=0.040`).
- **1/4-Kelly position sizer** on every matched market: shows side
  (BUY YES / BUY NO), suggested bankroll percentage, and dollar size at a
  $10k bankroll.
- **Polymarket trade-out deep-links** per matched market.
- **Background pre-fetch loop** (opt-in via `DISASTERS_PREFETCH=1`) walks 23
  upstreams on staggered schedules so the dashboard returns from cache on
  every page load.
- **Disk-persisted cache** under `./cache/` (configurable via
  `DISASTERS_CACHE_DIR`). YTD counts, climo-anchored projections, and
  last-known-good responses survive process restarts. Atomic-rename writes.
- **Per-source health monitor** at `/api/sources` (and rendered inline):
  GREEN / YELLOW / RED status, latency EMA, last-ok age, success-rate over
  the last 5 calls.
- **6 new ingestion sources**: NRL all-basin tropical cyclones, NOAA tsunami
  unified feed, NWS flood subset, SPC convective outlooks D1-D3, ReliefWeb
  humanitarian disasters, AirNow metro AQI.
- **Sparklines** in projection cards for series we have hand-curated history
  for (Atlantic storms, NIFC acres, US tornadoes, FEMA DR).

## What this dashboard intentionally does NOT cover

- Long-horizon climate drift (CO₂, sea ice, GISTEMP) → `climate-dashboard`
- Day-ahead temperature & precipitation markets → `polymarket_weather_dashboard`
- Pure geopolitical news / conflict → `world-state-dashboard`

## Run locally

```bash
cd disasters-dashboard
cp .env.example .env       # DEV_MODE=1 lets you skip gateway auth
pip install -r requirements.txt
python3 server.py
# → http://localhost:7053
```

Or via Docker from the repo root:

```bash
docker compose up --build disasters
```

Smoke-test individual modules:

```bash
# Earthquakes
python3 -m ingestion.usgs_quakes
python3 -m ingestion.usgs_significant

# Tropical cyclones
python3 -m ingestion.nhc_storms
python3 -m ingestion.jtwc_pacific

# US severe weather
python3 -m ingestion.nws_alerts
python3 -m ingestion.nws_floods
python3 -m ingestion.spc_tornadoes
python3 -m ingestion.spc_outlook

# Wildfires
python3 -m ingestion.nifc_fires

# Volcanoes
python3 -m ingestion.smithsonian_volcanoes

# Global / impact feeds
python3 -m ingestion.eonet_events
python3 -m ingestion.gdacs_alerts
python3 -m ingestion.tsunami_warnings
python3 -m ingestion.reliefweb_disasters
python3 -m ingestion.airnow_aqi
python3 -m ingestion.usdm_drought
python3 -m ingestion.fema_declarations

# Polymarket + analysis
python3 -m ingestion.polymarket_client
python3 -m analysis.negbin              # NB tail sanity checks + CI band examples
python3 -m analysis.market_matcher      # End-to-end matcher with synthetic input
python3 -m analysis.kelly               # Kelly position sizer worked example
python3 -m analysis.backtest            # Multi-year backtest dump
```

## Endpoints (27)

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /healthz` | — | Liveness probe (bypasses SSO) |
| `GET /api/health` | — | Same as `/healthz` but goes through SSO |
| `GET /api/summary` | per-feed | Fan-out single payload (`asyncio.gather` across 24 upstreams) |
| `GET /api/quakes?min_magnitude=5&days=30` | 5 min | Recent quakes feed |
| `GET /api/quakes/projection?min_magnitude=6` | 15 min | YTD + NB year-end projection with CI |
| `GET /api/quakes/significant?window=month` | 10 min | USGS PAGER significant-events feed |
| `GET /api/storms` | 10 min | NHC + NRL all-basin active tropical cyclones |
| `GET /api/storms/projection` | 30 min | Atlantic-season year-end count projection + CI |
| `GET /api/alerts?severity=Severe` | 3 min | NWS active alerts |
| `GET /api/floods` | 3 min | NWS active flood-typed alerts subset |
| `GET /api/eonet?category=all` | 10 min | EONET open events grouped by category |
| `GET /api/eonet/projection?category=wildfires` | 30 min | YTD-extrapolated year-end count |
| `GET /api/gdacs?min_alert=Orange` | 15 min | GDACS RSS filtered by severity |
| `GET /api/fires/active` | 15 min | NIFC active US wildfire incidents + acres |
| `GET /api/fires/projection` | 15 min | NIFC + climo year-end acres-burned projection |
| `GET /api/tornadoes` | 15 min | SPC preliminary daily storm reports |
| `GET /api/tornadoes/projection` | 1 h | SPC monthly-climo year-end count + CI |
| `GET /api/spc/outlooks` | 30 min | SPC convective outlook D1/D2/D3 categorical risks |
| `GET /api/volcanoes` | 12 h | Smithsonian GVP weekly active-volcano bulletin |
| `GET /api/drought?aoi=conus` | 12 h | USDM categorical percentages (D0-D4) |
| `GET /api/tsunami` | 5 min | NOAA tsunami unified-feed warnings |
| `GET /api/reliefweb?limit=30` | 1 h | ReliefWeb ongoing humanitarian disasters |
| `GET /api/aqi` | 10 min | AirNow metro AQI (set `AIRNOW_API_KEY`) |
| `GET /api/fema/recent?days=30` | 1 h | OpenFEMA recent declarations |
| `GET /api/fema/projection` | 1 h | OpenFEMA YTD + NB year-end DR projection + CI |
| `GET /api/markets` | 5 min | Polymarket disaster markets joined with model edges + Kelly + trade-out URL |
| `GET /api/map_features` | 5 min | GeoJSON FeatureCollection of every geocoded active threat |
| `GET /api/sources` | live | Per-upstream health monitor + persisted-cache view |
| `GET /api/backtest?n_years=10` | 1 h | Atlantic-storm + wildfire-acres projection vs realised |

## Files

```
disasters-dashboard/
├── server.py                          FastAPI + SSO middleware + 27 routes + asyncio.gather + background prefetch
├── ingestion/
│   ├── _cache.py                      In-process TTL cache + disk-persisted fallback
│   ├── _persistence.py                Disk-backed cache (atomic-rename writes)
│   ├── _http.py                       Polite UA helper + per-source health recording
│   ├── _health.py                     Per-source GREEN/YELLOW/RED status + latency EMA
│   ├── _background.py                 Daemon-thread pre-fetch loop (opt-in)
│   ├── usgs_quakes.py                 USGS FDSN feed + year-end projection
│   ├── usgs_significant.py            USGS PAGER significant-events feed
│   ├── eonet_events.py                NASA EONET v3 open events + count projection
│   ├── nhc_storms.py                  NHC active tropical cyclones + Atlantic projection
│   ├── jtwc_pacific.py                NRL ATCF all-basin tropical cyclones (NEW v0.3)
│   ├── nws_alerts.py                  NWS active severe-weather alerts
│   ├── nws_floods.py                  NWS flood-typed alerts subset (NEW v0.3)
│   ├── nifc_fires.py                  NIFC WFIGS active US fires + acres-burned model
│   ├── gdacs_alerts.py                GDACS RSS (severity-coded global events)
│   ├── fema_declarations.py           OpenFEMA recent + YTD + DR projection
│   ├── smithsonian_volcanoes.py       GVP weekly volcanic-activity bulletin
│   ├── spc_tornadoes.py               SPC daily reports + monthly-climo projection
│   ├── spc_outlook.py                 SPC convective outlook D1/D2/D3 (NEW v0.3)
│   ├── tsunami_warnings.py            NOAA tsunami unified Atom feed (NEW v0.3)
│   ├── reliefweb_disasters.py         ReliefWeb v1 disasters API (NEW v0.3)
│   ├── airnow_aqi.py                  AirNow metro AQI (NEW v0.3, key-gated)
│   ├── usdm_drought.py                USDM categorical percentages
│   └── polymarket_client.py           Polymarket Gamma disaster-market fetcher
├── analysis/
│   ├── poisson.py                     Pure-Python Poisson tail (p_at_least, p_between)
│   ├── negbin.py                      Negative-binomial tail + CI bands (NEW v0.3)
│   ├── kelly.py                       Kelly-criterion position sizer (NEW v0.3)
│   ├── map_features.py                GeoJSON FeatureCollection builder (NEW v0.3)
│   ├── market_matcher.py              Joins markets to projections → model_p + edge + Kelly + trade URL
│   └── backtest.py                    Replays projections vs realised for prior years
├── static/                            (reserved for vendored map JS, sandbox-blocked in v0.3)
├── index.html                         Single-file UI: SVG map + crit strip + projections w/ sparklines + Kelly + sources + backtest
├── Dockerfile                         Python 3.12-slim, non-root, port 7053
├── requirements.txt                   fastapi, uvicorn, requests, defusedxml
├── .env.example                       Reference for env vars
├── .dockerignore
└── README.md                          (this file)
```

## How each model works (summary)

### Negative-binomial year-end counts

For overdispersed count series (storms, tornadoes, M6+ quakes, FEMA DR,
EONET wildfires) we use NB(mu=lambda_remaining + ytd, alpha) instead of
plain Poisson(lambda). The dispersion `alpha` is hand-tuned from
1980-2024 historical year-to-year variance:

    Atlantic named storms     alpha = 0.040
    Atlantic hurricanes       alpha = 0.060
    Atlantic major hurricanes alpha = 0.090
    US tornadoes              alpha = 0.039
    Global M5+ quakes         alpha = 0.0007  (very low - large numbers)
    Global M6+ quakes         alpha = 0.006
    Global M7+ quakes         alpha = 0.020
    EONET wildfires           alpha = 0.030
    FEMA DR                   alpha = 0.020

`Var(X) = mu + alpha * mu^2`, so alpha=0 recovers Poisson. P(X >= k) is
computed via the regularised incomplete beta function with a Lentz
continued-fraction expansion (no scipy).

Compared to Poisson, NB widens the tail meaningfully in the +/- 1 σ region:
on Atlantic named storms with mu=14, P(>=20) under NB(0.04) is 12.3%
vs Poisson's 7.6%.

### 80% / 95% credible intervals

`nb_quantile_band(mu, alpha, ci=0.80)` returns `(lower_k, upper_k)` such
that P(lower_k <= X <= upper_k) >= ci. Implementation walks integer k
upward and watches the tail crossings.

### Wildfire acres (NIFC)

Same calendar-progress sigmoid model as v0.2 but the matcher now uses the
projection-side N(mu, sigma) tail directly via the shared
`market_matcher._score_wildfire_acres_market` helper.

### Polymarket edge & Kelly sizing

`analysis/market_matcher.py` parses the threshold via regex (with
word-boundary-gated unit suffixes), evaluates the right model, computes
`edge_pp = (model_p - implied_p) * 100`, and runs the result through
`analysis/kelly.position_size()` to emit:

    side: "YES" or "NO"
    kelly_full: full Kelly fraction (clamped to [-1, 1])
    kelly_quarter: 1/4 Kelly (clamped to [-25%, +25%])
    suggested_dollars_at_10k: 1/4-Kelly $ at a $10k bankroll

Threshold for surfacing a BUY/SELL signal in the UI is +/- 3 pp.

### Background pre-fetch + disk persistence

`ingestion/_background.start(jobs)` spawns a single daemon thread that
walks a list of (name, callable, interval_s) tuples. Each callable is
already cache-aware via `ingestion/_cache.cached`, so calling it
populates the in-memory cache *and* writes to disk via
`ingestion/_persistence`. On cold start the cache layer first tries the
disk file, so the dashboard returns warm data immediately.

The loop is opt-in: `DISASTERS_PREFETCH=1`. Disabled by default so smoke
tests don't blast live upstreams during CI.

### Per-source health

`ingestion/_http.get` records every upstream call into `ingestion/_health`
with: latency, HTTP status, ok/fail. The dashboard's `/api/sources`
endpoint surfaces a GREEN/YELLOW/RED status per source plus latency EMA
and success rate. Useful for "is GDACS down again?" debugging without
opening logs.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| v0   | ✓ done | NHC + USGS + EONET + NWS + Polymarket disaster matcher |
| v0.1 | ✓ done | NIFC acres-burned model + GDACS severity feed + USGS PAGER |
| v0.2 | ✓ done | FEMA + GVP + SPC tornadoes + USDM + backtest panel |
| v0.3 | ✓ done | SVG world map + 6 new feeds (NRL/tsunami/floods/SPC outlooks/ReliefWeb/AirNow) + NB overdispersion + Kelly + background pre-fetch + disk persistence + per-source health |
| v0.4 | open | NHC season-archive parser → tight Atlantic YTD (replaces lower-bound) |
| v0.5 | open | SPC annual archive scraper → live YTD tornado count |
| v0.6 | open | Geographic earthquake filtering for "California M8+ in 2026"-type markets |
| v0.7 | open | Per-volcano probability model for "Will Mount X erupt?" markets |
| v0.8 | open | Storm-track corridor overlay (re-use weather dashboard's corridor data) |
| v1.0 | open | Vendored Leaflet map view with OSM tiles (sandbox-permitting) |
| v1.1 | open | WebSocket push for sub-minute updates on critical events |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7053` | Override listen port. |
| `BIND_HOST` | `0.0.0.0` | Override bind host. |
| `DISASTERS_PREFETCH` | unset | Set `1` to enable the background pre-fetch loop. |
| `DISASTERS_CACHE_DIR` | `./cache/` | Override the disk-cache directory. |
| `AIRNOW_API_KEY` | unset | AirNow API key (free at airnowapi.org). Without it the AQI panel renders a placeholder. |

## Caveats / known limits

- **Not investment advice.** Edge values flag mispricings; they don't
  model Polymarket bid-ask, on-chain settlement risk, or fat-tail event
  variance beyond what the NB dispersion captures. **The 1/4-Kelly sizer
  is a guideline, not a recommendation** — always sanity-check a position
  against your bankroll and risk appetite.
- **Atlantic storm YTD is a lower bound** until the NHC archive parser
  lands (v0.4). The active-storms count only sees still-tracked systems,
  so the projection is conservative through the middle of the season.
- **EONET wildfire counts** are events, not acres. Acres-keyed markets go
  through `nifc_fires` instead.
- **SPC YTD is climo-implied** (not observed) — the projection's σ is
  inflated to compensate, but the live YTD scraper (v0.5) will tighten it.
- **NWS alerts are US-only.** Global severe-weather alerts would require
  CAP feeds from each national met service.
- **Map view uses an SVG graticule** instead of OSM tiles because the
  build sandbox blocks external CDNs (unpkg, cdnjs, jsdelivr all 403'd).
  The graticule is fine for showing where events cluster but lacks
  cartographic detail. v1.0 will swap in vendored Leaflet + OSM tiles
  when the sandbox allows.
- **AirNow requires a key** for the JSON observations endpoint. Without
  one the AQI panel still renders but says "set AIRNOW_API_KEY to populate".
- **NB dispersion alpha is hand-tuned** against 1980-2024 NOAA/SPC/USGS
  series. Updating the empirical alpha annually is a one-line change in
  `analysis/negbin.ALPHA`.
