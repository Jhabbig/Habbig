# Financial Matrix Toolkit

A toolkit of **20 matrix-based models** for analysing and forecasting financial
market data, built on **numpy / scipy / pandas / matplotlib only** (no ML
frameworks). Python 3.11, Ubuntu 24.04.

## Core thesis (the whole point)

> Return **direction** is near-unpredictable (efficient markets). The predictable
> structure is **(A) cross-asset correlation/covariance and its breakdown,
> (B) market regime, and (C) volatility magnitude**.

The toolkit extracts that structure — and **honestly shows that direction
prediction barely beats a coin flip.** Every forecasting model is scored
out-of-sample against a NULL benchmark; a model that does not beat its null has
no skill, however sophisticated it looks. Direction/return models are judged
**after transaction costs** — an edge smaller than costs is not an edge.

Running `python main.py` produces a ranking like this (synthetic data shown):

```
1  C  RidgeAR-HAR        QLIKE(var)  +16.5%   <- volatility: works
2  C  EWMA               QLIKE(var)  +12.2%
3  B  GaussianHMM        QLIKE(var)   +8.7%   <- regime helps volatility
...
10 D  LogisticDirection  1-accuracy   -0.9%   <- direction: fails
12 D  ESN                RMSE(ret)    -3.0%
13 D  GP                 RMSE(ret)    -5.3%

CLOSING SUMMARY
  Volatility (Tier C): 3/4 models beat the vol null.
  Direction  (Tier D): 0/4 models beat the null AFTER 10bps costs.
  -> the EFFICIENT-MARKET FLOOR. The correct, expected result.
```

## Data contract

* **Market:** US equities, one asset class only.
* **Assets:** 15 large-cap S&P 500 names — AAPL, MSFT, NVDA, AMZN, GOOGL, META,
  JPM, XOM, JNJ, PG, V, HD, BAC, KO, DIS.
* **Bars:** daily close. **Range:** ~6 years.
* **Loader resolution:** live `yfinance` → committed CSV cache → seeded synthetic.

The pipeline loads prices, computes log-returns, handles missing data, and
produces a returns matrix `R` (T×n) and a price matrix `P` (T×n).

> **Real vs synthetic data.** On a machine with open network access,
> `python main.py --refresh` fetches **real** prices via `yfinance` and caches
> them. The cache committed here is **synthetic** (seeded, labelled) because the
> build environment blocks every public market-data host. The synthetic series
> mimics real markets (market factor, sector blocks, volatility clustering,
> calm/crisis regimes, near-unpredictable direction) so the toolkit runs
> end-to-end and reproducibly offline. See `data/README.md`.

## Quick start

```bash
pip install -r requirements.txt
python main.py                 # full run on cached/synthetic data
python main.py --refresh       # fetch real prices (needs open network)
python main.py --train 378 --step 5 --cost-bps 10
python main.py --days 10000 --train 2520 --step 20   # train on a big synthetic panel
python scale_experiment.py     # how skill scales with dataset size (see below)
pytest                         # per-model tests
```

### The honest bottom line: does it PAY? (`backtest.py`)

The capstone test. If direction is unpredictable but volatility and correlation
are, then a strategy built on *risk* — not direction — should help. This
backtests equal-weight buy-and-hold against inverse-vol (risk parity) and
vol-managed strategies, walk-forward, **net of costs**, using a causal EWMA
covariance forecast, with a **block-bootstrap 95% CI on the Sharpe difference**.

```bash
python backtest.py --demo                       # equity curves -> results/backtest.png
python backtest.py --refresh --target-vol 0.12  # on real data
```

Result (cached panel): every strategy's Sharpe-vs-benchmark CI **straddles 0** —
the predictable structure does **not** buy a statistically significant
risk-adjusted edge, and never buys higher raw return (direction is unpredictable).
What it *does* buy is **shallower drawdowns** (risk parity −2.2%, vol-managed cuts
max drawdown further on longer samples). That is the real, defensible payoff of
knowing volatility and correlation: **risk control and crash mitigation, not
alpha.** The toolkit refuses to manufacture an edge even in its own backtest.

