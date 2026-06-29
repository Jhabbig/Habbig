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
pytest                         # per-model tests
```

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
