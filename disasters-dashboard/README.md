# Major Disasters Dashboard

Live tracker for natural disasters + Polymarket prediction-market edges.
Pairs with `climate-dashboard` (long-horizon climate trends) and
`polymarket_weather_dashboard` (short-horizon weather). This dashboard handles
the **discrete-event** middle ground: hurricanes, earthquakes, wildfires,
volcanic eruptions, severe-weather warnings, drought, and FEMA declarations.

Port: **7053**. Lives behind the gateway at `disasters.narve.ai` in production.

## What's built

| Panel | Source | Refresh |
|---|---|---|
| **Critical events strip** | GDACS + USGS PAGER + NHC + NWS roll-up | every 5 min |
| **Active named storms** | NHC `CurrentStorms.json` | 10 min |
| **NWS severe-weather alerts** | `api.weather.gov/alerts/active` | 3 min |
| **Active US wildfires + acres** | NIFC WFIGS Current GeoJSON | 15 min |
| **Today's storm reports (tornadoes/hail/wind)** | SPC daily preliminary CSV | 15 min |
| **Open EONET events** (wildfires/severe storms/volcanoes/floods) | NASA EONET v3 | 10 min |
| **Smithsonian active volcanoes** | GVP weekly bulletin RSS | 12 h |
| **Recent significant earthquakes (M5+)** | USGS FDSN event/1 | 5 min |
| **USGS PAGER alerts** | USGS `significant_month.geojson` | 10 min |
| **GDACS red/orange events globally** | GDACS RSS | 15 min |
| **US Drought Monitor** | UNL CategoricalPercent service | 12 h |
| **Year-end projections** (Atlantic storms/hurricanes/major hurricanes · M5/M6/M7+ quakes · NIFC wildfire acres · EONET wildfire counts · US tornadoes · FEMA DR declarations) | YTD + Poisson(λ) / Normal(μ,σ) | 15-60 min |
| **Polymarket disaster markets** with edge column | Polymarket Gamma | 5 min |
| **Backtest** (10 yrs of projection-vs-actual for Atlantic storms + NIFC acres) | Hand-curated annual truth tables | 1 h |

Every model emits a `lambda_remaining` (Poisson) or `(μ, σ)` (Normal) so the
market matcher can compute `P(year-end ≥ N)`, `P(year-end < N)`, or
`P(N ≤ year-end ≤ M)` for "at least N", "fewer than N", "between N and M"
markets. Threshold parsing handles unit suffixes (`million`, `thousand`,
`m`/`k`/`b`) and tornado/wildfire-specific phrasings.

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
python3 -m ingestion.usgs_quakes              # USGS quake feed + projection
python3 -m ingestion.usgs_significant         # USGS PAGER significant-events
python3 -m ingestion.eonet_events             # EONET open events
python3 -m ingestion.nhc_storms               # NHC active storms
python3 -m ingestion.nws_alerts               # NWS active alerts
python3 -m ingestion.nifc_fires               # NIFC active US wildfires + acres
python3 -m ingestion.gdacs_alerts             # GDACS red/orange globally
python3 -m ingestion.fema_declarations        # OpenFEMA YTD + recent
python3 -m ingestion.smithsonian_volcanoes    # GVP weekly bulletin
python3 -m ingestion.spc_tornadoes            # SPC reports + climo projection
python3 -m ingestion.usdm_drought             # USDM CategoricalPercent
python3 -m ingestion.polymarket_client        # Polymarket disaster markets
python3 -m analysis.market_matcher            # Market matcher with synthetic input
python3 -m analysis.backtest                  # Backtest dump
```

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /healthz` | — | Liveness probe (bypasses SSO) |
| `GET /api/health` | — | Same as `/healthz` but goes through SSO |
| `GET /api/summary` | per-feed TTL | Fan-out single payload (asyncio.gather across all upstreams) |
| `GET /api/quakes?min_magnitude=5&days=30` | 5 min | Recent quakes feed |
| `GET /api/quakes/projection?min_magnitude=6` | 15 min | YTD + Poisson year-end projection |
| `GET /api/quakes/significant?window=month` | 10 min | USGS PAGER significant-events feed |
| `GET /api/storms` | 10 min | Active NHC tropical cyclones |
| `GET /api/storms/projection` | 30 min | Atlantic-season year-end count projection |
| `GET /api/alerts?severity=Severe` | 3 min | NWS active alerts |
| `GET /api/eonet?category=all` | 10 min | EONET open events grouped by category |
| `GET /api/eonet/projection?category=wildfires` | 30 min | YTD-extrapolated year-end count |
| `GET /api/gdacs?min_alert=Orange` | 15 min | GDACS RSS filtered by severity |
| `GET /api/fires/active` | 15 min | NIFC active US wildfire incidents + acres |
| `GET /api/fires/projection` | 15 min | NIFC + climo year-end acres-burned projection |
| `GET /api/tornadoes` | 15 min | SPC preliminary daily storm reports |
| `GET /api/tornadoes/projection` | 1 h | SPC monthly-climo year-end count projection |
| `GET /api/volcanoes` | 12 h | Smithsonian GVP weekly active-volcano bulletin |
| `GET /api/drought?aoi=conus` | 12 h | USDM categorical percentages (D0-D4) |
| `GET /api/fema/recent?days=30` | 1 h | OpenFEMA recent declarations |
| `GET /api/fema/projection` | 1 h | OpenFEMA YTD + climo year-end DR projection |
| `GET /api/markets` | 5 min (markets) | Polymarket disaster markets joined with model edges |
| `GET /api/backtest?n_years=10` | 1 h | Atlantic-storm + wildfire-acres projection vs realised |

