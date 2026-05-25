#!/usr/bin/env python3
"""
Stop-Loss & Take-Profit System

Implements multiple stop-loss and take-profit strategies:
1. Volatility-based (ATR multiples)
2. Percentage-based
3. Time-based
4. Support/Resistance-based
"""

import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

import numpy as np

log = logging.getLogger("risk_stops")


class StopType(Enum):
    """Types of stops."""
    VOLATILITY_ATR = "volatility_atr"
    PERCENTAGE = "percentage"
    TIME_BASED = "time_based"
    SUPPORT_RESISTANCE = "support_resistance"


@dataclass
class StopLevelResult:
    """Result of stop-loss/take-profit calculation."""
    stop_price: float
    target_price: float
    risk_per_share: float
    risk_reward_ratio: float
    stop_type: StopType
    reasoning: str


class StopManager:
    """Manage stop-loss and take-profit levels for positions."""

    @staticmethod
    def volatility_based_stop(
        entry_price: float,
        atr_14: float,
        atr_multiplier: float = 2.0,
        side: str = "BUY",
    ) -> StopLevelResult:
        """
        Volatility-based stop using ATR (Average True Range).

        Args:
            entry_price: Entry price
            atr_14: 14-period ATR
            atr_multiplier: How many ATRs below/above entry (default 2.0)
            side: "BUY" or "SHORT"

        Returns: StopLevelResult with stop and target prices
        """
        risk_per_share = atr_14 * atr_multiplier

        if side == "BUY":
            stop_price = entry_price - risk_per_share
            target_price = entry_price + (risk_per_share * 1.5)  # 1.5:1 RR
        else:  # SHORT
            stop_price = entry_price + risk_per_share
            target_price = entry_price - (risk_per_share * 1.5)

        rr_ratio = abs(entry_price - target_price) / risk_per_share if risk_per_share > 0 else 0

        reasoning = (
            f"ATR-based: {atr_multiplier}x ATR (${atr_14:.2f}) = ${risk_per_share:.2f} risk/share. "
            f"RR ratio: {rr_ratio:.2f}:1"
        )

        return StopLevelResult(
            stop_price=stop_price,
            target_price=target_price,
            risk_per_share=risk_per_share,
            risk_reward_ratio=rr_ratio,
            stop_type=StopType.VOLATILITY_ATR,
            reasoning=reasoning,
        )

    @staticmethod
    def percentage_based_stop(
        entry_price: float,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float = 6.0,
        side: str = "BUY",
    ) -> StopLevelResult:
        """
        Percentage-based stop and target.

        Args:
            entry_price: Entry price
            stop_loss_pct: Stop loss % below entry (e.g., 2.0)
            take_profit_pct: Take profit % above entry (e.g., 6.0)
            side: "BUY" or "SHORT"

        Returns: StopLevelResult
        """
        if side == "BUY":
            stop_price = entry_price * (1 - stop_loss_pct / 100)
            target_price = entry_price * (1 + take_profit_pct / 100)
        else:  # SHORT
            stop_price = entry_price * (1 + stop_loss_pct / 100)
            target_price = entry_price * (1 - take_profit_pct / 100)

        risk_per_share = abs(entry_price - stop_price)
        rr_ratio = (abs(target_price - entry_price) / risk_per_share) if risk_per_share > 0 else 0

        reasoning = f"Percentage-based: {stop_loss_pct}% stop, {take_profit_pct}% target. RR: {rr_ratio:.2f}:1"

        return StopLevelResult(
            stop_price=stop_price,
            target_price=target_price,
            risk_per_share=risk_per_share,
            risk_reward_ratio=rr_ratio,
            stop_type=StopType.PERCENTAGE,
            reasoning=reasoning,
        )

    @staticmethod
    def time_based_stop(
        entry_price: float,
        entry_time_minutes: int,
        atr_14: float,
        max_hold_bars: int = 5,
        bar_minutes: int = 60,
        side: str = "BUY",
    ) -> Dict[str, any]:
        """
        Time-based stop: exit if no momentum in N bars.

        Args:
            entry_price: Entry price
            entry_time_minutes: When position was entered (minutes since midnight)
            atr_14: ATR for volatility reference
            max_hold_bars: Exit if no progress in this many bars
            bar_minutes: Bar size (60 = 1-hour bars)
            side: "BUY" or "SHORT"

        Returns: Dict with exit_time_minutes and stop price
        """
        exit_time = entry_time_minutes + (max_hold_bars * bar_minutes)
        risk_per_share = atr_14 * 2  # 2 ATR stop

        if side == "BUY":
            stop_price = entry_price - risk_per_share
        else:
            stop_price = entry_price + risk_per_share

        return {
            "exit_time_minutes": exit_time,
            "stop_price": stop_price,
            "reasoning": f"Exit at {exit_time} min if no progress (time-based stop after {max_hold_bars} bars)",
        }

    @staticmethod
    def support_resistance_stop(
        entry_price: float,
        support_price: float,
        resistance_price: float,
        side: str = "BUY",
    ) -> StopLevelResult:
        """
        Support/Resistance-based stop.

        Args:
            entry_price: Entry price
            support_price: Support level (below for long, above for short)
            resistance_price: Resistance level (above for long, below for short)
            side: "BUY" or "SHORT"

        Returns: StopLevelResult
        """
        if side == "BUY":
            # Long: place stop below support, target at resistance
            stop_price = support_price * 0.99  # Slightly below
            target_price = resistance_price
        else:  # SHORT
            # Short: place stop above resistance, target at support
            stop_price = resistance_price * 1.01  # Slightly above
            target_price = support_price

        risk_per_share = abs(entry_price - stop_price)
        rr_ratio = (abs(target_price - entry_price) / risk_per_share) if risk_per_share > 0 else 0

        reasoning = (
            f"Support/Resistance-based: "
            f"Support ${support_price:.2f}, Resistance ${resistance_price:.2f}. "
            f"RR: {rr_ratio:.2f}:1"
        )

        return StopLevelResult(
            stop_price=stop_price,
            target_price=target_price,
            risk_per_share=risk_per_share,
            risk_reward_ratio=rr_ratio,
            stop_type=StopType.SUPPORT_RESISTANCE,
            reasoning=reasoning,
        )

    @staticmethod
    def trailing_stop(
        entry_price: float,
        current_price: float,
        highest_price: float,
        trailing_pct: float = 2.0,
        side: str = "BUY",
    ) -> Optional[float]:
        """
        Trailing stop: follows profitable positions upward.

        Args:
            entry_price: Original entry
            current_price: Current market price
            highest_price: Highest price since entry
            trailing_pct: Trail by this % below highest
            side: "BUY" or "SHORT"

        Returns: Stop price if trailing stop should be tighter, else None
        """
        if side == "BUY":
            # Long: move stop up as price rises
            if current_price < entry_price:
                # Position is underwater, no trailing stop yet
                return None
            trailing_stop = highest_price * (1 - trailing_pct / 100)
            # Only tighten if this is higher than original stop
            return max(entry_price * 0.98, trailing_stop)  # But keep at least 2% below entry
        else:  # SHORT
            if current_price > entry_price:
                return None
            trailing_stop = highest_price * (1 + trailing_pct / 100)
            return min(entry_price * 1.02, trailing_stop)

    @staticmethod
    def breakeven_stop(
        entry_price: float,
        current_price: float,
        atr_14: float,
        profit_threshold_pct: float = 1.0,
        side: str = "BUY",
    ) -> Optional[float]:
        """
        Move stop to breakeven once position is profitable.

        Args:
            entry_price: Entry price
            current_price: Current market price
            atr_14: ATR (for small buffer)
            profit_threshold_pct: Trigger at this % profit
            side: "BUY" or "SHORT"

        Returns: Breakeven stop price, or None if not profitable enough
        """
        if side == "BUY":
            profit_pct = (current_price - entry_price) / entry_price * 100
            if profit_pct > profit_threshold_pct:
                # Move stop to entry + buffer
                return entry_price + (atr_14 * 0.5)  # Small buffer
        else:  # SHORT
            profit_pct = (entry_price - current_price) / entry_price * 100
            if profit_pct > profit_threshold_pct:
                return entry_price - (atr_14 * 0.5)

        return None

    @staticmethod
    def scale_out_levels(
        entry_price: float,
        target_price: float,
        num_scales: int = 3,
        side: str = "BUY",
    ) -> List[Tuple[float, int]]:
        """
        Scale out of position in multiple levels.

        Args:
            entry_price: Entry price
            target_price: Target price
            num_scales: Number of scale-out levels
            side: "BUY" or "SHORT"

        Returns: List of (price, shares_to_sell_pct) tuples
        """
        if side == "BUY":
            step = (target_price - entry_price) / num_scales
        else:
            step = (entry_price - target_price) / num_scales

        levels = []
        for i in range(1, num_scales + 1):
            if side == "BUY":
                level_price = entry_price + (step * i)
            else:
                level_price = entry_price - (step * i)

            # Sell 1/3 at each level (or adjust distribution)
            pct_to_sell = 100 / num_scales
            levels.append((level_price, pct_to_sell))

        return levels


