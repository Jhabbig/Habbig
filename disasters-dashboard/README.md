# Major Disasters Dashboard

Live tracker for natural disasters + Polymarket prediction-market edges. Pairs
with `climate-dashboard` (long-horizon climate trends) and
`polymarket_weather_dashboard` (short-horizon weather). This dashboard handles
the **discrete-event** middle ground: hurricanes, earthquakes, wildfires,
volcanic eruptions, severe-weather warnings.

Port: **7053**. Lives behind the gateway at `disasters.narve.ai` in production.

## What's built (v0)

| Panel | Source | Refresh |
|---|---|---|
| **Active named storms** | NHC `CurrentStorms.json` | 10 min |
| **NWS severe-weather alerts** | `api.weather.gov/alerts/active` | 3 min |
| **Open EONET events** (wildfires / severe storms / volcanoes) | NASA EONET v3 | 10 min |
| **Recent significant earthquakes (M5+)** | USGS FDSN event/1 | 5 min |
| **Year-end projections** (Atlantic storms · M5/M6/M7+ quakes · wildfires) | YTD count + Poisson(λ) over remaining days | 15-30 min |
| **Polymarket disaster markets** with edge column | Polymarket Gamma `tag_slug=` extreme-weather, hurricane, earthquake, wildfire, tornado, flood | 5 min |

Every model emits a `lambda_remaining` parameter so the market-matcher can
compute `P(year-end count ≥ N)` for "at least N storms" / "at least N M6+
earthquakes" markets via the Poisson tail.

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
python3 -m ingestion.usgs_quakes        # USGS quake feed + projection
python3 -m ingestion.eonet_events       # EONET open events
python3 -m ingestion.nhc_storms         # NHC active storms
python3 -m ingestion.nws_alerts         # NWS active alerts
python3 -m ingestion.polymarket_client  # Polymarket disaster markets
python3 -m analysis.market_matcher      # Market matcher with synthetic input
```

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /healthz` | — | Liveness probe (bypasses SSO) |
| `GET /api/health` | — | Same as `/healthz` but goes through SSO |
| `GET /api/summary` | per-feed TTL | Single payload for the front page |
| `GET /api/quakes?min_magnitude=5&days=30` | 5 min | Recent quakes feed |
| `GET /api/quakes/projection?min_magnitude=6` | 15 min | YTD + Poisson year-end projection |
| `GET /api/storms` | 10 min | Active NHC tropical cyclones |
| `GET /api/storms/projection` | 30 min | Atlantic-season year-end count projection |
| `GET /api/alerts?severity=Severe` | 3 min | NWS active alerts |
| `GET /api/eonet?category=all` | 10 min | EONET open events grouped by category |
| `GET /api/eonet/projection?category=wildfires` | 30 min | YTD-extrapolated year-end count |
| `GET /api/markets` | 5 min (markets) | Polymarket disaster markets joined with model edges |

## Files

```
disasters-dashboard/
├── server.py                       FastAPI + gateway-SSO middleware + routes
├── ingestion/
│   ├── _cache.py                   Tiny in-process TTL cache shared across modules
│   ├── _http.py                    Polite User-Agent + sane-timeout HTTP helper
│   ├── usgs_quakes.py              USGS FDSN feed + year-end projection (Poisson)
│   ├── eonet_events.py             NASA EONET v3 open events + count projection
│   ├── nhc_storms.py               NHC active tropical cyclones + Atlantic projection
│   ├── nws_alerts.py               NWS active severe-weather alerts
│   └── polymarket_client.py        Polymarket Gamma disaster-market fetcher
├── analysis/
│   ├── poisson.py                  Pure-Python Poisson tail (no scipy)
│   └── market_matcher.py           Joins markets to projections → model_p + edge_pp
├── index.html                      Single-file UI (no build step, no JS deps)
├── Dockerfile                      Python 3.12-slim, non-root, port 7053
├── requirements.txt                fastapi, uvicorn, requests
├── .env.example                    Reference for env vars
├── .dockerignore
└── README.md                       (this file)
```