## Files

```
disasters-dashboard/
├── server.py                          FastAPI + SSO middleware + ~20 routes + asyncio.gather fan-out
├── ingestion/
│   ├── _cache.py                      In-process TTL cache shared across modules
│   ├── _http.py                       Polite User-Agent + sane-timeout HTTP helper
│   ├── usgs_quakes.py                 USGS FDSN feed + year-end projection
│   ├── usgs_significant.py            USGS significant_month.geojson + PAGER impact
│   ├── eonet_events.py                NASA EONET v3 open events + count projection
│   ├── nhc_storms.py                  NHC active tropical cyclones + Atlantic projection
│   ├── nws_alerts.py                  NWS active severe-weather alerts
│   ├── nifc_fires.py                  NIFC WFIGS active US fires + acres-burned model
│   ├── gdacs_alerts.py                GDACS RSS (severity-coded global events)
│   ├── fema_declarations.py           OpenFEMA recent + YTD + DR projection
│   ├── smithsonian_volcanoes.py       GVP weekly volcanic-activity bulletin
│   ├── spc_tornadoes.py               SPC daily reports + monthly-climo projection
│   ├── usdm_drought.py                USDM categorical percentages
│   └── polymarket_client.py           Polymarket Gamma disaster-market fetcher
├── analysis/
│   ├── poisson.py                     Pure-Python Poisson tail (p_at_least, p_between)
│   ├── market_matcher.py              Joins markets to projections → model_p + edge_pp
│   └── backtest.py                    Replays projections vs realised for prior years
├── index.html                         Single-file UI (no build step, no JS deps): crit strip + active grid + projection grid + drought bar + GDACS + significant + quakes + fires + volcanoes + markets + backtest
├── Dockerfile                         Python 3.12-slim, non-root, port 7053
├── requirements.txt                   fastapi, uvicorn, requests, defusedxml
├── .env.example                       Reference for env vars
├── .dockerignore
└── README.md                          (this file)
```

## How each model works

### Atlantic named storms / hurricanes / major hurricanes (NHC)

`nhc_storms.atlantic_season_projection()` reads `CurrentStorms.json` to get
the count of named systems **currently active** (a *lower bound* on
season-to-date — closed storms aren't counted), then layers a Poisson(λ)
prior over the remaining season days using the 1991-2020 climatological
mean of 14 named storms/year. Year-end count = `active_lower_bound + λ_remaining`.

The market matcher converts the named-storm projection to hurricane and
major-hurricane projections via climatological ratios:
  - hurricanes ≈ 50% of named storms (climo)
  - major hurricanes (Cat3+) ≈ 21% of named storms (climo)

A future iteration can swap the lower-bound with a parser of the NHC
season-archive RSS for a tight YTD count.

### Earthquakes (USGS)

`usgs_quakes.year_end_projection(min_magnitude)` fires a single FDSN query
for everything from `Jan 1` to today, counts the events, and extrapolates
linearly to year-end. The remaining-days lambda is `(YTD/days) × days_left`,
which the market matcher feeds into `p_at_least(λ, N − YTD)` for "at least
N M6+ quakes" markets.

`usgs_significant.significant_recent()` uses the curated PAGER feed which
includes alert level (red / orange / yellow / green based on expected
fatality bins), felt reports, and tsunami flag.

### US wildfire acres (NIFC)

`nifc_fires.acres_burned_year_end_projection()` combines:
  - **Active acres floor** from NIFC's WFIGS Current FeatureService (sum of
    `DailyAcres` across active US wildfire incidents)
  - **Calendar-progress prior** using a sigmoid model of cumulative-acres-by-
    day-of-year (centred on day-220, slope 25), which fits historical NIFC
    daily-cumulative curves
  - **σ** scaled by remaining-year fraction times the historical std of
    annual totals (2014-2024)

