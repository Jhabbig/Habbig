"""Edge calculator — Gaussian probability model for weather markets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scipy.stats import norm

from gamma_client import WeatherMarket
from weather_client import ForecastResult

logger = logging.getLogger(__name__)


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


def calculate_probability(forecast: ForecastResult, market: WeatherMarket) -> float:
    """Calculate probability that actual temperature satisfies market condition.

    Uses Gaussian model: temp ~ N(forecast_mean, forecast_std²)
    """
    mean = forecast.mean_temp_f
    std = max(forecast.std_temp_f, 0.1)  # Guard against zero std causing division-by-zero in norm.cdf

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


def calculate_edge(
    forecast: ForecastResult,
    market: WeatherMarket,
    edge_threshold: float = 0.08,
) -> Signal:
    """Compare model probability vs market price and generate a trading signal."""
    model_prob = calculate_probability(forecast, market)

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

    abs_edge = abs(edge)
    if abs_edge > 0.20 and forecast.source == "open-meteo-ensemble":
        confidence = "high"
    elif abs_edge > 0.12:
        confidence = "medium"
    else:
        confidence = "low"

    signal = Signal(
        market=market, forecast=forecast,
        model_prob=model_prob, market_prob=market_prob,
        edge=edge, action=action, confidence=confidence,
    )

    if action != "NO_TRADE":
        logger.info(
            "SIGNAL: %s %s | Model: %.1f%% vs Market: %.1f%% | Edge: %+.1f%% | %s",
            action, market.question[:60],
            model_prob * 100, market_prob * 100, edge * 100, confidence,
        )

    return signal
