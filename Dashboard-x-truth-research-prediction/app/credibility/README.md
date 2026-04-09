# app/credibility/ — Source credibility scoring

Computes per-source credibility scores. The pipeline applies multiple penalties:
exponential decay on old predictions, smoothing toward a prior so brand-new
sources don't get a frequentist 100%, category-spread bonuses (rewarding
sources who only predict in their lane), and dominance penalties (capping
sources who only predict in one tiny niche).

Configured via `app/config.yaml` under the `credibility:` key — half-life,
prior strength, spread map, dominance threshold, etc.

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `engine.py` | `CredibilityEngine` — orchestrates scoring. Reads source records from the DB, applies decay → smoothed accuracy → category penalties, writes a `CredibilitySnapshot`. |
| `category_scores.py` | Per-category credibility computation. `compute_category_credibility()` and `smoothed_accuracy()` (Bayesian smoothing with a prior pseudo-count). |
| `decay.py` | Time-based decay. `decay_weight()` returns `0.5 ** (days_elapsed / half_life_days)`; `decay_weighted_accuracy()` applies it across a list of records. |
| `diversity.py` | `category_spread_penalty()` rewards sources who predict across multiple categories; `category_dominance_penalty()` penalizes sources whose predictions cluster in one category beyond the configured threshold. |
