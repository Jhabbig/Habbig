# narve walk-forward backtest → proof report → live page

**Date:** 2026-06-18
**Status:** design (awaiting review)
**Owner:** narve / Habbig — product MVP demo

## Goal

Produce a real, no-hindsight number proving narve's core thesis —
**credibility-scored social-media forecasters beat the prediction market** —
suitable for the YC pitch ("show our product actually works"), then surface
that proven track record on the single live product page (`/markets/active`).

This is the functionality-first MVP demo from the 2026-06-09 strategy meeting:
single vertical (US politics), backtesting as the #1 deliverable, no UI polish,
no Stripe, no multi-vertical.

### Acceptance criteria
- A walk-forward (point-in-time) backtest runs over a curated set of resolved
  US-politics markets with **zero lookahead** (mechanically guaranteed).
- It reports ROI, win-rate, Sharpe, max-drawdown — run under **two** cold-start
  methodologies and against **two** baselines.
- Every bet is auditable: its as-of inputs are logged and explainable one-by-one.
- A report artifact (markdown/HTML) is produced for the pitch deck.
- Phase 2: the proven number + live signals render on `/markets/active`.

## Context — what already exists

Built earlier this session (commit `638f645`, not yet run with real data):
- `gateway/backtest.py` — engine: `simulate(params, predictions)`,
  `run_backtest(run_id)`, `sharpe_ratio()`, Kelly staking, ROI/win-rate/drawdown.
- `gateway/markets_routes.py` — the single `/markets/active` page +
  `/api/analytics/prediction-accuracy`.
- `gateway/migrations/201_market_resolutions.py`, `202_prediction_market_price.py`.
- `gateway/scraper_routes.py`, `gateway/jobs/market_price_jobs.py` — live loop.

**DB reality (local, 2026-06-18):** 150 predictions, **0 resolved**,
**0 market_snapshots**, **0 backtest_runs**, migrations 201/202 not yet applied
locally, 13 source_credibility rows. The loop is a skeleton with no data in it.

The live scraper loop is **blocked** on manual steps (Playwright install,
Twitter/TruthSocial logins, prod box reachable). The demo must NOT depend on it.

## Approach

A **replay harness** that walks history forward in time. At each date it feeds
the existing engine only what was knowable then, records the bet narve would
have made, and scores it at market resolution. Reuses `simulate()` — does not
replace it.

Chosen over (a) waiting for the live loop (weeks, blocked) and (b) deriving
signals from price movement (would demo a momentum strategy, not narve's
"who's right on social media" thesis — wrong product).

## Architecture — 4 units

### 1. Golden dataset — `gateway/data/backtest/*.json`
~15-20 resolved US-politics markets. Each record:
- `market_id`, `question`, `resolved_outcome` (1/0), `resolved_at`
- `price_timeline`: `[{date, yes_price}]` — real Polymarket/Kalshi history
- `forecasts`: `[{source_handle, predicted_probability, made_at, url}]` —
  **real, dated** forecaster predictions made before resolution
Plain JSON, hand-curated, auditable. **Sourcing split:** the user provides the
forecasters + their specific real predictions (race / what they said / date);
the build pulls the matching market price timelines + outcomes and structures
the JSON.

### 2. Replay harness — `gateway/backtest_replay.py`
Iterates dates chronologically. At each step:
- computes each forecaster's credibility from **only their prior resolved calls**
- forms narve's credibility-weighted probability for each open market
- compares to that day's market price; emits a bet if `edge > threshold`
- logs the decision with all as-of inputs
Feeds results into the existing `simulate()` engine.

### 3. Scoring + report — `gateway/backtest_report.py`
ROI, win-rate, Sharpe, max-drawdown. Runs:
- **Two cold-start methodologies** (see Decisions): strict two-window AND
  Bayesian-prior — report both numbers (robustness flex).
- **Two baselines**: always-follow-market, and flat/unweighted ensemble.
Outputs a markdown/HTML report for the pitch deck with per-bet detail.

### 4. Live page wiring (Phase 2) — `gateway/markets_routes.py`
Once the number is real, `/markets/active` shows the proven track record +
current live signals. Deferred until the report number exists.

## Data flow

```
golden dataset (JSON) ──► replay harness ──► per-step decision log
                            │  (both cold-start methods)
                            ▼
                       simulate() engine ──► scoring ──► report (MD/HTML)
                                                            │
                                            [Phase 2] ──► /markets/active
```

## Decisions

- **Cold-start: show both.** A forecaster's score needs history; on day 1 there
  is none. Run (a) strict two-window (earlier calibration window builds
  credibility, later test window bets using only credibility known at that
  point) AND (b) Bayesian neutral prior updated per resolved call, betting once
  N calls exist. Report both — robust to methodology, and a pitch flex.
- **Baselines are mandatory.** A number with no baseline is meaningless; every
  run reports narve vs. follow-market vs. flat-ensemble.
- **Dataset over volume.** Small N, every row bulletproof and explainable,
  beats large-but-fuzzy for a *demo*. Live loop handles large-N later.

## Error handling / integrity guards

- **Lookahead assertion:** harness hard-fails if any input timestamp ≥ the
  decision date. The bug that invalidates backtests, caught mechanically.
- **Mandatory baseline comparison** on every run.
- **Small-N honesty:** report states N explicitly, shows per-bet detail, never
  implies more data than exists.

## Testing

- Unit: credibility calc, edge calc, Kelly stake, Sharpe (engine helpers exist).
- **Known-answer synthetic fixture:** a forecaster who is always right must
  score ~1.0 and win every bet — proves the harness itself isn't lying.
- Harness builds against the synthetic fixture first; real dataset drops in after.

## Parallel track (not on critical path)

The live scraper loop keeps accumulating real data for eventual large-N
validation, but is gated on the user's manual steps (Playwright, social logins,
prod box). Surfaced in the task list, NOT a demo dependency.

## Out of scope (YAGNI / per meeting)

UI polish, Stripe/billing, multi-vertical, design work, any live-loop dependency
for the demo. Just: dataset → harness → number → report → page.

## Open inputs

- **User provides:** the forecaster list + their specific real dated predictions.
  Build can start on the synthetic fixture immediately and drop real calls in.
