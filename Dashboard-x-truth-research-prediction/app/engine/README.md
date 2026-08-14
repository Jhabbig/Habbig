# app/engine/ — Stage 3 Prediction Engine (fusion layer)

The fusion layer of the real-time pipeline. Two upstream components feed it:

- **Component 1 — Credibility Ranker** (`app/credibility/`): per-post
  `credibility_score` (0–1) + metadata for X/Reddit posts, scored at runtime.
- **Component 2 — Metric-Prediction LLM + extraction** (`app/processing/
  extractor.py`, `llm_extractor.py`): `predicted_metrics` (structured JSON) +
  `extracted_features` from posts/pages.

The engine ingests both, fuses them into a calibrated **P(YES) for the matched
prediction-market outcome** with a confidence score, returns it to the caller,
and logs everything needed to reproduce and later grade the prediction.

```
credibility[] ──┐
metrics{}    ───┤→ dedup cache → normalize → fuse (v0: weighted ensemble)
context{}    ───┘        ↑                        → calibrate (Platt/isotonic)
                         │                        → degrade-handling
                    Redis / in-proc               → audit (async batch writer)
                                                  → response + metrics
```

## Interfaces

**Input** (`POST /api/v1/engine/predict`, auth: `X-API-Key`):

```json
{
  "job_id": "uuid",
  "user_id": "string",
  "job_class": "interactive",
  "credibility": [{"post_id": "...", "source": "x|reddit", "score": 0.8, "features": {}}],
  "metrics": {
    "predicted": {"predicted_probability": 0.72},
    "extracted": {"engagement_velocity": 400},
    "model_version": "claude-haiku-4-5",
    "usage": {"input_tokens": 1500, "output_tokens": 120, "cache_read_input_tokens": 1000}
  },
  "context": {"market_slug": "btc-150k", "market_implied_probability": 0.55, "category": "crypto"}
}
```

**Output**:

```json
{
  "job_id": "uuid",
  "prediction": {"p_yes": 0.68, "side": "YES"},
  "confidence": 0.71,
  "contributing_signals": [{"signal": "credibility", "weight": 0.35, "value": 0.7}, ...],
  "model_versions": {"credibility": "credibility-v1", "metric": "claude-haiku-4-5", "fusion": "weighted_v0.1:weighted_v0"},
  "degraded": false,
  "degraded_reasons": [],
  "cache_hit": false,
  "model_tier": "claude-opus-4-8",
  "latency_ms": 0.42
}
```

Other endpoints: `GET /metrics` (per-stage latency percentiles, cache hit
rate, cost/1k, tier mix, degraded rate, cost alert), `GET /config` (live
hot-reloaded config), `POST /replay`, `POST /calibration/fit`, `GET /cost`.

## Hard requirements → where they live

