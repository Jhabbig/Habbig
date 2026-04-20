# narve Pulse — README

**Status:** v1.1 (preliminary, expanded) — built locally, not yet deployed to the production server.

narve Pulse is the gateway-internal dashboard that forecasts human happiness
and the second-order metrics each shift drives downstream. It lives at `/pulse`
on the apex (`narve.ai/pulse` once deployed) and is included with every plan —
no per-dashboard subscription gate.

## What it does

- **Pulse Index** — a 0-100 composite headline score across **21 wellbeing
  metrics** in 7 categories (happiness, connection, mental health, meaning,
  material security, time/attention, daily friction). Component weights sum to
  exactly **1.00**.
- **21 driver charts** — each with 50-120 years of historical data + a
  linear-extrapolation forecast to 2030, confidence band, era-event annotations,
  and a "earliest → today" delta card.
- **"What this moves" cards** — every chart pairs the metric with 3-6 historical
  downstream consequences (sectors, behaviors, markets) marked with direction,
  magnitude, and lag estimate. These are observed historical associations, not
  causal claims.
- **Time Machine** — a global year scrubber (1900 → 2030) that re-snaps every
  chart and the headline number in lockstep. Drag past 2024 to enter forecast
  mode.
- **"What's Moving Now"** — top 8 high-magnitude downstream effects across the
  whole dataset, sorted by Pulse weight.
- **Historical Misery Playbook** — four documented historical episodes
  (Great Depression, Stagflation, GFC, COVID) showing exactly what dropped,
  what rose, and what surprised, with hard numbers on every line. The section
  that makes narve look like a forecaster with receipts, not a guesser.

## Architecture

Unlike the other six narve dashboards (which are separate microservices on
ports 8888 / 5050 / 7050 / 8000 / 8051 / 8052 reverse-proxied through the
gateway), narve Pulse is **rendered directly by the gateway** from a static
curated dataset. No new process to deploy, no new port, same auth, same theme.

```
/Users/shocakarel/Habbig/gateway/
├── server.py
│   ├── @app.get("/pulse")  ← new route, auth-gated, renders pulse.html
│   └── my_dashboards()     ← injects the 7th "narve Pulse" card on the hub
└── static/
    ├── pulse.html          ← page template (uses gateway.css + pulse.css shell)
    ├── pulse.css           ← scoped .pulse-* styles, monochrome theme tokens
    ├── pulse.js            ← SVG chart renderer + Pulse Index calc + Time Machine
    └── pulse_data/
        └── pulse_metrics.json   ← curated dataset, 15 metrics, 1900-2030
```

## Data sources (v1.1 curated)

All values are public-domain historical statistics, curated by hand from the
sources below. v1.2 will replace curation with live API pulls. Each chart card
in the UI displays its own source link.

| Metric | Source |
|---|---|
| % "very happy" (US) | General Social Survey (GSS) |
| Cantril ladder (US) | World Happiness Report / Gallup World Poll |
| Marriage rate (US) | CDC NCHS / NVSS |
| Divorce rate (US) | CDC NCHS / NVSS |
| Total fertility rate (US) | CDC NCHS + UN World Population Prospects 2024 |
| Median age first marriage (M, US) | US Census Bureau MS-2 tables |
| Adults with no close friends (US) | Survey Center on American Life / GSS |
| Single-person households (US) **[new v1.1]** | US Census Bureau HH tables |
| Couples who met online (US) **[new v1.1]** | Stanford "How Couples Meet" |
| Suicide rate (US) | CDC WONDER / NCHS |
| Antidepressant use (US) | CDC NHANES Data Briefs |
| Teen persistent sadness (US) **[new v1.1]** | CDC YRBS (high schoolers) |
| Weekly religious attendance (US) | Gallup / Pew Religious Landscape Study |
| Generalized social trust (US) | General Social Survey (GSS) |
| Trust in federal government (US) **[new v1.1]** | Pew / ANES stitched series |
| Sleep duration (US) | Gallup Sleep Survey / CDC NHIS |
| Daily screen time (US) | Pew / DataReportal Digital |
| Weekly meeting hours (US) **[new v1.1]** | Microsoft Work Trend Index / Reclaim.ai |
| "$400 emergency cover" (US) | Federal Reserve SHED |
| Housing affordability (US) **[new v1.1]** | NAR Housing Affordability Index |
| Robocalls received (US) | YouMail Robocall Index / FCC |

## Pulse Index methodology

The Pulse Index is a **weighted average of normalized component metrics**. For
each year:

1. Each metric is **densified** to year-by-year values via linear interpolation
   between sparse data points (e.g., GSS biennial → annual).
2. Each metric is **normalized to 0-100** within its own observed historical
   min/max range.
3. **Negative-polarity metrics** (suicide rate, divorce rate, loneliness, etc.)
   are inverted so 100 always represents "good for happiness."
4. The Index is the **weighted mean** of all components present for that year,
   using the `pulse_weight` field in `pulse_metrics.json`. Years where less
   than 55% of total weight is present are skipped (mostly pre-1972).