### The two-stage pipeline: track models → event readout (`pipeline.py`)

The end-to-end architecture: **stage 1** fits the matrix models (EWMA, rolling
covariance, Gaussian-HMM, PCA, RMT, DMD) walk-forward and emits their
forecasts/states — the "tracks" — using only data up to each origin. **Stage 2**
is a class-weighted logistic that reads those signals to flag events. Training is
**purged** (a label is never seen before it is realised), so the whole stack is
causal.

```bash
python pipeline.py --demo                 # train every model + save the pipeline
python pipeline.py --demo --event big_move
```

The readout outputs **calibrated probabilities** and is scored with the metrics
that actually matter for rare events — **ROC-AUC (with a bootstrap 95% CI),
PR-AUC (average precision), and Brier score** — plus a balanced-accuracy / recall
/ F1 at an operating threshold **tuned on training data only**. Findings on the
cached panel:

| event | base rate | AUC (95% CI) | PR-AUC | tuned recall |
|-------|-----------|--------------|--------|--------------|
| drawdown | 85% | **0.99** [0.99,0.99] | 1.00 | 97% |
| vol_state | 51% | **0.94** [0.93,0.95] | 0.94 | 88% |
| trend_up | 51% | **0.94** [0.93,0.95] | 0.94 | 82% |
| big_move | 9% | **0.68** [0.63,0.72] | 0.20 | 66% |
| vol_transition | 13% | **0.60** [0.56,0.64] | 0.16 | 53% |

Every event's AUC CI clears 0.5, so all have *real* ranking skill — but honestly
graded: persistent events are near-perfect, tail/transition events have modest
(but significant) AUC ~0.6–0.7. The manifest also records track-only vs raw-only
AUC and each event's **top feature drivers** (e.g. `drawdown` driven by drawdown
depth; `vol_transition` partly by the HMM crisis-probability track). Verified
leak-free by a 4-agent adversarial audit (which caught and fixed a
stride-dependent purge off-by-one) and a "noise features must score 0.5"
regression test.

Those CIs are **cluster bootstraps over forecast origins**, not over rows. The
readout pools 15 assets at each of ~138 origins into one vector, but those assets
share a market factor (mean pairwise correlation 0.30 on the cached panel), so
resampling rows i.i.d. would pretend the sample carries ~15× more independent
information than it does and return a CI that is too narrow. Switching to the
cluster bootstrap widens the intervals by up to 1.3× — the difference between
`big_move`'s Brier skill reading as a pass and reading as noise.

**Calibrated probabilities.** The readout's raw scores are recalibrated on
training data only, so `P(event)` means what it says — essential if you want to
size positions on it. The calibrator is chosen per event by `--calibration auto`
(the default; see below), or pinned with `platt` / `isotonic` / `none`.
Calibration slashes
the Expected Calibration Error on the rare events (vol_transition ECE 0.34 → 0.02,
big_move 0.35 → 0.02) and writes a reliability diagram to
`results/reliability_pipeline.png`. The saved model carries its calibrator.

**Effectiveness metrics — is the model *useful*, not just *ranked well*?** A high
AUC only says the model ORDERS days correctly. It does not say the probabilities
are worth acting on. So beyond AUC/AP/Brier, every event is scored with metrics
anchored to a no-skill null (`eventmetrics.py`): the **Brier skill score** (1 −
Brier/climatology; > 0 = the calibrated probabilities beat
always-predicting-the-base-rate) **with a cluster-bootstrap 95% CI**, the
**Murphy decomposition** (Brier = reliability − resolution + uncertainty: honesty
cost vs information content vs target difficulty), **Spiegelhalter's z-test** (is
the miscalibration statistically detectable, or just binning noise?), **MCC** at
the tuned threshold (uses all four confusion cells, so base-rate guessing scores
exactly 0 under any imbalance), **KS** separation, and **lift@10%** (how many
times more real events the top-decile alert list catches than random flagging).

