#!/usr/bin/env python3
"""
Position Sizing Engine for Stock Trading

Implements multiple sizing strategies:
1. Fixed Fractional Kelly — f * Kelly % of portfolio per trade
2. Volatility-Adjusted ATR — size based on market volatility
3. Correlation-Aware — reduce size if similar sector position exists
4. Confidence-Scaled — adjust for ML signal confidence

Input: account equity, confidence, volatility, correlations
Output: max position size in shares/dollars
"""

import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("stock_risk_sizing")


@dataclass
class SizingParams:
    """Input parameters for position sizing."""
    account_equity: float        # Total account balance in USD
    confidence_score: float      # 0-1, from ML model
    atr_14: float               # 14-period ATR
    current_price: float        # Current stock price
    sector_etf_correlation: float = 0.7  # Correlation to sector (e.g., XLK)
    market_correlation: float = 0.5       # Correlation to SPY
    existing_sector_exposure: float = 0.0 # % of portfolio already in sector
    existing_correlation_bucket_exposure: float = 0.0  # % in correlation bucket
    max_portfolio_pct: float = 0.05  # Max 5% per trade (hard limit)
    max_sector_pct: float = 0.30    # Max 30% sector exposure
    max_correlation_bucket_pct: float = 0.10  # Max 10% per correlation bucket


@dataclass
class SizingResult:
    """Output of position sizing."""
    max_shares: int
    max_usd: float
    sizing_method: str
    kelly_pct: float
    volatility_adjustment: float
    correlation_adjustment: float
    confidence_adjustment: float
    reasoning: str


