# Voter Sentiment Dashboard

How American voters feel and how their day-to-day lives are going — at a
glance.

Listens on `:7053`. Subdomain: `mood.narve.ai` (registered in
`gateway/config.json`).

## What it shows

A composite **Voter Mood Index** (0–100) on top, then nine cards covering
the indicators that drive it:

| Card | Series | Cadence | Why voters care |
|---|---|---|---|
| Consumer sentiment (UMich) | `UMCSENT` | monthly | Direct survey of how people feel about the economy. |
| Headline inflation (CPI YoY) | `CPIAUCSL` | monthly | Pain at every checkout. |
| Unemployment rate | `UNRATE` | monthly | Are jobs available? |
| Real wages (hourly, YoY) | `CES0500000003` ÷ `CPIAUCSL` | monthly | Are paychecks keeping up? |
| Gas — US regular avg | `GASREGW` | weekly | The most-watched price in America. |
| 30-year fixed mortgage | `MORTGAGE30US` | weekly | Housing affordability. |
| Misery index | derived | monthly | Unemployment + CPI YoY (Okun). |
| Initial jobless claims | `ICSA` | weekly | Leading job-loss signal. |
| Personal saving rate | `PSAVERT` | monthly | How much slack households have. |

An **Election-Cycle Forecast** sits right under the mood banner — the
flagship signal. It runs an OLS regression of incumbent-party House seat
change on UMich consumer sentiment in April of each midterm year
(1978-2022, n=12), then projects the current sentiment reading forward to
the next midterm with a 90% prediction interval. The scatter plot to the
right shows every historical cycle, the regression line, the prediction
band, and where today sits in that distribution. R² is published openly —
it's lower than political narratives imply, and the dashboard is up-front
about that uncertainty.

A **Vibecession + Approval row** sits below the forecast:

- **Vibecession index** — `sentiment_percentile − fundamentals_percentile`,
  both measured monthly against the prior 20 years. Positive = voters feel
  better than the data suggests; negative = voters feel worse. Sparkline
  shows the gap over the last ~10 years with red/green fills for negative /
  positive territory. No other free dashboard publishes this number.
- **Presidential approval** — weighted 4-week rolling net approval from
  FiveThirtyEight's archived approval-polls CSV (mirrored on the
  `fivethirtyeight/data` GitHub repo). 538 stopped updating in mid-2024;
  if the latest poll is > 60 days old, the card surfaces a "historical"
  pill so the staleness is obvious.

A **state-level "Where it hurts" panel** below the national cards: a
swing-state strip (PA, MI, WI, AZ, GA, NV, NC) plus the five most-stressed
and five least-stressed states, each ranked by where its current
unemployment rate sits on its own 20-year percentile distribution.

Plus a Polymarket section filtered to politics / midterm / approval / "right
track" markets.

## Voter Mood Index

Equal-weighted mean of five 0–1 sub-scores, then ×100:

- **sentiment** — UMich percentile vs the last 20 years
- **jobs** — `1 −` unemployment percentile vs the last 20 years
- **inflation** — `1 −` CPI-YoY percentile vs the last 20 years
- **real wages** — sigmoid of real-wage YoY (positive = better)
- **gas** — `1 −` price percentile vs the last 5 years (voters care about
  recent pain, not the 1990s baseline)

The index is intentionally backward-looking and descriptive — it summarises
how voters' recent lived experience compares to the baseline of the past
couple of decades. It is **not** a forecast.

## Data sources

| What | Source | Cadence |
|---|---|---|
| Consumer sentiment | University of Michigan / Surveys of Consumers (via FRED) | monthly |
| Unemployment, CPI, earnings, savings | BLS & BEA (via FRED) | monthly |
| Gas prices | EIA (via FRED) | weekly |
| Mortgage rates | Freddie Mac PMMS (via FRED) | weekly |
| Initial claims | DOL (via FRED) | weekly |
| Recession dating | NBER (via FRED) | monthly |
| Markets | Polymarket Gamma — `politics`, `us-elections`, `midterms`, `2028-election`, `presidential-approval`, etc. | live |