def example_usage():
    """Demonstrate stop-loss and take-profit calculation."""
    logging.basicConfig(level=logging.INFO)

    entry = 150.0
    atr = 2.50

    # Example 1: ATR-based
    result1 = StopManager.volatility_based_stop(
        entry_price=entry,
        atr_14=atr,
        atr_multiplier=2.0,
        side="BUY",
    )
    print(f"\n1. ATR-Based Stop (2x ATR)")
    print(f"   Stop: ${result1.stop_price:.2f}")
    print(f"   Target: ${result1.target_price:.2f}")
    print(f"   RR: {result1.risk_reward_ratio:.2f}:1")

    # Example 2: Percentage-based
    result2 = StopManager.percentage_based_stop(
        entry_price=entry,
        stop_loss_pct=2.0,
        take_profit_pct=6.0,
        side="BUY",
    )
    print(f"\n2. Percentage-Based Stop (2% loss, 6% target)")
    print(f"   Stop: ${result2.stop_price:.2f}")
    print(f"   Target: ${result2.target_price:.2f}")
    print(f"   RR: {result2.risk_reward_ratio:.2f}:1")

    # Example 3: Support/Resistance
    result3 = StopManager.support_resistance_stop(
        entry_price=entry,
        support_price=147.0,
        resistance_price=160.0,
        side="BUY",
    )
    print(f"\n3. Support/Resistance Stop")
    print(f"   Stop: ${result3.stop_price:.2f}")
    print(f"   Target: ${result3.target_price:.2f}")
    print(f"   RR: {result3.risk_reward_ratio:.2f}:1")

    # Example 4: Scale-out levels
    scales = StopManager.scale_out_levels(
        entry_price=entry,
        target_price=160.0,
        num_scales=3,
        side="BUY",
    )
    print(f"\n4. Scale-Out Levels (3 levels)")
    for i, (price, pct) in enumerate(scales, 1):
        print(f"   Level {i}: ${price:.2f} (sell {pct:.0f}%)")


if __name__ == "__main__":
    example_usage()
