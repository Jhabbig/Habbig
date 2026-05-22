# Climate Change Dashboard (v4)

Long-horizon climate prediction markets with model-derived edges.

Listens on `:7052`. Subdomain: `climate.narve.ai` (registered in `gateway/config.json`).

## What's new in v4

The dashboard has been turned from "neat hobby site" into a forecast-honest,
multi-indicator, polished tool with 12+ indicator cards, IPCC scenario
matching, top-opportunities ranking, and an RSS feed of high-edge markets.
Major moves:

### Backend

- **Modular `app/` package** — the 1300-line `server.py` was split into
  `app/fetchers/<source>.py` (one per data source, each exposing a pure
  `parse(text)` and a cached `fetch()`) and `app/models/<indicator>.py`.
  `server.py` is now ~250 lines of routes.
- **Six more atmospheric species and indicators**: SF₆ (electrical-tracer
  GHG, NOAA GML); N₂O (NOAA GML); ocean heat content 0-2000 m (NOAA NCEI);
  global mean sea level (NOAA STAR); NH snow cover extent (Rutgers Global
  Snow Lab); country-level CO₂ emissions (Our World in Data).
- **Derived radiative-forcing model** — composes CO₂/CH₄/N₂O/SF₆ atmospheric
  concentrations into total W/m² above pre-industrial via the IPCC AR5 /
  Myhre 1998 formulas (including the CH₄ ↔ N₂O absorption-band overlap
  term). Includes "effective CO₂ ppm" framing.
- **IPCC SSP scenarios** — hard-coded AR6 anchor points for SSP1-2.6 /
  SSP2-4.5 / SSP3-7.0 / SSP5-8.5 trajectories of CO₂ and temperature out
  to 2100, with linear interpolation between anchors and a current-pace
  matching function that picks the closest scenario to today's reading.
- **HTTP-mocked integration tests** — patches `app.http.get` with fixture
  responses and exercises every `/api/*` endpoint, so future upstream URL
  changes surface in CI rather than silently in production.

### Frontend

- **Indicator overlay chart** — z-score-normalized multi-series chart with
  toggle pills. Click pills to add/remove series; hover for raw-unit values;
  ENSO El Niño / La Niña years shaded behind the data.
- **Long-term outlook chart** — observed history + all four SSP trajectories
  on the same real-units axis (temperature or CO₂). Toggle between metrics.
- **Fan charts + calibration badges** on the projection cards — last 60
  months of observations plus a forecast cone widening to (μ ± 1.28·σ) at
  year-end. Each card shows "Model error ±X over last N yrs" from the
  existing backtest.
- **"Current pace ≈ SSP X" pills** on the temperature and CO₂ cards.
- **"Today's highlights" panel** — auto-derived chips: temperature record
  streaks, 12-month GHG changes, Arctic sea-ice DOY rank, ENSO state +
  streak length.
- **Top-emitters table** — top-10 CO₂ emitting countries with per-capita,
  share-of-global, and decade-comparison summary.
- **Markets table 2.0** — filterable (topic / min-edge / min-liquidity /
  scored-only), sortable, with a top-opportunities ribbon above showing
  the 3 best edges weighted by liquidity. URL hash captures filter/sort
  state for shareable views.
- **Kelly position sizing** — every market row shows the recommended
  fraction (¼K / ½K / fullK) for the +EV side; with a bankroll set,
  displayed as $-amounts. Click any row to expand a detail panel with
  Kelly breakdowns at $1k / $10k / $100k. Bankroll stays in localStorage.
- **Quick-nav** in the header for jumping between sections on a long page.
- **Graceful unavailable state** for the best-effort upstream sources
  (sea level, OHC, snow cover) — if a URL has shifted, the card explicitly
  says so instead of silently disappearing.
- **CSV export** for markets, backtest, overlay, scenarios, and emitters.
- **Accessibility + mobile** — ARIA labels, `aria-pressed`, `aria-live`,
  keyboard-activatable buttons, and a `@media (max-width: 600px)` layout.

### Shareability

- **`/snapshot.txt`** — plain-text one-pager (highlights → temperature →
  GHGs → forcing → sea ice → ENSO → top emitters → methodology link).
  Suitable for posting, piping, or scheduling.
- **`/feed.xml`** — RSS 2.0 feed of today's highlights.
- **`/feed.xml?kind=opportunities&min_edge=N&min_liq=N`** — RSS feed of
  Polymarket climate markets where the model and the market disagree by
  at least N percentage points, ranked by |edge|.
- **`/methodology` page** — plain-English description of every model.

### Tests

- **85 pytest cases** + a **jsdom headless-DOM smoke test** in CI.
  Coverage: parsers (with realistic upstream-format fixtures), math
  helpers, projection models, threshold-prob monotonicity, market regex
  routing, the Kelly formula, the ENSO segmenter, calibration summaries,
  radiative forcing, IPCC scenarios, country emissions, snow-cover and
  ocean-heat parsers, the snapshot/feed endpoints, and several
  regressions for bugs found in the v4 code review.

