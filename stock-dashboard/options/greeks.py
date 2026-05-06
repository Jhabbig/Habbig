#!/usr/bin/env python3
"""
Black-Scholes Greeks Calculator

Computes delta, gamma, vega, theta, and rho for options.
Includes caching and batch processing for efficiency.
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple
from functools import lru_cache

import numpy as np
from scipy.stats import norm

log = logging.getLogger("options_greeks")

# Cache parameters: (S, K, T, r, sigma) -> (delta, gamma, vega, theta, rho)
_greeks_cache: Dict[Tuple, Dict] = {}
_CACHE_TTL = 3600  # 1 hour


@dataclass
class GreeksResult:
    """Greeks for a single option."""
    delta: float         # ∂Price/∂S — directional sensitivity
    gamma: float         # ∂Delta/∂S — delta acceleration
    vega: float          # ∂Price/∂σ — volatility sensitivity (per 1% vol)
    theta: float         # ∂Price/∂t — time decay per day
    rho: float           # ∂Price/∂r — rate sensitivity
    price: float         # Theoretical option price


def _cache_key(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> Tuple:
    """Create a cache key for Greeks."""
    return (round(S, 2), round(K, 2), round(T, 4), round(r, 4), round(sigma, 4), option_type)


def _get_cached(key: Tuple, now: float) -> Optional[Dict]:
    """Get cached Greeks if still valid."""
    entry = _greeks_cache.get(key)
    if entry and (now - entry["ts"]) < _CACHE_TTL:
        return entry["greeks"]
    elif entry:
        del _greeks_cache[key]
    return None


def _set_cached(key: Tuple, greeks: Dict) -> None:
    """Cache Greeks for later use."""
    if len(_greeks_cache) > 5000:
        _greeks_cache.clear()
    _greeks_cache[key] = {"greeks": greeks, "ts": time.time()}


class BlackScholesCalculator:
    """Black-Scholes option pricing and Greeks."""

    def __init__(self, risk_free_rate: float = 0.05):
        """
        Args:
            risk_free_rate: Annual risk-free rate (default 5%)
        """
        self.r = risk_free_rate

    @staticmethod
    def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
        """Compute d1 in Black-Scholes formula."""
        if T <= 0 or sigma <= 0:
            return 0
        numerator = np.log(S / K) + (r + 0.5 * sigma**2) * T
        denominator = sigma * np.sqrt(T)
        return numerator / denominator

    @staticmethod
    def _d2(d1: float, sigma: float, T: float) -> float:
        """Compute d2 in Black-Scholes formula."""
        if T <= 0 or sigma <= 0:
            return 0
        return d1 - sigma * np.sqrt(T)

    def call_price(self, S: float, K: float, T: float, sigma: float) -> float:
        """Black-Scholes call option price."""
        if T <= 0:
            return max(S - K, 0)
        if sigma <= 0:
            return max(S - np.exp(-self.r * T) * K, 0)

        d1 = self._d1(S, K, T, self.r, sigma)
        d2 = self._d2(d1, sigma, T)

        call = S * norm.cdf(d1) - K * np.exp(-self.r * T) * norm.cdf(d2)
        return max(call, 0)

    def put_price(self, S: float, K: float, T: float, sigma: float) -> float:
        """Black-Scholes put option price."""
        if T <= 0:
            return max(K - S, 0)
        if sigma <= 0:
            return max(np.exp(-self.r * T) * K - S, 0)

        d1 = self._d1(S, K, T, self.r, sigma)
        d2 = self._d2(d1, sigma, T)

        put = K * np.exp(-self.r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        return max(put, 0)

    def greeks_call(self, S: float, K: float, T: float, sigma: float) -> GreeksResult:
        """Compute all Greeks for a call option."""
        cache_key = _cache_key(S, K, T, self.r, sigma, "call")
        cached = _get_cached(cache_key, time.time())
        if cached:
            return cached

        if T <= 0 or sigma <= 0:
            # Deep ITM or expired
            return GreeksResult(
                delta=1.0 if S > K else 0.0,
                gamma=0.0,
                vega=0.0,
                theta=0.0,
                rho=0.0,
                price=max(S - K, 0),
            )

        d1 = self._d1(S, K, T, self.r, sigma)
        d2 = self._d2(d1, sigma, T)

        # Delta: N(d1)
        delta = norm.cdf(d1)

        # Gamma: n(d1) / (S * sigma * sqrt(T))
        gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T)) if sigma > 0 else 0

        # Vega: S * n(d1) * sqrt(T) / 100 (per 1% vol)
        vega = S * norm.pdf(d1) * np.sqrt(T) / 100 if sigma > 0 else 0

        # Theta: -(S * n(d1) * sigma) / (2 * sqrt(T)) - r * K * exp(-r*T) * N(d2)
        # Per day, so divide by 365
        theta_annual = -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - self.r * K * np.exp(-self.r * T) * norm.cdf(d2)
        theta = theta_annual / 365 if T > 0 else 0

        # Rho: K * T * exp(-r*T) * N(d2) / 100 (per 1% rate)
        rho = K * T * np.exp(-self.r * T) * norm.cdf(d2) / 100

        price = self.call_price(S, K, T, sigma)

        result = GreeksResult(
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            rho=rho,
            price=price,
        )

        _set_cached(cache_key, result)
        return result

    def greeks_put(self, S: float, K: float, T: float, sigma: float) -> GreeksResult:
        """Compute all Greeks for a put option."""
        cache_key = _cache_key(S, K, T, self.r, sigma, "put")
        cached = _get_cached(cache_key, time.time())
        if cached:
            return cached

        if T <= 0 or sigma <= 0:
            return GreeksResult(
                delta=-1.0 if S < K else 0.0,
                gamma=0.0,
                vega=0.0,
                theta=0.0,
                rho=0.0,
                price=max(K - S, 0),
            )

        d1 = self._d1(S, K, T, self.r, sigma)
        d2 = self._d2(d1, sigma, T)

        # Delta: -N(-d1) = N(d1) - 1
        delta = norm.cdf(d1) - 1

        # Gamma: same as call
        gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T)) if sigma > 0 else 0

        # Vega: same as call
        vega = S * norm.pdf(d1) * np.sqrt(T) / 100 if sigma > 0 else 0

        # Theta: (S * n(d1) * sigma) / (2 * sqrt(T)) + r * K * exp(-r*T) * N(-d2)
        # Per day
        theta_annual = (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) + self.r * K * np.exp(-self.r * T) * norm.cdf(-d2)
        theta = theta_annual / 365 if T > 0 else 0

        # Rho: -K * T * exp(-r*T) * N(-d2) / 100
        rho = -K * T * np.exp(-self.r * T) * norm.cdf(-d2) / 100

        price = self.put_price(S, K, T, sigma)

        result = GreeksResult(
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            rho=rho,
            price=price,
        )

        _set_cached(cache_key, result)
        return result

    def greeks_batch(
        self,
        S: float,
        K_list: List[float],
        T: float,
        sigma: float,
        option_type: str = "call",
    ) -> List[GreeksResult]:
        """
        Compute Greeks for multiple strikes efficiently.

        Useful for Greeks surface or chain analysis.
        """
        greeks_fn = self.greeks_call if option_type == "call" else self.greeks_put
        return [greeks_fn(S, K, T, sigma) for K in K_list]

    def implied_volatility(
        self,
        S: float,
        K: float,
        T: float,
        market_price: float,
        option_type: str = "call",
        initial_guess: float = 0.20,
        max_iterations: int = 100,
        tolerance: float = 1e-6,
    ) -> Optional[float]:
        """
        Back out implied volatility from market price using Newton-Raphson.

        Args:
            S, K, T: Spot, strike, time to expiration
            market_price: Observed option price
            option_type: 'call' or 'put'
            initial_guess: Starting sigma guess (default 20%)
            max_iterations: Max iterations for convergence
            tolerance: Convergence tolerance

        Returns: Implied volatility, or None if doesn't converge
        """
        price_fn = self.call_price if option_type == "call" else self.put_price
        greeks_fn = self.greeks_call if option_type == "call" else self.greeks_put

        sigma = initial_guess
        for i in range(max_iterations):
            greeks = greeks_fn(S, K, T, sigma)
            price_diff = greeks.price - market_price
            vega = greeks.vega

            if abs(price_diff) < tolerance:
                return sigma
            if vega < 1e-6:
                return None  # Vega too small

            sigma = sigma - price_diff / vega
            sigma = max(0.001, min(5.0, sigma))  # Keep in reasonable range

        # Failed to converge
        log.warning(f"IV convergence failed after {max_iterations} iterations")
        return None

    def portfolio_greeks(self, positions: List[Dict]) -> Dict[str, float]:
        """
        Aggregate Greeks across a portfolio of positions.

        Args:
            positions: List of dicts with keys:
                - ticker, quantity, strike, expiration_days, volatility, option_type

        Returns: Aggregated delta, gamma, vega, theta, rho
        """
        total_greeks = {
            "delta": 0.0,
            "gamma": 0.0,
            "vega": 0.0,
            "theta": 0.0,
            "rho": 0.0,
        }

        for pos in positions:
            greeks_fn = self.greeks_call if pos["option_type"] == "call" else self.greeks_put
            T = pos["expiration_days"] / 365.0
            greeks = greeks_fn(
                pos["ticker"],  # Assume ticker value is spot price for simplicity
                pos["strike"],
                T,
                pos["volatility"],
            )

            qty = pos["quantity"]
            total_greeks["delta"] += greeks.delta * qty
            total_greeks["gamma"] += greeks.gamma * qty
            total_greeks["vega"] += greeks.vega * qty
            total_greeks["theta"] += greeks.theta * qty
            total_greeks["rho"] += greeks.rho * qty

        return total_greeks


def example_usage():
    """Demonstrate Greeks calculation."""
    logging.basicConfig(level=logging.INFO)

    calc = BlackScholesCalculator(risk_free_rate=0.05)

    # Example: AAPL call option
    S = 150.0  # Spot price
    K = 155.0  # Strike
    T = 30 / 365.0  # 30 days to expiration
    sigma = 0.25  # 25% volatility

    greeks = calc.greeks_call(S, K, T, sigma)
    print(f"\nAAPL Call ($155 strike, 30 DTE, 25% vol)")
    print(f"  Price: ${greeks.price:.2f}")
    print(f"  Delta: {greeks.delta:.4f}")
    print(f"  Gamma: {greeks.gamma:.6f}")
    print(f"  Vega: {greeks.vega:.2f} per 1% vol")
    print(f"  Theta: ${greeks.theta:.2f} per day")
    print(f"  Rho: {greeks.rho:.2f} per 1% rate")

    # Test IV calculation
    market_price = 3.50
    iv = calc.implied_volatility(S, K, T, market_price, option_type="call")
    print(f"\nImplied Vol from ${market_price:.2f} price: {iv*100:.1f}% (should be ~25%)")


if __name__ == "__main__":
    example_usage()