Current weights (v1.1) — rebalanced to sum to exactly 1.00:

```
Happiness self-reports        30%  (GSS happiness 18% + Cantril 12%)
Connection                    25%  (no-close-friends 6% + marriage 5% + fertility 5%
                                     + divorce 4% + single-person HH 2% + age first marriage 2%
                                     + met online 1%)
Mental health                 16%  (suicide 7% + antidepressants 5% + teen sadness 4%)
Meaning & trust               11%  (religious attendance 4% + social trust 4%
                                     + trust in government 3%)
Time & attention              10%  (sleep 4% + screen time 3% + meeting hours 3%)
Material security              6%  ($400 emergency 3% + housing affordability 3%)
Daily friction                 2%  (robocalls 2%)
                            ----
                             100%  ✓
```

## Forecast methodology

Each chart's forecast is a **simple, transparent statistical extrapolation**.
The exact method is named on every chart card. v1 uses:

- **Linear regression** on the last 15-25 years of data for stable trends
- **Log-linear with saturation** for adoption-curve metrics (e.g., screen time)
- **Cited published forecasts** for demographic series (UN WPP cross-checks
  fertility forecasts)

Confidence intervals are hand-set at v1 to reflect typical CI widths for these
series. v1.1 will compute proper bootstrap CIs from the regression residuals.

Forecast horizon: **2030** for all metrics. Anything beyond 2030 we cite from
authoritative sources only — narve does not extrapolate 30 years on its own.

## Honest v1.1 limitations

- **Curated data, not live.** Historical points were entered by hand from
  public sources. Some values may be ±0.1-0.5 off from the latest authoritative
  release. v1.2 will replace curation with live API pulls.
- **Simple forecast methods.** Linear / log-linear extrapolation is honest and
  defensible but not state-of-the-art. v3 plans proprietary structural models.
- **No backtests yet.** v2 will publish out-of-sample backtests on every chart
  (fit on data through 2015, score against 2015-2025 actuals).
- **US-primary.** Most metrics are US data with global context where the data
  is clean (UN, WHR). v2 expands global coverage.
- **21 metrics.** Up from 15 in v1.0. v1.2 targets: dating app penetration,
  notification volume, commute time, air quality by city, household debt, loneliness
  by age cohort.
- **Misery Playbook deltas are estimates.** The `pulse_index_delta_est`
  values on each historical episode are qualitative best-reads, not
  back-computed from the v1.1 index (some of the underlying series didn't
  exist during 1929-33). v2 will back-compute them from the available
  series for each episode.
- **No live prediction-market pill yet.** v2 will pull love/demographics
  markets from the existing narve Polymarket / Kalshi / Manifold pipeline and
  display them next to narve's own forecasts.

## Roadmap

- **v1.0** — 15 metrics, Pulse Index, Time Machine, consequence cards,
  "What's Moving Now", methodology page. *(shipped local)*
- **v1.1** *(this release)* — 21 metrics, weights rebalanced to 1.00,
  Historical Misery Playbook section (Depression, Stagflation, GFC, COVID),
  6 new metrics (trust in government, single-person households, met online,
  teen sadness, meeting hours, housing affordability).
- **v1.2** — Quarterly automated refresh script pulling latest values from
  FRED / BLS / CDC / Census / GSS APIs. Regenerate JSON. "Last updated" per
  chart reflects actual upstream release dates.
- **v2.0** — Out-of-sample backtests on every chart with R² and accuracy
  scores published. Live Polymarket / Kalshi market pills. Cohort lens for
  generational comparisons. Back-computed Misery Playbook index deltas.
- **v3.0** — Proprietary structural models replacing linear extrapolation for
  the Pulse Index drivers. Bayesian update on each new release. Personal
  inputs layer (optional, opt-in only).

## Local development

```bash
cd ~/Habbig/gateway
fuser -k 7777/tcp 2>/dev/null; sleep 1
nohup python3 -m uvicorn server:app --host 127.0.0.1 --port 7777 > /tmp/pulse.log 2>&1 &
open http://127.0.0.1:7777/pulse
```

The `dev` mode bypasses the auth gate on localhost, so you can hit `/pulse`
directly without logging in.

## Smoke test results (v1.1)

```
=== /pulse                       status=200 (auth bypassed on localhost)
=== /pulse.css                   status=200 bytes=17579
=== /pulse.js                    status=200 bytes=33017
=== pulse_metrics.json           status=200 bytes=47386
=== /dashboards (Pulse card)     present
=== metrics count                21
=== playbook episodes            4
=== pulse_weight total           1.00
=== template token substitution  no orphans
=== page markers                 10 (playbook + charts grids)
=== server log                   no errors / warnings
=== node --check pulse.js        OK
=== python -m ast server.py      OK
=== python json.load metrics     OK
```

## Disclaimer

narve Pulse is a forecasting product based on public statistical data. It is
not financial, investment, medical, or legal advice. Forecasts are uncertain.
Past correlations between happiness drivers and downstream metrics do not
guarantee future relationships. Each consequence card describes observed
historical associations, not causal claims.
