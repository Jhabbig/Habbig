# app/processing/ — Prediction processing pipeline

The middle layer between raw scraped posts and the dashboard view. Three stages:

1. **Extract** — turn a raw post into a structured `Prediction` (outcome, probability, category).
2. **Rank** — score that prediction by EV vs market and apply risk flags.
3. **Resolve** — when the market closes, mark the prediction correct or incorrect and update source credibility.

The orchestration lives in `app/scheduler.py`. Tunables live in `app/config.yaml`.

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `extractor.py` | Pulls structured `ExtractionResult`s out of raw post text. Combines regex patterns (for explicit "X% chance" phrasing) with category classification using keyword maps from `config.yaml`. |
| `ranker.py` | `compute_ev_score()` (predicted-prob vs market-implied prob, scaled by inverse market odds) and `compute_risk_flags()` (low source credibility, low market liquidity, low sample size, etc.). |
| `resolver.py` | `MarketResolver` — pulls closed Polymarket markets, finds predictions tied to them, marks each prediction correct/incorrect, writes a `SourcePredictionRecord`, recomputes source credibility. |