The market matcher uses `N(μ, σ)` to score "X+ acres burn in 2026" markets.

### EONET wildfires

`eonet_events.year_end_count_projection(category="wildfires")` is the
event-count version (vs the acres model above). Useful for markets keyed
on "how many fires" rather than "how many acres". Uses the same Poisson
tail over the remaining year as USGS does for quakes.

### US tornadoes (SPC)

`spc_tornadoes.ytd_tornado_projection()` uses the 1991-2020 SPC monthly
climatology (`MONTHLY_CLIMO_TORNADOES`) to compute climo-implied YTD and
remaining-year mean. Tornadoes are overdispersed vs Poisson — empirical
year-to-year std is ~250 around the climo mean of 1249 — so the matcher
uses an inflated σ rather than `sqrt(λ)`.

A v0.x upgrade would scrape the SPC annual archive for actual YTD count.

### FEMA major-disaster declarations

`fema_declarations.ytd_count_projection()` calls OpenFEMA filtered by
declaration date ≥ Jan 1, dedupes the per-state-county rows on
`disasterNumber`, and extrapolates linearly to year-end.

### Polymarket edge

`polymarket_client.fetch_disaster_markets()` pulls every market under 8
disaster tag slugs, filters by an explicit DISASTER_KEYWORDS allow-list
plus a REJECT_KEYWORDS deny-list (because Polymarket tags are noisy: the
"earthquake" tag occasionally sweeps in an "election landslide" market).

`analysis.market_matcher.enrich_markets()` then routes each matched market
through whichever model fits its title, parses the threshold via regex
(`at least N`, `fewer than N`, `between N and M`, plus `million`/`thousand`/
`m`/`k`/`b` unit suffixes with word boundaries so `"M7"` doesn't get
scaled and `"60 major"` doesn't grab the leading `m`), and computes:

    edge_pp = (model_p − implied_p) × 100

Threshold for surfacing a BUY/SELL signal in the UI is ±3 pp.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| v0   | ✓ done | NHC + USGS + EONET + NWS + Polymarket disaster matcher |
| v0.1 | ✓ done | NIFC acres-burned model · GDACS severity feed · USGS PAGER alerts |
| v0.2 | ✓ done | FEMA OpenFEMA declarations · Smithsonian GVP volcanoes · SPC tornado climo · USDM drought · backtest panel |
| v0.3 | open | NHC season-archive parser → tight Atlantic YTD (replaces lower-bound) |
| v0.4 | open | SPC annual archive scraper → live YTD tornado count |
| v0.5 | open | Geographic earthquake filtering for "California M8+ in 2026"-type markets |
| v0.6 | open | Per-volcano probability model for "Will Mount X erupt?" markets |
| v0.7 | open | Storm-track corridor overlay (re-use weather dashboard's corridor data) |
| v1.0 | open | Map view (MapLibre) overlaying active EONET + NIFC + USGS + NHC events |
| v1.1 | open | AirNow wildfire-smoke exposure for AQI markets |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7053` | Override listen port. |
| `BIND_HOST` | `0.0.0.0` | Override bind host. |

## Caveats / known limits

- **Not investment advice.** Edge values flag mispricings; they don't model
  Polymarket's bid-ask spread, on-chain settlement risk, or the fat tails of
  disaster distributions. Major hurricanes, big quakes, and bad fire years
  are well known to be **overdispersed** vs Poisson — the σ in the tornado
  model is empirically inflated for that reason; the others use raw Poisson
  σ which understates uncertainty in the tail.
- **Storm YTD is a lower bound** until the NHC archive parser lands (v0.3).
  The active-storms count only sees still-tracked systems, so the Atlantic
  named-storm projection is conservative through the middle of the season.
- **EONET wildfire counts** are events, not acres. Acres-keyed markets go
  through `nifc_fires` instead.
- **SPC YTD is climo-implied** (not observed) — the projection's σ is
  inflated to compensate, but the live YTD scraper (v0.4) will tighten it.
- **NWS alerts are US-only.** Global severe-weather alerts would require
  CAP feeds from each national met service.
- **GDACS sev coding is conservative**: a magnitude-7 quake in an
  unpopulated area scores Green; a magnitude-5 in a populated area scores
  Orange. That is intentional — GDACS is impact-not-magnitude.
- **NIFC ArcGIS feed** sometimes has stale `DailyAcres` for fires whose
  daily report hasn't posted yet. Treat the active-acres total as a
  same-day signal, not a real-time stream.