### What was added in v3

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
| Atmospheric N₂O | NOAA GML globally-averaged (`n2o_mm_gl.csv`) | monthly |
| Atmospheric SF₆ | NOAA GML globally-averaged (`sf6_mm_gl.csv`) | monthly |
| Arctic + Antarctic sea ice extent | NSIDC Sea Ice Index G02135 v4.0 | daily |
| Global SST 60°S–60°N | NOAA OISST 2.1 via Climate Reanalyzer JSON | daily |
| Ocean heat content (0-2000 m) | NOAA NCEI yearly anomaly | annual |
| Global mean sea level | NOAA STAR satellite altimetry | monthly |
| NH snow cover extent | Rutgers Global Snow Lab | monthly |
| ENSO state | NOAA CPC Oceanic Niño Index (`oni.data`) | monthly |
| Country-level CO₂ emissions | Our World in Data (`owid-co2-data.csv`) | annual |
| Markets | Polymarket Gamma `tag_slug=climate-change` (+ siblings) | live |
| IPCC SSP trajectories | Hard-coded AR6 anchor points | static |

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

## Run tests

```bash
pip install pytest
python3 -m pytest tests/
```

85 tests cover parsers (with realistic upstream-format fixtures), math
helpers, projection models, threshold-probability monotonicity, the
market-scoring regex routing, the Kelly formula, the ENSO segmenter,
calibration summaries, regressions for the v4 bug-review findings,
country-emissions parsing, the radiative-forcing math, IPCC scenario
interpolation + matching, ocean-heat / sea-level / snow-cover parsers,
the /snapshot.txt and /feed.xml endpoints, and end-to-end integration
with HTTP-mocked upstream fetchers.

## Headless-DOM smoke test

A jsdom-based check loads the real `static/index.html`, mocks all the
`/api/*` responses, runs the inline JS, and reports any runtime errors
plus the length of each rendered section. Catches a class of bugs the
parse-only check misses (missing element IDs, race conditions, type
errors on null returns from `getElementById`, etc).

```bash
npm install --no-save --no-package-lock jsdom@^25
node scripts/check_dom.js
```

CI runs this on every push under the `headless-dom-check-climate-dashboard`
job — failures there mean the dashboard would have errored in a browser.

## Endpoints

- `GET /api/summary` — single page-load payload: temperature + CO₂ + CH₄ + N₂O + ice projections, threshold probabilities, ENSO state, and per-series calibration summaries
- `GET /api/highlights` — auto-derived chips (records, streaks, 12-month deltas, ENSO regime)
- `GET /api/methodology` — structured plain-English description of every model + backtest
- `GET /api/markets` — climate markets with model edges (Arctic + Antarctic sea ice, year-end record, anomaly threshold, CO₂ / CH₄ / N₂O threshold)
- `GET /api/temperature` — full GISTEMP series + projection
- `GET /api/co2` — full Mauna Loa series + projection (with residual std)
- `GET /api/methane` — full NOAA GML CH₄ series + projection + threshold probabilities
- `GET /api/n2o` — full NOAA GML N₂O series + projection + threshold probabilities
- `GET /api/sea-ice` — Arctic + Antarctic recent daily series + annual extremes + record check
- `GET /api/sst` — Climate Reanalyzer JSON (whole multi-year structure)
- `GET /api/regime` — ONI + ENSO state
- `GET /api/backtest` — last 5 completed years, projection-vs-actual for temperature / CO₂ / CH₄ / N₂O models, with per-series calibration summary (MAE, RMSE, bias)
- `GET /api/sf6` — full NOAA GML SF₆ series + projection
- `GET /api/forcing` — combined radiative forcing in W/m² + effective CO₂ ppm + per-gas breakdown
- `GET /api/scenarios` — IPCC SSP trajectories + "current pace ≈ SSP X" match
- `GET /api/emissions` — country-level CO₂ emissions (top emitters + global decade-change)
- `GET /api/ocean-heat` — NOAA NCEI 0-2000m heat content yearly anomaly
- `GET /api/sea-level` — NOAA STAR global mean sea level
- `GET /api/snow-cover` — Rutgers NH snow cover extent
- `GET /snapshot.txt` — plain-text dashboard snapshot
- `GET /feed.xml` — RSS feed of today's highlights
- `GET /feed.xml?kind=opportunities&min_edge=N&min_liq=N` — RSS of high-edge climate markets
- `GET /api/health` — liveness + running git commit

Static pages: `/` (dashboard), `/methodology` (model docs).

## Container

```bash
docker build -t climate-dashboard .
docker run --rm -p 7052:7052 climate-dashboard
```

## Notes

- This dashboard intentionally does **not** cover natural disasters or short-term weather — those live in `polymarket_weather_dashboard` and (planned) the Major Disasters dashboard.
- The Polymarket fetcher reuses the same gamma pattern as the weather dashboard, but applies a stricter climate-keyword filter so it doesn't sweep in tornado/hurricane markets.
- ENSO state (ONI) is shown as a banner because it's the single most useful short-term covariate for both temperature and SST markets.