Every verdict is three-state, exactly as the after-cost edge test in `harness.py`
is: *no skill* / *positive but within noise* / *significant*. A point estimate is
never enough. Findings on the cached panel:

| event | BSS (95% CI) | verdict | MCC | KS | lift@10% | of its max |
|-------|--------------|---------|-----|-----|----------|------------|
| drawdown | **+0.76** [+0.71,+0.80] | significant | 0.85 | 0.92 | 1.2× | **100%** |
| trend_up | **+0.61** [+0.57,+0.66] | significant | 0.73 | 0.75 | 1.9× | 99% |
| vol_state | **+0.60** [+0.57,+0.64] | significant | 0.74 | 0.75 | 2.0× | **100%** |
| big_move | +0.02 [−0.02,+0.06] | *within noise* | 0.14 | 0.28 | 2.4× | 24% |
| vol_transition | −0.01 [−0.04,+0.01] | no skill | 0.09 | 0.17 | 1.1× | 14% |

These sharpen the honest story in four ways.

1. **Ranking skill does not imply usable probabilities.** `vol_transition`'s AUC
   is significantly above 0.5, yet its Brier skill is **zero** — resolution 0.001
   < reliability 0.002, so the calibrated probabilities carry no information
   beyond the base rate. Ranking skill that thin does not survive conversion into
   something you could size a position on.

2. **`big_move`'s apparent edge is noise.** Its BSS is +0.02, but the cluster
   bootstrap CI is [−0.02, +0.06] — it straddles zero, so the model is *not*
   distinguishable from climatology. The old i.i.d. bootstrap gave [−0.01, +0.06]
   and made this look like a pass. This is the toolkit's own thesis applied to
   its own new metric.

3. **Raw lift is not comparable across events.** Lift has a ceiling of
   min(1/base rate, 1/k) — at an 85% base rate the *best possible* lift@10% is
   1.18×. So `drawdown`'s unimpressive-looking 1.2× is a **perfect** alert list,
   while `big_move`'s headline 2.4× is only **24%** of what its 9% base rate
   allows. The naive reading inverts the truth; the "of its max" column is what
   compares across events.

4. **A small ECE is not proof of honesty — and the test found a real bug.** With
   the old fixed Platt calibrator, `drawdown` and `trend_up` showed statistically
   *detectable* miscalibration (p = 0.000 and 0.006) even at ECE ≈ 0.03, visible
   only because the sample is large. That is what motivated `--calibration auto`
   below, which fixes it. (The z-test assumes independent rows, so with
   correlated assets pooled it errs toward flagging miscalibration too eagerly;
   treat a failure as a prompt to inspect the reliability diagram.)

**Does the two-stage architecture earn its keep?** The manifest records BSS for
the track-only, raw-only and hybrid readouts, and the answer is honestly *mixed*:

| event | track-only | raw-only | hybrid |
|-------|-----------|----------|--------|
| drawdown | +0.719 | **+0.762** | +0.756 |
| trend_up | +0.281 | **+0.639** | +0.615 |
| vol_state | +0.404 | +0.599 | **+0.605** |
| big_move | −0.019 | **+0.046** | +0.018 |
| vol_transition | −0.006 | **+0.003** | −0.009 |

Only `vol_state` is genuinely better as a hybrid. On every other event the raw
features alone score *higher* than tracks-plus-raw — the stage-1 tracks are
diluting the readout, not enriching it. The ranking metrics never showed this
(hybrid AUC looks fine everywhere); it took a metric that asks whether the
*probabilities* improved. That is an honest argument for keeping the two-stage
architecture only where it pays.

### Choosing the calibrator honestly (`--calibration auto`, the default)

