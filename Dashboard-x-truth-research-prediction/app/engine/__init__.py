"""Stage 3 — Prediction Engine (fusion layer).

Fuses Component 1 (per-post credibility scores) with Component 2 (LLM-predicted
metrics + extracted features) into a single calibrated P(YES) prediction for a
prediction-market outcome, at concurrent-user scale.

Modules:
  config    — hot-reloading `engine:` section of config.yaml (no redeploy)
  schemas   — request/response contracts (the public interface)
  normalize — signal normalization to comparable [0, 1] scales
  fusion    — swappable fusion strategies + probability calibrators
  cache     — content-hash dedup cache (Redis or in-process), hit-rate metric
  tiering   — per-job-class model tier routing (Haiku/Sonnet/Opus/fine-tuned)
  metrics   — per-stage latency, cache hit rate, cost/1k, tier mix, degraded rate
  audit     — background writer persisting every prediction for reproducibility
  service   — the orchestrator (`PredictionEngine.predict`)
  batch     — Message Batches API queue for non-interactive jobs (-50%)
  replay    — grade audits vs realized outcomes, re-score, fit calibrators
  api       — FastAPI router mounted under /api/v1/engine/*
"""
