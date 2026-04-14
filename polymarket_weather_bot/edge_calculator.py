"""Edge calculator — ensemble-counting + Gaussian fallback probability models."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scipy.stats import norm

from gamma_client import WeatherMarket
from weather_client import ForecastResult

logger = logging.getLogger(__name__)

# Minimum ensemble members to trust counting over Gaussian.
# GFS ensemble has 31 members; anything less falls back to Gaussian.
_MIN_ENSEMBLE_MEMBERS = 10


@dataclass
class Signal:
    """A trading signal generated from comparing forecast vs market price."""
    market: WeatherMarket
    forecast: ForecastResult
    model_prob: float
    market_prob: float
    edge: float
    action: str
    confidence: str
    prob_method: str = "gaussian"  # "ensemble" or "gaussian"


# ── Probability models ───────────────────────────────────────────────────────


def calculate_probability_ensemble(forecast: ForecastResult, market: WeatherMarket) -> float:
    """Count fraction of GFS ensemble members that satisfy the market condition.

    More robust than Gaussian when the distribution is skewed or bimodal
    (e.g. a coin-flip front passage).  Clips to [0.05, 0.95] because even a
    unanimous 31-member ensemble shouldn't produce a 100% bet.
    """
    members = forecast.raw_ensemble
    n = len(members)

    if market.threshold is not None:
        if market.is_over:
            count = sum(1 for t in members if t > market.threshold)
        else:
            count = sum(1 for t in members if t < market.threshold)
        prob = count / n
    elif market.temp_lower is not None and market.temp_upper is not None:
        # Bucket: count members inside [lower-0.5, upper+0.5]
        lo = market.temp_lower - 0.5
        hi = market.temp_upper + 0.5
        count = sum(1 for t in members if lo <= t <= hi)
        prob = count / n
    else:
        return 0.0

    return max(0.05, min(0.95, prob))


def calculate_probability_gaussian(forecast: ForecastResult, market: WeatherMarket) -> float:
    """Gaussian model: temp ~ N(forecast_mean, forecast_std**2).

    Used as fallback when the forecast has too few raw ensemble members
    (e.g. deterministic or NWS source).
    """
    mean = forecast.mean_temp_f
    std = max(forecast.std_temp_f, 0.1)

    if market.threshold is not None:
        if market.is_over:
            prob = 1.0 - norm.cdf(market.threshold, loc=mean, scale=std)
        else:
            prob = norm.cdf(market.threshold, loc=mean, scale=std)
    elif market.temp_lower is not None and market.temp_upper is not None:
        prob = (
            norm.cdf(market.temp_upper + 0.5, loc=mean, scale=std)
            - norm.cdf(market.temp_lower - 0.5, loc=mean, scale=std)
        )
    else:
        return 0.0

    return max(0.01, min(0.99, prob))


def calculate_probability(forecast: ForecastResult, market: WeatherMarket) -> tuple[float, str]:
    """Pick the best available model and return (probability, method_name).

    Prefers ensemble counting when >= 10 raw members are available,
    otherwise falls back to Gaussian.
    """
    if len(forecast.raw_ensemble) >= _MIN_ENSEMBLE_MEMBERS:
        return calculate_probability_ensemble(forecast, market), "ensemble"
    return calculate_probability_gaussian(forecast, market), "gaussian"


# ── Edge calculation ─────────────────────────────────────────────────────────


def calculate_edge(
    forecast: ForecastResult,
    market: WeatherMarket,
    edge_threshold: float = 0.08,
) -> Signal:
    """Compare model probability vs market price and generate a trading signal."""
    model_prob, prob_method = calculate_probability(forecast, market)

    if market.outcome == "Yes":
        market_prob = market.market_price
    else:
        market_prob = 1.0 - market.market_price

    edge = model_prob - market_prob

    if edge > edge_threshold:
        action = "BUY_YES"
    elif edge < -edge_threshold:
        action = "BUY_NO"
    else:
        action = "NO_TRADE"

    # Confidence: ensemble agreement is stronger evidence than Gaussian tail area
    abs_edge = abs(edge)
    if prob_method == "ensemble":
        n = len(forecast.raw_ensemble)
        if market.threshold is not None:
            above = sum(1 for t in forecast.raw_ensemble if t > market.threshold)
            agreement = max(above, n - above) / n
        else:
            agreement = 0.5
        if agreement >= 0.90 and abs_edge > 0.15:
            confidence = "high"
        elif agreement >= 0.75 and abs_edge > 0.10:
            confidence = "medium"
        else:
            confidence = "low"
    else:
        if abs_edge > 0.20 and forecast.source == "open-meteo-ensemble":
            confidence = "high"
        elif abs_edge > 0.12:
            confidence = "medium"
        else:
            confidence = "low"

    platform = getattr(market, "platform", "polymarket")

    signal = Signal(
        market=market, forecast=forecast,
        model_prob=model_prob, market_prob=market_prob,
        edge=edge, action=action, confidence=confidence,
        prob_method=prob_method,
    )

    if action != "NO_TRADE":
        logger.info(
            "SIGNAL [%s/%s]: %s %s | Model: %.1f%% vs Market: %.1f%% | Edge: %+.1f%% | %s",
            platform, prob_method, action, market.question[:55],
            model_prob * 100, market_prob * 100, edge * 100, confidence,
        )

    return signal
