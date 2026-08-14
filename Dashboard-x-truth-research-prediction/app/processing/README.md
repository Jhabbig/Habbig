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
| `extractor.py` | Pulls structured `ExtractionResult`s out of raw post text. Regex patterns cover explicit percentages ("70% chance", "30 percent chance", "odds of X at 40%", "I give it 65%"), fraction odds ("1 in 5 chance"), negated percentages ("90% sure this won't pass" → No @ 0.90), directional claims with question-sentence guards, and a calibrated verbal-probability lexicon ("almost certain" → 0.92, "coin flip" → 0.50, "long shot" → 0.12). Short posts qualify when they carry an explicit probability; past-tense vetoes are soft (a post can report a result AND make a prediction). Category classification uses keyword maps from `config.yaml`. Accuracy is pinned by the labeled benchmark in `app/tests/test_extraction_accuracy.py`. |
| `ranker.py` | `compute_ev_score()` (predicted-prob vs market-implied prob, scaled by inverse market odds) and `compute_risk_flags()` (low source credibility, low market liquidity, low sample size, etc.). |
| `resolver.py` | `MarketResolver` — pulls closed Polymarket markets, finds predictions tied to them, marks each prediction correct/incorrect, writes a `SourcePredictionRecord`, recomputes source credibility. |