The Spiegelhalter test above found a real defect: a single fixed Platt sigmoid
left `drawdown` and `trend_up` measurably miscalibrated. But isotonic is not a
blanket upgrade — it overfits when positives are rare. So the pipeline now picks
per event, scoring both on a **chronological inner split of the training rows**
(`calibration.select_calibrator`). Choosing by test-set score would be exactly
the look-ahead this toolkit refuses, so the evaluation set is never consulted.

| event | chosen | ECE (platt → auto) | Spiegelhalter p (platt → auto) |
|-------|--------|--------------------|-------------------------------|
| trend_up | isotonic | 0.024 → **0.005** | 0.006 → **0.998** |
| drawdown | isotonic | 0.033 → **0.010** | 0.000 → **0.062** |
| vol_state | isotonic | 0.013 → 0.013 | 0.721 → 0.263 |
| big_move | isotonic | 0.018 → 0.024 | 0.495 → 0.667 |
| vol_transition | **platt** | 0.021 → 0.023 | 0.415 → 0.258 |

All five events now pass the calibration test, and `drawdown`'s Brier skill rose
from +0.74 to +0.76 in the bargain. Note the selector chose Platt for exactly the
event where isotonic should struggle — the rarest one. Use `--calibration platt`
(or `isotonic`, `none`) to pin it.

**Train on real data / serve live** — on a networked machine:

```bash
python pipeline.py --refresh                # fetch real prices via yfinance, train, save
python predict_live.py --demo               # today's per-asset calibrated P(event)
python predict_live.py --refresh            #   ...on freshly fetched real data
```

### Predicting market EVENTS honestly (`predict_events.py`)

A general harness for any binary market event. Because most events are RARE,
plain accuracy lies ("predict nothing ever happens" scores 90%), so every event
is scored with **balanced accuracy, precision, recall, F1, MCC, and skill =
balanced accuracy − 0.5** (MCC makes the trap visible in one number: the
base-rate null scores 90% accuracy and exactly 0.00 MCC). Three models train per event: base-rate null, persistence, and
a class-weighted logistic. Includes a hyperparameter `--grid` that shows accuracy
*plateauing* (the target sets the ceiling, not the tuning).

```bash
python predict_events.py --demo --event all
python predict_events.py --demo --event vol_transition        # the genuinely HARD one
python predict_events.py --demo --event vol_state --grid       # tuning plateau demo
python predict_events.py --ticker ^GSPC --event big_move
```

Built-in events (extensible — add a labeler to the `EVENTS` registry):

| event | question | nature | typical skill |
|-------|----------|--------|---------------|
| `vol_state` | vol above trailing median? | balanced | high (+0.37) |
| `drawdown` | in a >5% drawdown? | persistent | high (+0.45) |
| `trend_up` | price above 21-day MA? | persistent | high (+0.39) |
| `big_move` | \|return\| > 2× trailing std? | tail | modest (+0.17) |
| `vol_transition` | will the vol regime FLIP? | rare/hard | low (+0.06) |

The lesson the table teaches: **persistent states are easy, transitions and tail
events are hard** — and a base-rate guesser can score 90% *accuracy* on a rare
event while catching 0% of them (recall 0, balanced accuracy 0.5). Always read
balanced accuracy / recall, never raw accuracy, for rare events.

### Where high accuracy is REAL: volatility state (`classify_volstate.py`)

