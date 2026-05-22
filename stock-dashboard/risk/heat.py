#!/usr/bin/env python3
"""
Portfolio Heat Tracker

Monitors:
- Sector exposure (max 30% per sector)
- Correlation buckets (max 10% per bucket)
- Daily/intraday PnL limits (circuit breakers)
- Greeks exposure
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("portfolio_heat")

# Sector to ETF mapping
SECTOR_MAP = {
    "AAPL": "XLK",
    "MSFT": "XLK",
    "NVDA": "XLK",
    "TSLA": "XLY",
    "AMZN": "XLY",
    "JPM": "XLF",
    "BAC": "XLF",
    "GE": "XLI",
    "PG": "XLP",
    "JNJ": "XLV",
    "CVX": "XLE",
    "XOM": "XLE",
    "DIS": "XLC",
    "META": "XLC",
    "NFLX": "XLC",
}

# Correlation buckets (define grouping for similar stocks)
CORRELATION_BUCKETS = {
    "mega_cap_tech": ["AAPL", "MSFT", "GOOGL", "NVDA", "META"],
    "growth": ["TSLA", "AMZN", "NFLX", "ROKU", "SQ"],
    "finance": ["JPM", "BAC", "GS", "MS", "WFC"],
    "healthcare": ["JNJ", "UNH", "PFE", "LLY"],
}


@dataclass
class Position:
    """A portfolio position."""
    ticker: str
    quantity: int
    avg_price: float
    current_price: float
    entry_date: int
    side: str = "BUY"  # BUY or SHORT

    @property
    def market_value(self) -> float:
        """Current position market value."""
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized PnL."""
        if self.side == "BUY":
            return (self.current_price - self.avg_price) * self.quantity
        else:
            return (self.avg_price - self.current_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        """Unrealized PnL as % of entry value."""
        entry_value = self.avg_price * self.quantity
        if entry_value == 0:
            return 0
        return (self.unrealized_pnl / entry_value) * 100


@dataclass
class HeatStatus:
    """Portfolio heat status snapshot."""
    timestamp: int
    total_exposure_pct: float  # % of account in positions
    sector_exposures: Dict[str, float] = field(default_factory=dict)
    correlation_exposures: Dict[str, float] = field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    intraday_loss_pct: float = 0.0
    circuit_breaker_status: str = "NORMAL"  # NORMAL, WARNING, STOP, MANUAL
    alert_messages: List[str] = field(default_factory=list)
    portfolio_delta: float = 0.0
    portfolio_gamma: float = 0.0
    portfolio_vega: float = 0.0
    portfolio_theta: float = 0.0


class PortfolioHeatTracker:
    """Monitors portfolio risk and heat levels."""

    def __init__(
        self,
        account_equity: float,
        max_daily_loss_pct: float = 5.0,
        max_intraday_loss_pct: float = 10.0,
        max_stop_loss_pct: float = 15.0,
    ):
        """
        Args:
            account_equity: Total account balance
            max_daily_loss_pct: Stop new trades if daily loss exceeds (%)
            max_intraday_loss_pct: Manual review if intraday loss exceeds (%)
            max_stop_loss_pct: Hard stop all trading if loss exceeds (%)
        """
        self.account_equity = account_equity
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_intraday_loss_pct = max_intraday_loss_pct
        self.max_stop_loss_pct = max_stop_loss_pct

        self.positions: Dict[str, Position] = {}
        self.opening_equity = account_equity
        self.daily_start_equity = account_equity

    def add_position(self, ticker: str, quantity: int, avg_price: float, current_price: float) -> None:
        """Add or update a position."""
        self.positions[ticker] = Position(
            ticker=ticker,
            quantity=quantity,
            avg_price=avg_price,
            current_price=current_price,
            entry_date=int(datetime.now(timezone.utc).timestamp()),
        )

    def remove_position(self, ticker: str) -> None:
        """Remove a closed position."""
        if ticker in self.positions:
            del self.positions[ticker]

    def update_position_price(self, ticker: str, current_price: float) -> None:
        """Update a position's current price."""
        if ticker in self.positions:
            self.positions[ticker].current_price = current_price

    def get_sector_exposure(self) -> Dict[str, float]:
        """
        Get % exposure to each sector.

        Returns:
            Dict mapping sector ETF (e.g., "XLK") to exposure %
        """
        exposure = {}
        total_value = sum(pos.market_value for pos in self.positions.values())

        for pos in self.positions.values():
            sector = SECTOR_MAP.get(pos.ticker, "OTHER")
            pct = (pos.market_value / self.account_equity * 100) if self.account_equity > 0 else 0
            exposure[sector] = exposure.get(sector, 0) + pct

        return exposure

    def get_correlation_bucket_exposure(self) -> Dict[str, float]:
        """
        Get % exposure to each correlation bucket.

        Returns:
            Dict mapping bucket name to exposure %
        """
        exposure = {}

        for bucket_name, tickers in CORRELATION_BUCKETS.items():
            total = 0
            for ticker in tickers:
                if ticker in self.positions:
                    pct = (self.positions[ticker].market_value / self.account_equity * 100) if self.account_equity > 0 else 0
                    total += pct
            if total > 0:
                exposure[bucket_name] = total

        return exposure

    def get_portfolio_greeks(self) -> Dict[str, float]:
        """
        Aggregate Greeks across all option positions.
        (Returns 0 if no options; integrate with options module later)
        """
        return {
            "delta": 0.0,
            "gamma": 0.0,
            "vega": 0.0,
            "theta": 0.0,
        }

    def get_heat_status(self) -> HeatStatus:
        """Get current portfolio heat status."""
        alerts = []
        circuit_breaker = "NORMAL"

        # Calculate current equity and PnL
        current_equity = self.account_equity + self._calculate_unrealized_pnl()
        daily_pnl = current_equity - self.daily_start_equity
        daily_pnl_pct = (daily_pnl / self.account_equity * 100) if self.account_equity > 0 else 0

        # Intraday loss from opening
        intraday_loss = self.opening_equity - current_equity
        intraday_loss_pct = (intraday_loss / self.opening_equity * 100) if self.opening_equity > 0 else 0

        # Total exposure
        total_exposure = sum(pos.market_value for pos in self.positions.values()) / self.account_equity * 100

        # Sector exposure
        sector_exposure = self.get_sector_exposure()

        # Check sector limits
        for sector, exposure_pct in sector_exposure.items():
            if exposure_pct > 30:
                alerts.append(f"⚠️ {sector} sector at {exposure_pct:.1f}% (max 30%)")

            if exposure_pct > 25:
                if circuit_breaker == "NORMAL":
                    circuit_breaker = "WARNING"

        # Correlation bucket exposure
        correlation_exposure = self.get_correlation_bucket_exposure()
        for bucket, exposure_pct in correlation_exposure.items():
            if exposure_pct > 10:
                alerts.append(f"⚠️ {bucket} bucket at {exposure_pct:.1f}% (max 10%)")

        # Circuit breakers: daily loss
        if daily_pnl_pct < -self.max_daily_loss_pct:
            circuit_breaker = "STOP"
            alerts.append(f"🛑 Daily loss {daily_pnl_pct:.2f}% exceeds limit {self.max_daily_loss_pct}%")

        # Circuit breakers: intraday loss
        if intraday_loss_pct > self.max_intraday_loss_pct:
            circuit_breaker = "MANUAL" if circuit_breaker != "STOP" else "STOP"
            alerts.append(f"⚠️ Intraday loss {intraday_loss_pct:.2f}% exceeds limit {self.max_intraday_loss_pct}%")

        # Circuit breakers: hard stop
        if intraday_loss_pct > self.max_stop_loss_pct:
            circuit_breaker = "STOP"
            alerts.append(f"🛑 HARD STOP: Loss {intraday_loss_pct:.2f}% exceeds {self.max_stop_loss_pct}%")

        # Check position concentration
        for ticker, pos in self.positions.items():
            pct = (pos.market_value / self.account_equity * 100) if self.account_equity > 0 else 0
            if pct > 5:
                alerts.append(f"⚠️ {ticker} at {pct:.1f}% (max 5% per position)")

        # Oversized drawdowns
        if pos.unrealized_pnl_pct < -10:
            alerts.append(f"⚠️ {ticker} at {pos.unrealized_pnl_pct:.1f}% loss")

        greeks = self.get_portfolio_greeks()

        return HeatStatus(
            timestamp=int(datetime.now(timezone.utc).timestamp()),
            total_exposure_pct=total_exposure,
            sector_exposures=sector_exposure,
            correlation_exposures=correlation_exposure,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            intraday_loss_pct=intraday_loss_pct,
            circuit_breaker_status=circuit_breaker,
            alert_messages=alerts,
            portfolio_delta=greeks["delta"],
            portfolio_gamma=greeks["gamma"],
            portfolio_vega=greeks["vega"],
            portfolio_theta=greeks["theta"],
        )

    def can_enter_trade(self) -> bool:
        """Check if circuit breaker allows new trades."""
        status = self.get_heat_status()
        return status.circuit_breaker_status in ["NORMAL", "WARNING"]

    def reset_daily_pnl(self) -> None:
        """Reset daily PnL tracking (call at market open)."""
        self.daily_start_equity = self.account_equity

    def _calculate_unrealized_pnl(self) -> float:
        """Sum of unrealized PnL across all positions."""
        return sum(pos.unrealized_pnl for pos in self.positions.values())

    def __repr__(self) -> str:
        """String representation."""
        status = self.get_heat_status()
        return (
            f"PortfolioHeat(exposure={status.total_exposure_pct:.1f}% "
            f"daily_pnl={status.daily_pnl_pct:.2f}% "
            f"status={status.circuit_breaker_status})"
        )


def demo():
    """Demo: Track portfolio heat."""
    logging.basicConfig(level=logging.INFO)

    # $100k account
    tracker = PortfolioHeatTracker(
        account_equity=100_000,
        max_daily_loss_pct=5.0,
        max_intraday_loss_pct=10.0,
    )

    # Add some positions
    tracker.add_position("AAPL", 100, 150.0, 151.0)  # Tech
    tracker.add_position("MSFT", 50, 400.0, 405.0)   # Tech
    tracker.add_position("TSLA", 50, 250.0, 245.0)   # Growth (losing)
    tracker.add_position("JPM", 30, 175.0, 176.0)    # Finance

    status = tracker.get_heat_status()

    print("\n=== Portfolio Heat Report ===")
    print(f"Total Exposure: {status.total_exposure_pct:.1f}%")
    print(f"Daily PnL: {status.daily_pnl_pct:.2f}%")
    print(f"Intraday Loss: {status.intraday_loss_pct:.2f}%")
    print(f"Circuit Breaker: {status.circuit_breaker_status}")

    print("\n=== Sector Exposure ===")
    for sector, exposure in status.sector_exposures.items():
        print(f"  {sector}: {exposure:.1f}%")

    if status.alert_messages:
        print("\n=== Alerts ===")
        for alert in status.alert_messages:
            print(f"  {alert}")

    print(f"\nCan enter trades: {tracker.can_enter_trade()}")


if __name__ == "__main__":
    demo()
