"""Edge calculator — ensemble-counting + Gaussian fallback probability models."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from scipy.stats import norm

from gamma_client import WeatherMarket
from orderbook import book_midpoint, effective_buy_price, synthesize_no_book
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
    edge_yes: float = 0.0
    edge_no: float = 0.0
    exec_price_yes: Optional[float] = None
    exec_price_no: Optional[float] = None
    price_tier_yes: str = "last"  # "book" / "midpoint" / "last"
    price_tier_no: str = "last"
    price_tier: str = "last"  # tier of the side the signed edge points to


# ── Probability models ───────────────────────────────────────────────────────


def calculate_probability_ensemble(forecast: ForecastResult, market: WeatherMarket) -> float:
    """Count GFS ensemble members that satisfy the market condition.

    More robust than Gaussian when the distribution is skewed or bimodal
    (e.g. a coin-flip front passage).  Uses the Laplace estimator
    (m+1)/(n+2) — the honest probability from n samples: a unanimous
    31-member ensemble yields ~0.97, never 1.0, without a hard clip that
    would manufacture fake edge on correctly-extreme-priced markets.
    """
    members = forecast.raw_ensemble
    n = len(members)

    if market.threshold is not None:
        if market.is_over:
            count = sum(1 for t in members if t > market.threshold)
        else:
            count = sum(1 for t in members if t < market.threshold)
    elif market.temp_lower is not None and market.temp_upper is not None:
        # Bucket: count members inside [lower-0.5, upper+0.5]
        lo = market.temp_lower - 0.5
        hi = market.temp_upper + 0.5
        count = sum(1 for t in members if lo <= t <= hi)
    else:
        return 0.0

    return (count + 1) / (n + 2)


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
        prob = norm.cdf(market.temp_upper + 0.5, loc=mean, scale=std) - norm.cdf(market.temp_lower - 0.5, loc=mean, scale=std)
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
    yes_book: Optional[dict] = None,
    no_book: Optional[dict] = None,
    probe_notional: float = 50.0,
) -> Signal:
    """Compare model probability vs executable market prices and generate a signal.

    Edge is computed against what a trade would actually cost: walk the order
    book asks for the effective fill price of a probe_notional-sized buy
    ("book" tier), else bid/ask midpoint ("midpoint"), else last/quoted price
    ("last").  edge_yes prices the YES fill, edge_no the NO fill — the two are
    no longer forced to be complements, so the spread is paid honestly.
    """
    model_prob, prob_method = calculate_probability(forecast, market)

    last_yes = market.market_price if market.outcome == "Yes" else 1.0 - market.market_price
    last_yes = min(max(last_yes, 0.0), 1.0)

    if no_book is None and yes_book is not None:
        no_book = synthesize_no_book(yes_book)

    exec_yes, tier_yes = effective_buy_price(yes_book, probe_notional, last_yes)
    if no_book is not None:
        exec_no, tier_no = effective_buy_price(no_book, probe_notional, 1.0 - last_yes)
    else:
        mid = book_midpoint(yes_book)
        if mid is not None:
            exec_no, tier_no = 1.0 - mid, "midpoint"
        else:
            exec_no, tier_no = 1.0 - last_yes, "last"

    market_prob = exec_yes
    edge_yes = model_prob - exec_yes
    edge_no = (1.0 - model_prob) - exec_no

    # Signed convention kept from the old single-edge model:
    # positive edge → YES side is cheap, negative → NO side is cheap.
    # When neither side is cheap (both edges <= 0) report edge_yes so the
    # sign can never flip positive on a fairly-priced spread.
    if edge_no > edge_yes and edge_no > 0:
        edge = -edge_no
    else:
        edge = edge_yes

    if edge_yes > edge_threshold and edge_yes >= edge_no:
        action = "BUY_YES"
    elif edge_no > edge_threshold:
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
    price_tier = tier_yes if edge >= 0 else tier_no

    signal = Signal(
        market=market,
        forecast=forecast,
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        action=action,
        confidence=confidence,
        prob_method=prob_method,
        edge_yes=edge_yes,
        edge_no=edge_no,
        exec_price_yes=exec_yes,
        exec_price_no=exec_no,
        price_tier_yes=tier_yes,
        price_tier_no=tier_no,
        price_tier=price_tier,
    )

    if action != "NO_TRADE":
        logger.info(
            "SIGNAL [%s/%s]: %s %s | Model: %.1f%% vs Market: %.1f%% (%s) | Edge: %+.1f%% | %s",
            platform,
            prob_method,
            action,
            market.question[:55],
            model_prob * 100,
            market_prob * 100,
            price_tier,
            edge * 100,
            confidence,
        )

    return signal