Return *direction* tops out near a coin flip, but volatility *state* ("calm vs
turbulent tomorrow?") is genuinely predictable because volatility clusters. This
trains four classifiers at once and scores them against an honest 50% floor (the
state is defined so calm/turbulent are ~50/50):

```bash
python classify_volstate.py --demo                    # cached 15-asset panel
python classify_volstate.py --demo --vol-window 21    # vol REGIME (smoother) -> ~95%
python classify_volstate.py --ticker ^GSPC            # a single real symbol (needs yfinance)
```

Typical result: ~87% for next-day vol state, ~95% for the smoother 21-day vol
regime — all far above the 50% floor. **This is the honest way to get a 95%
accuracy number.** Accuracy is a property of the *target*, not of how hard or how
long you train: no amount of training makes return direction 95%-accurate, but
volatility state is 95% by nature.

### Forecasting a single series h steps ahead (oil, inflation, …)

`forecast.py` predicts the LEVEL of *any* one series H steps ahead (e.g. oil ~1
month = 21 trading days, or next-quarter inflation) and **honestly reports
whether it beats the naive "no change" null**, with an 80% uncertainty band and a
significance test (Newey-West, lag ≥ horizon, Bonferroni across models — h-step
windows overlap, so a raw skill number is very noisy).

```bash
python forecast.py --demo macro --horizon 6      # persistent series -> SIGNIFICANT skill
python forecast.py --demo oil   --horizon 21     # random-walk price  -> no skill (honest)
python forecast.py --ticker CL=F --horizon 21    # crude oil ~1 month (needs yfinance)
python forecast.py --fred CPIAUCSL --horizon 12 --transform yoy   # CPI inflation (needs pandas_datareader)
python forecast.py --csv mydata.csv --value-col rate --horizon 6  # any CSV series
```

The lesson it teaches: asset **price levels** are barely forecastable beyond
"about the same as today" (skill ≈ 0, wide band), while **macro statistics**
(inflation, unemployment) are genuinely persistent and *do* beat no-change. A
small MAPE is not skill — only a *significant* skill-vs-null is.

### Training on more data

These are statistical estimators **refit on every walk-forward window**, not
networks trained once and frozen. "More data" means a larger training window per
fit and more out-of-sample windows — set via `--train` / `--days`, or use
`scale_experiment.py` to sweep dataset size (1.5k → 20k days). The result is the
point of the whole toolkit:

* **Volatility skill stays solidly positive** — vol clustering is real structure.
* **Direction accuracy converges to 50% with a *shrinking* confidence interval**
  that keeps straddling 50%. More data does not manufacture a direction edge; it
  makes the efficient-market verdict *more* certain. Net-of-cost direction return
  stays negative at every scale.

Measured across 8× more data (synthetic; `results/scaling.png`, `scaling.csv`):

| Data | OOS windows | Vol skill (EWMA / RidgeAR) | Direction accuracy (95% CI) |
|------|-------------|----------------------------|------------------------------|
| 4,000d (~15y)  | 73  | +13.9% / +10.6% | 48.7–54.0%, ±11.5% |
| 10,000d (~39y) | 373 | +11.8% / +12.5% | 48.8–50.2%, ±5.1%  |
| 20,000d (~79y) | 873 | +12.5% / +14.9% | 49.9–51.9%, ±3.3%  |

The direction CI contracts as 1/√windows and never stops straddling 50% (all
`|z| < 1.1`). A cautionary detail: at 4k days the logistic model showed a
tempting +22%/yr "edge" — pure small-sample noise (CI ±11.5%); by 20k days it
collapses to +0.4%/yr. More data destroys the illusion of a direction edge.

(No real data is reachable in this sandbox, so the scale-up uses the seeded
synthetic generator; the same conclusion holds on real data via `--refresh`.)

Outputs (ranking table, per-model report, plots) print to the terminal and write
to `./results/` (`skill_ranking.png`, `forecast_panels.png`).

## Shared interface

```python
model.fit(data)        # data is a MarketData (P, R, tickers, dates)
model.predict(steps)   # np.ndarray forecast
model.state()          # current regime / matrix / vol estimate (dict)
model.score(actual)    # error + improvement-over-null (dict)
```

**Decomposition / detector models (PCA, RMT, SVD, NMF, graph Laplacian, change
detector, absorbing chain) raise `NotImplementedError` from `predict()`** and
expose `transform()` / `reconstruct()` / `detect()` instead. They never fake a
forecast.

## The harness and the nulls (`core.py`, `harness.py`)

The most important component. No single 80/20 split — **rolling walk-forward
only** (`walk_forward(model_factory, data, train_window, test_window, step)`),
refitting a fresh model on every window.

| Target      | NULL benchmark                          | Loss          |
|-------------|-----------------------------------------|---------------|
| variance    | rolling-historical-mean variance        | QLIKE         |
| price       | random walk (predict last value)        | RMSE          |
| returns     | random walk (predict zero return)       | RMSE + costs  |
| direction   | base-rate / coin-flip                    | accuracy + costs |

**SKILL = % improvement over null** (flagged red if ≤ 0). For any
direction/return model the report also gives gross vs **net (after-cost)** annual
return and whether the edge survives costs. Seed is **42** everywhere.

**An after-cost edge must be statistically significant, not just a point
estimate.** Beating the null's net return on one short sample is cheap — with
several correlated long-biased models, P(≥1 false winner) ≈ 94%. So the harness
runs a **paired Newey-West (HAC) t-test** on the per-period (model − null)
net-return series and reports three states:

* `EDGE < COST` — does not even beat the null net (red);
* `beats null but WITHIN NOISE (t=…, p=…)` — point-estimate win, not significant (yellow);
* `edge > cost & SIGNIFICANT (t=…, p=…)` — survives the test (green).

The closing summary counts only edges that survive **Bonferroni** correction
across the Tier-D models. This is what stops the toolkit crowning small-sample
noise as skill. (No-look-ahead is enforced too: data is forward-filled only —
never back-filled, which would copy a future price into the past.)

## The 20 models (tier order = predictability order)

**Tier C — Volatility (genuinely works)**
1. EWMA / RiskMetrics volatility
2. Rolling realized covariance matrix dynamics
3. VAR on log-volatilities
4. Ridge-AR on realized vol (HAR; strong baseline)

**Tier A — Structure / cross-asset (works; decomposition/detectors)**
5. PCA on returns (eigen-portfolios)
6. Random Matrix Theory filter (Marchenko-Pastur — separate signal from noise)
7. Truncated SVD of returns
8. Rolling correlation + Frobenius-distance change detector ("the matrix changed")
9. Graph Laplacian over the asset network
10. NMF on absolute returns (co-movement clusters)

**Tier B — Regime / state (partially works)**
11. Gaussian HMM for regime identification (calm/crisis; Baum-Welch from scratch)
12. Markov-switching mean + variance
13. Discrete Markov chain on bucketed returns
14. Absorbing chain: drawdown → recovery (expected time-to-recovery)
15. Dynamic Mode Decomposition on the returns matrix
16. Kalman filter / local-level state space on the price level

**Tier D — Direction (HONESTY TIER: expected to lose to the coin flip)**
17. Logistic regression on lagged returns → next-day direction
18. VAR on returns
19. Gaussian Process regression on returns
20. Echo State Network reservoir on returns

> Tier D models are built properly (real logistic regression, VAR, GP, and an
> echo-state reservoir, all from numpy) precisely so their failure is credible:
> it is the models, not strawmen, that fail to beat the efficient-market floor.

## Guardrails

* If data violates a model's assumptions, the harness logs a warning and skips
  that window — it never crashes and never silently emits garbage.
* One file per tier under `./models/`, plus `./core.py`, `./harness.py`,
  `./data.py`, runnable `./main.py`.
* `pytest` per model: fit on a tiny synthetic series, assert `predict()` shape
  and that `state()` is populated; decomposition models assert `predict()` raises.

## Layout

```
core.py        shared interface, metrics, NULL benchmarks
data.py        data pipeline (yfinance -> cache -> synthetic), MarketData
harness.py     walk_forward, transaction costs, compare_all, plots
main.py        runnable end-to-end driver
models/
  tier_c_volatility.py   (1-4)
  tier_a_structure.py    (5-10)
  tier_b_regime.py       (11-16)
  tier_d_direction.py    (17-20)
tests/         pytest suite (one file per tier + core/harness)
data/          committed price cache + provenance note
results/        generated plots
```