All upstream sources are free and require no API key. FRED data is fetched
via the public `fredgraph.csv?id=...` endpoint with a 6–24h TTL per series.

## Run locally

```bash
pip install -r requirements.txt
python3 server.py
# → http://localhost:7053
```

## Endpoints

- `GET /api/summary` — single page-load payload: mood index, misery, real
  wages, every indicator card with sparkline data
- `GET /api/markets` — politics markets filtered to sentiment-relevant
  questions
- `GET /api/mood` — composite voter-mood index with per-component breakdown
- `GET /api/states` — state-level unemployment + own-history percentile
  stress score (50 states + DC, plus swing-state strip)
- `GET /api/election-cycle` — historical mood → midterm seat-change OLS
  regression with current implied seat change for the incumbent's party
  (90% prediction interval)
- `GET /api/approval` — weighted weekly presidential approval aggregate
  from FiveThirtyEight's archived CSV (1945-2024 depending on the
  president), 52-week sparkline + 4-week rolling smoothed net
- `GET /api/vibecession` — the vibecession index: sentiment percentile
  minus fundamentals percentile, monthly history + verbal flavor
- `GET /api/election-cycle/backtest` — leave-one-out cross-validation of
  the election-cycle regression: per-cycle predicted vs actual seats,
  plus aggregate MAE / RMSE / out-of-sample R²
- `GET /api/csv/<series_id>` — passthrough CSV download for any tracked
  FRED series (national or state-level)
- `GET /methodology` — long-form methodology page with formulas, sources,
  and the live LOO backtest table
- `GET /embed/<card>` — iframe-friendly single-card widget; supported
  cards: `mood`, `forecast`, `vibecession`, `approval`
- `GET /api/pollster-scorecard` — top/bottom pollsters by predictive
  plus-minus from FiveThirtyEight's archived ratings CSV
- `GET /api/global-mood` — same mood-composite formula computed for
  six countries (US, UK, Germany, France, Canada, Japan) from OECD
  consumer-confidence + harmonised unemployment + CPI via FRED
- `GET /api/partisan-sentiment` — UMich consumer sentiment by
  respondent partisanship (R / D / I + partisan-gap) from quarterly
  Table 32 releases
- `GET /api/right-track` — right-direction vs wrong-direction polling
  aggregate across major pollsters
- `GET /api/revisions` — persistent-snapshot DB feed of FRED
  revisions we've detected, plus stats on observations tracked
- `GET /api/series/<id>` — raw FRED series (any of the IDs above) with
  computed YoY where applicable
- `GET /api/health` — liveness

## Container

```bash
docker build -t voter-dashboard .
docker run --rm -p 7053:7053 voter-dashboard
```

## What's next

See [`ROADMAP.md`](ROADMAP.md) for the v2 plan — partisan / demographic
splits of UMich sentiment, state-level mood map, and an
election-cycle regression that turns the mood index into an implied
seat-change forecast for the incumbent's party.

## Notes

- The mood index intentionally weights its five components equally. Picking
  weights is editorial, and any choice rewards people who agree with you and
  annoys people who don't. Equal weights make the assumption transparent.
- The polymarket filter is conservative — markets only show up if their
  title contains a sentiment-relevant keyword (`approval`, `midterm`,
  `right track`, `recession`, `inflation`, etc.). If you don't see a market
  you expect, add the keyword to `SENTIMENT_KEYWORDS` in `server.py`.
- Approval-rating numbers from Gallup / Pew / 538 require API keys (or
  scraping), so we surface political mood through Polymarket prices instead.
- Cards quietly disappear when their underlying FRED series fails to fetch,
  rather than rendering as broken. Check `/api/health` and the server log if
  the page looks sparse.
