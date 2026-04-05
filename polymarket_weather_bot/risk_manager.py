"""Risk manager — Kelly criterion sizing and daily loss limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import Config
from edge_calculator import Signal

logger = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Calculated position size for a trade."""
    amount: float
    kelly_fraction: float
    adjusted_fraction: float
    reason: str
    approved: bool


class RiskManager:
    """Manages position sizing and daily loss limits."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.daily_pnl: float = 0.0
        self.open_positions: dict = {}
        self.trades_today: int = 0

    def calculate_kelly(self, signal: Signal) -> float:
        """Calculate Kelly criterion bet fraction.

        Kelly formula: f* = (bp - q) / b
        """
        if signal.action == "BUY_YES":
            p = signal.model_prob
            if signal.market_prob <= 0 or signal.market_prob >= 1:
                return 0.0
            b = (1.0 / signal.market_prob) - 1.0
        elif signal.action == "BUY_NO":
            p = 1.0 - signal.model_prob
            no_price = 1.0 - signal.market_prob
            if no_price <= 0 or no_price >= 1:
                return 0.0
            b = (1.0 / no_price) - 1.0
        else:
            return 0.0

        q = 1.0 - p
        kelly = (b * p - q) / b
        return max(0.0, kelly)

    def size_position(self, signal: Signal) -> PositionSize:
        """Calculate position size with all risk checks applied."""
        if self.daily_pnl <= -self.config.DAILY_LOSS_LIMIT:
            return PositionSize(0.0, 0.0, 0.0,
                                f"Daily loss limit hit (PnL: ${self.daily_pnl:.2f})", False)

        existing = self.open_positions.get(signal.market.condition_id, 0.0)
        if existing > 0:
            return PositionSize(0.0, 0.0, 0.0,
                                f"Already have ${existing:.2f} position in this market", False)

        kelly = self.calculate_kelly(signal)
        if kelly <= 0:
            return PositionSize(0.0, kelly, 0.0, "Kelly fraction is zero or negative", False)

        adjusted = kelly * self.config.KELLY_FRACTION
        amount = self.config.BANKROLL * adjusted

        max_pos = self.config.MAX_POSITION
        if amount > max_pos:
            amount = max_pos
            adjusted = max_pos / self.config.BANKROLL

        if amount < 1.0:
            return PositionSize(0.0, kelly, adjusted,
                                f"Position size too small (${amount:.2f})", False)

        return PositionSize(round(amount, 2), kelly, adjusted, "Approved", True)

    def record_trade(self, condition_id: str, amount: float) -> None:
        self.open_positions[condition_id] = self.open_positions.get(condition_id, 0.0) + amount
        self.trades_today += 1

    def record_pnl(self, pnl: float) -> None:
        self.daily_pnl += pnl

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.trades_today = 0

    def is_halted(self) -> bool:
        return self.daily_pnl <= -self.config.DAILY_LOSS_LIMIT