class PositionSizer:
    """Professional position sizing with multiple strategies."""

    def __init__(self, kelly_fraction: float = 0.25):
        """
        Args:
            kelly_fraction: Fractional Kelly (0.25 is safe, 1.0 is full Kelly)
        """
        self.kelly_fraction = kelly_fraction

    def size_position(self, params: SizingParams) -> SizingResult:
        """
        Size a position using the conservative Kelly strategy with adjustments.

        Returns SizingResult with recommended shares and reasoning.
        """
        adjustments = {
            "volatility": 1.0,
            "correlation": 1.0,
            "confidence": 1.0,
        }

        # Step 1: Volatility adjustment
        # Higher volatility → smaller position
        # Use ATR as volatility proxy; normalize to recent market vol
        atr_ratio = params.atr_14 / params.current_price if params.current_price > 0 else 0.02
        # If ATR is 2% of price, vol adjustment = 1.0
        # If ATR is 4% of price, vol adjustment = 0.5
        adjustments["volatility"] = max(0.3, 1.0 / (1.0 + atr_ratio * 10))

        # Step 2: Correlation adjustment
        # If we already have sector exposure, reduce new position
        # Correlation buckets: [0-0.3], [0.3-0.6], [0.6-1.0]
        sector_impact = params.existing_sector_exposure / params.max_sector_pct if params.max_sector_pct > 0 else 0
        adjustments["correlation"] = 1.0 - (sector_impact * 0.5)

        # If sector correlation is high, also penalize
        if params.sector_etf_correlation > 0.7:
            adjustments["correlation"] *= 0.8

        # Step 3: Confidence adjustment
        # Lower confidence → smaller position
        # At 0.5 confidence, size = 0.5x
        # At 1.0 confidence, size = 1.0x
        adjustments["confidence"] = params.confidence_score

        # Step 4: Calculate Kelly %
        # Assume win rate and RR from confidence:
        # confidence 0.6 => assume 55% win rate with 1.5:1 RR
        win_rate = 0.5 + (params.confidence_score * 0.1)  # 0.5 -> 0.6 range
        avg_win_rr = 1.5  # Assume 1.5:1 risk-reward
        kelly_pct = (win_rate * avg_win_rr - (1 - win_rate)) / avg_win_rr if avg_win_rr > 0 else 0.01
        kelly_pct = max(0.01, min(0.10, kelly_pct))  # Clamp 1-10%

        # Apply fractional Kelly
        kelly_pct *= self.kelly_fraction

        # Step 5: Compute base position size
        base_risk_dollars = params.account_equity * kelly_pct
        risk_per_atr = params.atr_14 * 2  # Risk 2 ATRs
        base_shares = int(base_risk_dollars / risk_per_atr) if risk_per_atr > 0 else 0
        base_usd = base_shares * params.current_price

        # Step 6: Apply adjustments
        adjusted_usd = base_usd
        adjusted_usd *= adjustments["volatility"]
        adjusted_usd *= adjustments["correlation"]
        adjusted_usd *= adjustments["confidence"]

        adjusted_shares = int(adjusted_usd / params.current_price) if params.current_price > 0 else 0

        # Step 7: Enforce hard limits
        max_portfolio_usd = params.account_equity * params.max_portfolio_pct
        max_sector_usd = params.account_equity * (params.max_sector_pct - params.existing_sector_exposure)
        max_correlation_usd = params.account_equity * (
            params.max_correlation_bucket_pct - params.existing_correlation_bucket_exposure
        )

        max_usd = min(
            adjusted_usd,
            max_portfolio_usd,
            max_sector_usd,
            max_correlation_usd,
        )
        max_shares = int(max_usd / params.current_price) if params.current_price > 0 else 0

        reasoning = (
            f"Kelly {kelly_pct*100:.1f}% (confidence-scaled). "
            f"Vol adj {adjustments['volatility']:.2f}x, "
            f"Corr adj {adjustments['correlation']:.2f}x, "
            f"Conf adj {adjustments['confidence']:.2f}x. "
            f"Base: {base_shares}sh, Adjusted: {adjusted_shares}sh, Final: {max_shares}sh "
            f"(${max_usd:.0f}). "
        )

        if params.existing_sector_exposure / params.max_sector_pct > 0.8:
            reasoning += "[SECTOR CAP] "
        if adjustments["volatility"] < 0.7:
            reasoning += "[HIGH VOL] "

        return SizingResult(
            max_shares=max(0, max_shares),
            max_usd=max(0.0, max_usd),
            sizing_method="Kelly Fractional (0.25)",
            kelly_pct=kelly_pct,
            volatility_adjustment=adjustments["volatility"],
            correlation_adjustment=adjustments["correlation"],
            confidence_adjustment=adjustments["confidence"],
            reasoning=reasoning,
        )

    def size_trade_from_risk(
        self,
        account_equity: float,
        entry_price: float,
        stop_loss_price: float,
        max_loss_pct: float = 0.02,
    ) -> int:
        """
        Size a position based on risk: how much can we lose on this trade?

        Args:
            account_equity: Total account balance
            entry_price: Entry price
            stop_loss_price: Stop loss price
            max_loss_pct: Max loss as % of account (default 2%)

        Returns: Max shares to buy/short
        """
        risk_per_share = abs(entry_price - stop_loss_price)
        if risk_per_share <= 0:
            return 0

        max_loss_dollars = account_equity * max_loss_pct
        max_shares = int(max_loss_dollars / risk_per_share)
        return max(0, max_shares)

    def size_portfolio_allocation(
        self,
        account_equity: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """
        Calculate optimal Kelly percentage based on historical trade statistics.

        Args:
            account_equity: Total account balance
            win_rate: Historical win rate (0-1)
            avg_win: Average winner size (dollars)
            avg_loss: Average loser size (dollars)

        Returns: Kelly percentage as decimal (0-1)
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return 0.025  # Default to 2.5%

        b = avg_win / avg_loss  # Ratio of wins to losses
        p = win_rate
        q = 1 - win_rate

        kelly = (b * p - q) / b
        kelly = max(0.01, min(0.1, kelly))  # Clamp 1-10%
        kelly *= self.kelly_fraction  # Apply fractional Kelly

        return kelly


def example_usage():
    """Demonstrate position sizing."""
    logging.basicConfig(level=logging.INFO)

    sizer = PositionSizer(kelly_fraction=0.25)

    # Example 1: High confidence, low volatility, new position
    params = SizingParams(
        account_equity=100_000,
        confidence_score=0.75,
        atr_14=2.50,
        current_price=150.0,
        sector_etf_correlation=0.65,
        market_correlation=0.45,
        existing_sector_exposure=0.05,
    )

    result = sizer.size_position(params)
    print(f"\nExample 1: High Confidence Tech Stock")
    print(f"  Shares: {result.max_shares}")
    print(f"  USD: ${result.max_usd:,.0f}")
    print(f"  Method: {result.sizing_method}")
    print(f"  Kelly %: {result.kelly_pct*100:.2f}%")
    print(f"  Reasoning: {result.reasoning}")

    # Example 2: Medium confidence, high volatility
    params2 = SizingParams(
        account_equity=100_000,
        confidence_score=0.55,
        atr_14=5.00,  # High volatility
        current_price=150.0,
        sector_etf_correlation=0.75,
        existing_sector_exposure=0.15,
    )

    result2 = sizer.size_position(params2)
    print(f"\nExample 2: Medium Confidence, High Vol")
    print(f"  Shares: {result2.max_shares}")
    print(f"  USD: ${result2.max_usd:,.0f}")
    print(f"  Reasoning: {result2.reasoning}")

    # Example 3: Risk-based sizing
    shares_risk = sizer.size_trade_from_risk(
        account_equity=100_000,
        entry_price=150.0,
        stop_loss_price=145.0,
        max_loss_pct=0.02,
    )
    print(f"\nExample 3: Risk-Based Sizing")
    print(f"  Max shares (2% risk): {shares_risk}")

    # Example 4: Historical Kelly
    kelly = sizer.size_portfolio_allocation(
        account_equity=100_000,
        win_rate=0.55,
        avg_win=500,
        avg_loss=400,
    )
    print(f"\nExample 4: Historical Kelly-Based")
    print(f"  Recommended allocation: {kelly*100:.2f}% per trade")


if __name__ == "__main__":
    example_usage()