| Requirement | Implementation |
|---|---|
| Concurrency, no global locks | Stateless async hot path (no LLM calls — Components 1+2 ran upstream). Everything on the event loop is lock-free; audit writes go through a non-blocking queue. Horizontal scale = more workers sharing Redis + DB. Proven by `scripts/load_test_engine.py` (500 concurrent, p95 < 1 ms in-process). |
| Dedup cache + hit-rate metric | `cache.py` — key `sha256(canonical content)` (job_id/user_id excluded, so concurrent users asking about the same posts share one result). Redis when `REDIS_URL` set, in-process fallback. Hit rate on `/metrics` and in the cost readout. |
| Model-tier switch per job class | `tiering.py` + `engine.model_tiers` in config.yaml. Config is mtime-watched (`config.py`) — edit + save is live, **no redeploy**. |
| Batch API + prompt caching | `batch.py` — non-interactive extraction through the Message Batches API (−50%), shared system prompt marked `cache_control: ephemeral` (cached input ~0.1×). Results land in `extraction_cache`, so the interactive path gets free hits. Scheduler-driven when `engine.batch.enabled`. |
| Determinism & auditability | `audit.py` + `fusion_audit` table: full inputs, signals, model versions, prompt hash, fusion version, tier, tokens, cost per prediction. `compute_prediction()` is pure — replaying stored inputs reproduces the output bit-for-bit (asserted in tests and by `/replay`'s determinism check). |
| Graceful degradation | Missing Component 1/2 never fails a request: fusion runs on whatever signals exist, confidence is cut by the configured penalty, `degraded: true` with machine-readable reasons. Both missing → the 0.5 prior at the confidence floor. |

## Fusion v0 (swappable)

`fusion.py` — transparent weighted ensemble, not a black box:

1. `normalize.py` puts every signal on [0,1]: mean post credibility, the
   LLM-predicted probability, the live market price, and min-max-normalized
   extracted metrics (`engine.fusion.metric_ranges`).
2. Weights (config) are renormalized over the signals actually present.
3. Combination: `linear` weighted mean, or `logistic`
   (`sigmoid(bias + scale·(mean − 0.5))`).
4. Calibration: `none | platt | isotonic` — params in config, fit from logged
   outcomes via `/calibration/fit` (or `replay.fit_calibration`).

Everything sits behind the `FusionStrategy` interface; a trained model
replaces v0 by calling `register_fusion("my_model", factory)` and flipping
`engine.fusion.strategy` — ingestion and serving are untouched. Every
prediction is logged from day one so that replacement can be trained on real
outcomes.

## Replay harness

`replay.py` closes the accuracy loop:

- `grade_pending()` backfills `realized_outcome` from `resolved_market` once
  a referenced market settles.
- `replay()` re-scores every graded job with the *current* fusion config and
  reports stored-vs-replayed accuracy, Brier score, a 10-bin reliability
  table, and a determinism check.
- `fit_calibration()` fits Platt/isotonic params from (raw score, outcome)
  pairs; paste the result into `engine.fusion.calibration` (hot-reloaded).

## Cost model (measured, not estimated)

Run `scripts/measure_tokens.py` **first** — it measures real tokens in/out for
one metric-prediction job (cold and warm-cache) and prices it. That number ×
job volume decides viability before anything is built on top.

At runtime the engine records upstream `metrics.usage` tokens per job, prices
them against `engine.cost.prices_per_mtok` (with cached-input and batch
discounts), and exposes `$ per 1k predictions` on `/metrics`, `/cost`, and
`scripts/cost_readout.py`. The alert flag flips above
`engine.cost.alert_usd_per_1k`.

Sizing basis: 500 peak concurrent users at ~0.3 duty cycle ≈ 1M jobs/month.
The three cost levers, compounding: dedup cache (repeat content = $0),
Haiku-tier routing for non-interactive classes (5× cheaper than Opus), batch
API + prompt caching for the offline path (−50% / −90% cached input).

## ⚠ Legal constraint (do not skip)

X and Reddit terms prohibit using their API data to **train** models. This
engine uses their data only as **runtime signals**. Any trained fusion model
or calibrator must learn exclusively from the `fusion_audit` table — our own
logged predictions vs realized outcomes. The fitting helpers
(`fit_platt`/`fit_isotonic`/`fit_calibration`) only accept
(score, outcome) pairs for this reason. Credibility scores about identifiable
accounts carry defamation/profiling risk — they stay internal
(key-authenticated API), never published, unless legally cleared.

## Files in this directory

| File | Purpose |
|---|---|
| `config.py` | Hot-reloading `engine:` config (mtime watch) + defaults tree. |
| `schemas.py` | Pydantic request/response contracts + canonical content hash. |
| `normalize.py` | Signal normalization to comparable [0,1] scales. |
| `fusion.py` | `FusionStrategy` interface, weighted-ensemble v0, Platt/isotonic calibrators + fitting (logged outcomes only). |
| `cache.py` | Content-hash dedup cache (Redis/in-process), hit-rate metric. |
| `tiering.py` | Job-class → model tier resolution (config-driven). |
| `metrics.py` | Latency percentiles, cache/degraded rates, tier mix, token + $ accounting, cost alert. |
| `audit.py` | Non-blocking batched writer into `fusion_audit`. |
| `service.py` | `PredictionEngine.predict` orchestrator + pure `compute_prediction`. |
| `ingest.py` | Internal-queue path: maps the scheduler's ranked predictions onto EngineJobs (job class `pipeline`) so every scraped signal flows through Stage 3 and feeds the replay loop. |
| `batch.py` | Message Batches queue for non-interactive extraction. |
| `replay.py` | Grading, replay/calibration reporting, cost readout aggregate. |
| `api.py` | `/api/v1/engine/*` router (auth injected in `app/main.py`). |
