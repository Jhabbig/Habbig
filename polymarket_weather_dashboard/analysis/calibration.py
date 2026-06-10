"""Model calibration + skill metrics on historical year-end count series.

For each model with a hand-curated truth table we compute:

  * RMSE: root-mean-squared error of the pre-season climo prior vs actual.
  * Median absolute error (MAE).
  * Coverage rate: in what fraction of years did the model's 80% credible
    interval actually contain the realised year-end count?
  * Brier score for the "did the year exceed the climo median?" forecast.
    The forecast probability is P(X >= median | NB(mu_climo, alpha)).
  * Log loss (binary) for the same exceedance event.

These metrics are what professional quant shops report when claiming "our
model is well-calibrated." Showing them publicly is what separates a serious
forecasting product from a hand-wave: traders can decide for themselves
whether the projected probabilities are worth trusting.
"""
from __future__ import annotations

import math

from analysis.negbin import ALPHA, nb_cdf_at_least, nb_quantile_band

# Reuse the truth tables from the backtest module
from analysis.backtest import (
    ATLANTIC_NAMED_STORMS_BY_YEAR,
    ANNUAL_ACRES_BY_YEAR,
)

# Hand-curated tornado + FEMA actuals (1991-2020 climo-anchored, 2014-2024)
US_TORNADOES_BY_YEAR: dict[int, int] = {
    2014: 911, 2015: 1281, 2016: 1057, 2017: 1525, 2018: 1126,
    2019: 1517, 2020: 1075, 2021: 1376, 2022: 1331, 2023: 1426, 2024: 1735,
}

# OpenFEMA YTD-by-year-end for major (DR) declarations only. Pulled from
# the OpenFEMA archive 2024-Q4 snapshot, de-duped at the disasterNumber level.
FEMA_DR_BY_YEAR: dict[int, int] = {
    2014: 45, 2015: 43, 2016: 60, 2017: 59, 2018: 58, 2019: 62,
    2020: 59, 2021: 50, 2022: 90, 2023: 56, 2024: 73,
}

# Per-domain climatological mean. For series with their own historical
# climo (e.g. NIFC), use the truth-table mean.
def _climo_mean(series: dict[int, float]) -> float:
    return sum(series.values()) / len(series)


def _domain_alpha(domain: str) -> float:
    return ALPHA.get(domain, 0.0)


def _rmse(rows: list[float]) -> float:
    if not rows:
        return 0.0
    return math.sqrt(sum(r * r for r in rows) / len(rows))


def _mae(rows: list[float]) -> float:
    if not rows:
        return 0.0
    s = sorted(abs(r) for r in rows)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _score_series(name: str, alpha_key: str, truth: dict[int, float]) -> dict:
    if not truth:
        return {"name": name, "rows": 0}
    alpha = _domain_alpha(alpha_key)
    years = sorted(truth.keys())
    mu_climo = _climo_mean(truth)
    median_climo = sorted(truth.values())[len(truth) // 2]
    # Leave-one-out climatology — for each target year, climo mean is computed
    # across other years to avoid look-ahead bias.
    errs: list[float] = []
    covered_in_80: list[bool] = []
    covered_in_95: list[bool] = []
    brier_pieces: list[float] = []
    log_loss_pieces: list[float] = []
    for y in years:
        actual = truth[y]
        loo = [v for yy, v in truth.items() if yy != y]
        loo_mean = sum(loo) / len(loo)
        errs.append(loo_mean - actual)
        # Credible-interval coverage uses the LOO climo mean as μ + alpha
        band80 = nb_quantile_band(loo_mean, alpha, ci=0.80)
        band95 = nb_quantile_band(loo_mean, alpha, ci=0.95)
        if band80:
            covered_in_80.append(band80[0] <= actual <= band80[1])
        if band95:
            covered_in_95.append(band95[0] <= actual <= band95[1])
        # Brier + log loss for "did the year exceed climo median?"
        loo_median = sorted(loo)[len(loo) // 2]
        # Forecast probability: P(X >= median | NB(μ_loo, α))
        p_exceed = nb_cdf_at_least(int(round(loo_median)) + 1, loo_mean, alpha)
        outcome = 1.0 if actual > loo_median else 0.0
        brier_pieces.append((p_exceed - outcome) ** 2)
        # Clamp p to avoid log(0)
        p_safe = min(max(p_exceed, 1e-6), 1.0 - 1e-6)
        log_loss_pieces.append(-(outcome * math.log(p_safe) + (1 - outcome) * math.log(1 - p_safe)))
    return {
        "name": name,
        "alpha": alpha,
        "rows": len(years),
        "year_min": min(years),
        "year_max": max(years),
        "climo_mean": round(mu_climo, 2),
        "climo_median": round(median_climo, 2),
        "rmse": round(_rmse(errs), 2),
        "mae": round(_mae(errs), 2),
        "coverage_80_pct": round(100 * sum(covered_in_80) / len(covered_in_80), 1) if covered_in_80 else None,
        "coverage_95_pct": round(100 * sum(covered_in_95) / len(covered_in_95), 1) if covered_in_95 else None,
        "brier_score": round(sum(brier_pieces) / len(brier_pieces), 4) if brier_pieces else None,
        "log_loss": round(sum(log_loss_pieces) / len(log_loss_pieces), 4) if log_loss_pieces else None,
    }


def report() -> dict:
    return {
        "models": [
            _score_series("Atlantic named storms", "atlantic_named_storms",
                          ATLANTIC_NAMED_STORMS_BY_YEAR),
            _score_series("US wildfire acres burned", "wildfire_count",
                          {y: v / 1_000_000 for y, v in ANNUAL_ACRES_BY_YEAR.items()}),
            _score_series("US tornadoes", "us_tornadoes", US_TORNADOES_BY_YEAR),
            _score_series("FEMA major-disaster (DR) declarations", "fema_dr",
                          FEMA_DR_BY_YEAR),
        ],
        "method": (
            "Leave-one-out: for each historical year, project the climo mean "
            "across other years, then score against the realised value. RMSE "
            "and MAE are in domain units. Coverage % is the fraction of years "
            "where the realised value fell inside the model's predicted 80% "
            "(or 95%) credible interval - a well-calibrated model should hit "
            "80% / 95%. Brier and log loss are for 'did the year exceed the "
            "climo median?' - lower is better, perfect = 0."
        ),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(report(), indent=2))
