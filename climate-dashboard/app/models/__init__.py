"""Prediction models.

Each module typically exposes:
  projection(...)         current-year forecast
  threshold_probs(...)    P(year-end ≥ T) for a few thresholds
  backtest(...)           replay model 'as of June' for the last N years

Models are kept dependency-light (stdlib only) so they can be unit-tested
without network access.
"""