## How each model works

### Atlantic named storms (NHC)

`nhc_storms.atlantic_season_projection()` reads `CurrentStorms.json` to
get the count of named systems **currently active** (a *lower bound* on
season-to-date — closed storms aren't counted), then layers a Poisson(λ)
prior over the remaining season days using the 1991-2020 climatological mean
of 14 named storms/year. Year-end count = `active_lower_bound + λ_remaining`.

A future iteration can swap the lower-bound with a parser of the NHC
season-archive RSS for a tight YTD count. The Poisson tail handles the rest.

### Earthquakes (USGS)

`usgs_quakes.year_end_projection(min_magnitude)` fires a single FDSN query
for everything from `Jan 1` to today, counts the events, and extrapolates
linearly to year-end. The remaining-days lambda is `(YTD/days) × days_left`,
which is what `analysis.market_matcher` feeds into `p_at_least(λ, N − YTD)`
for "at least N M6+ quakes" markets.

We also keep a hand-coded climatological mean per threshold (M5: ~1500/yr,
M6: ~140/yr, M7: ~15/yr, M8: ~1/yr) so the UI can show "is this on pace?".

### Wildfires (NASA EONET)

`eonet_events.year_end_count_projection(category="wildfires")` counts open
+ closed wildfire events year-to-date and extrapolates linearly. EONET
events are coarser than acres-burned, but the count moves on the same
seasonal envelope and there's no auth required.

A v0.x upgrade would be to swap in NIFC's acres-burned record-pace model
for the markets that ask about acreage thresholds.

### Polymarket edge

`polymarket_client.fetch_disaster_markets()` pulls every market under
6 disaster tag slugs, filters by an explicit DISASTER_KEYWORDS allow-list
plus a REJECT_KEYWORDS deny-list (because Polymarket tags are noisy: the
"earthquake" tag occasionally sweeps in an "election landslide" market).

`analysis.market_matcher.enrich_markets()` then routes each matched market
through whichever model fits its title, parses the threshold count via
regex (`at least N`, `more than N`, etc.), and computes:

    edge_pp = (model_p − implied_p) × 100

Threshold for surfacing a BUY/SELL signal in the UI is ±3 pp.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| v0 | ✓ done | NHC + USGS + EONET + NWS + Polymarket disaster matcher |
| v0.1 | open | NHC season-archive parser → tight Atlantic YTD |
| v0.2 | open | NIFC acres-burned record-pace for wildfire-acreage markets |
| v0.3 | open | Storm-track corridor overlay (re-use weather dashboard's corridor data) |
| v0.4 | open | Multi-year backtest panel — replay each projection 'as of June' for the last 5 years |
| v0.5 | open | Disaster-declaration tracker (FEMA OpenFEMA API) |
| v1.0 | open | Map view (MapLibre) overlaying active EONET events on world basemap |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7053` | Override listen port. |
| `BIND_HOST` | `0.0.0.0` | Override bind host. |

## Caveats / known limits

- **Not investment advice.** Edge values flag mispricings; they don't model
  Polymarket's bid-ask spread, on-chain settlement risk, or the genuine
  fat tails of disaster distributions. Independent of every model below,
  large quakes and major hurricanes are well-known to be **overdispersed**
  vs Poisson — treat all probabilities as a starting point, not a number
  to bet against blindly.
- **Storm YTD is a lower bound** until the NHC archive parser lands (v0.1).
  The active-storms count only sees still-tracked systems, so the
  projection is conservative in the middle of the season.
- **EONET wildfire counts** are events not acres. A market keyed on
  "5 million acres burned in California" is not currently scoreable until
  the NIFC ingestion lands (v0.2).
- **USGS climatological priors** are 1990-2019 averages; recent years skew
  slightly higher at M6 and lower at M7 because of catalog completeness
  changes. Treat the climo column as orientation, not ground truth.
- **NWS alerts** are US-only. Global severe-weather alerts would require
  CAP feeds from each national met service.
