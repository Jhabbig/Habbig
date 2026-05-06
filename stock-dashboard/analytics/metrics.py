#!/usr/bin/env python3
"""
Performance Metrics Calculator

Computes professional trading metrics:
- Sharpe Ratio (risk-adjusted return)
- Sortino Ratio (downside risk only)
- Calmar Ratio (return / max drawdown)
- Recovery Factor (net profit / max drawdown)
- Profit Factor (gross profit / gross loss)
- Win Rate, Payoff Ratio, etc.
"""

import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import numpy as np

log = logging.getLogger("analytics_metrics")


@dataclass
class TradeMetrics:
    """Metrics for a single trade."""
    pnl: float           # Profit/loss in dollars
    pnl_pct: float       # Profit/loss as % of risk
    hold_time: int       # Minutes held
    is_winner: bool      # True if profitable


@dataclass
class PerformanceMetrics:
    """Complete performance metrics summary."""
    # Trade statistics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_winner: float
    avg_loser: float
    largest_winner: float
    largest_loser: float
    payoff_ratio: float

    # Money statistics
    gross_profit: float
    gross_loss: float
    net_profit: float
    profit_factor: float

    # Return statistics
    total_return_pct: float
    annual_return_pct: float
    cumulative_return_pct: float

    # Risk statistics
    volatility_pct: float
    downside_volatility_pct: float
    max_drawdown_pct: float
    max_drawdown_dollar: float
    max_drawdown_date_range: Tuple[int, int]  # (start_ts, end_ts)
    recovery_days: int
    avg_drawdown_pct: float

    # Risk-adjusted returns
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    recovery_factor: float
    monthly_sharpe: float

    # Other metrics
    consecutive_winners: int
    consecutive_losers: int
    avg_hold_time_minutes: float


class PerformanceAnalyzer:
    """Analyzes trading performance and computes metrics."""

    def __init__(self, risk_free_rate: float = 0.05):
        """
        Args:
            risk_free_rate: Annual risk-free rate (default 5%)
        """
        self.risk_free_rate = risk_free_rate

    def analyze_trades(self, trades: List[Dict]) -> PerformanceMetrics:
        """
        Analyze a list of closed trades and compute metrics.

        Args:
            trades: List of trade dicts with keys:
                - pnl (realized_pnl in dollars)
                - pnl_pct (realized_pnl_pct)
                - hold_duration_minutes
                - entry_date, exit_date (timestamps)

        Returns: PerformanceMetrics
        """
        if not trades:
            return self._empty_metrics()

        # Separate winners and losers
        winners = [t for t in trades if t["pnl"] > 0]
        losers = [t for t in trades if t["pnl"] < 0]
        total_trades = len(trades)

        # Trade statistics
        winning_trades = len(winners)
        losing_trades = len(losers)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0

        avg_winner = sum(t["pnl"] for t in winners) / winning_trades if winners else 0
        avg_loser = abs(sum(t["pnl"] for t in losers) / losing_trades) if losers else 0
        largest_winner = max((t["pnl"] for t in winners), default=0)
        largest_loser = abs(min((t["pnl"] for t in losers), default=0))
        payoff_ratio = avg_winner / avg_loser if avg_loser > 0 else 0

        # Money statistics
        gross_profit = sum(t["pnl"] for t in winners)
        gross_loss = abs(sum(t["pnl"] for t in losers))
        net_profit = gross_profit - gross_loss
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Equity curve and returns
        equity_curve = self._build_equity_curve(trades)
        daily_returns = self._compute_daily_returns(equity_curve)
        returns_pct = [r * 100 for r in daily_returns]

        total_return_pct = (equity_curve[-1] - 1) * 100 if equity_curve else 0
        cumulative_return_pct = total_return_pct

        # Estimate annual return (simple)
        trading_days = len(equity_curve)
        annual_return_pct = ((1 + total_return_pct / 100) ** (252 / max(trading_days, 1)) - 1) * 100

        # Risk statistics
        volatility_pct = np.std(returns_pct) * np.sqrt(252) if returns_pct else 0
        downside_returns = [r for r in returns_pct if r < 0]
        downside_volatility_pct = np.std(downside_returns) * np.sqrt(252) if downside_returns else 0

        # Drawdown analysis
        drawdowns = self._compute_drawdowns(equity_curve)
        max_dd_pct = min(drawdowns) if drawdowns else 0
        max_dd_dollar = max_dd_pct * (equity_curve[-1] if equity_curve else 1)
        avg_dd_pct = np.mean(drawdowns) if drawdowns else 0
        recovery_days = self._compute_recovery_days(equity_curve)
        max_dd_range = self._find_max_drawdown_range(equity_curve)

        # Risk-adjusted returns
        sharpe = self._compute_sharpe(returns_pct, self.risk_free_rate)
        sortino = self._compute_sortino(returns_pct, self.risk_free_rate)
        calmar = self._compute_calmar(annual_return_pct, max_dd_pct)
        recovery_factor = net_profit / abs(max_dd_dollar) if max_dd_dollar != 0 else 0
        monthly_sharpe = sharpe  # Placeholder; would need monthly data

        # Streak statistics
        consecutive_wins, consecutive_losses = self._compute_streaks(trades)

        # Hold time
        hold_times = [t["hold_duration_minutes"] for t in trades if "hold_duration_minutes" in t]
        avg_hold_time = sum(hold_times) / len(hold_times) if hold_times else 0

        return PerformanceMetrics(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            avg_winner=avg_winner,
            avg_loser=avg_loser,
            largest_winner=largest_winner,
            largest_loser=largest_loser,
            payoff_ratio=payoff_ratio,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            net_profit=net_profit,
            profit_factor=profit_factor,
            total_return_pct=total_return_pct,
            annual_return_pct=annual_return_pct,
            cumulative_return_pct=cumulative_return_pct,
            volatility_pct=volatility_pct,
            downside_volatility_pct=downside_volatility_pct,
            max_drawdown_pct=max_dd_pct,
            max_drawdown_dollar=max_dd_dollar,
            max_drawdown_date_range=max_dd_range,
            recovery_days=recovery_days,
            avg_drawdown_pct=avg_dd_pct,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            recovery_factor=recovery_factor,
            monthly_sharpe=monthly_sharpe,
            consecutive_winners=consecutive_wins,
            consecutive_losers=consecutive_losses,
            avg_hold_time_minutes=avg_hold_time,
        )

    def _build_equity_curve(self, trades: List[Dict]) -> List[float]:
        """
        Build a normalized equity curve starting at 1.0.
        Assumes trades are sorted by exit_date.
        """
        equity = [1.0]
        for trade in trades:
            if trade["pnl_pct"] is not None:
                # PnL % is return on risk, so scale appropriately
                return_decimal = trade["pnl_pct"] / 100
                new_equity = equity[-1] * (1 + return_decimal)
                equity.append(new_equity)
        return equity

    def _compute_daily_returns(self, equity_curve: List[float]) -> List[float]:
        """Compute daily returns from equity curve."""
        if len(equity_curve) < 2:
            return []
        returns = []
        for i in range(1, len(equity_curve)):
            ret = (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            returns.append(ret)
        return returns

    def _compute_drawdowns(self, equity_curve: List[float]) -> List[float]:
        """Compute drawdown at each point."""
        if not equity_curve:
            return []
        drawdowns = []
        running_max = equity_curve[0]
        for value in equity_curve:
            running_max = max(running_max, value)
            dd = (value - running_max) / running_max
            drawdowns.append(dd)
        return drawdowns

    def _find_max_drawdown_range(self, equity_curve: List[float]) -> Tuple[int, int]:
        """Find start and end indices of maximum drawdown."""
        if not equity_curve or len(equity_curve) < 2:
            return (0, 0)

        max_dd = 0
        dd_start = 0
        dd_end = 0
        running_max_idx = 0

        for i, value in enumerate(equity_curve):
            if value > equity_curve[running_max_idx]:
                running_max_idx = i
            dd = (value - equity_curve[running_max_idx]) / equity_curve[running_max_idx]
            if dd < max_dd:
                max_dd = dd
                dd_start = running_max_idx
                dd_end = i

        return (dd_start, dd_end)

    def _compute_recovery_days(self, equity_curve: List[float]) -> int:
        """Estimate days to recover from max drawdown."""
        if len(equity_curve) < 2:
            return 0

        # Find the trough (max dd point)
        running_max = equity_curve[0]
        trough_idx = 0
        for i, value in enumerate(equity_curve):
            if value > running_max:
                running_max = value
            elif (running_max - value) / running_max > (running_max - equity_curve[trough_idx]) / running_max:
                trough_idx = i

        # Count days from trough to recovery
        recovery_idx = trough_idx
        for i in range(trough_idx + 1, len(equity_curve)):
            if equity_curve[i] >= running_max:
                recovery_idx = i
                break
            if equity_curve[i] > running_max:
                running_max = equity_curve[i]

        return recovery_idx - trough_idx

    def _compute_sharpe(self, daily_returns_pct: List[float], rf_rate: float) -> float:
        """Sharpe ratio = (return - rf) / volatility."""
        if not daily_returns_pct or len(daily_returns_pct) < 2:
            return 0

        returns = np.array(daily_returns_pct)
        avg_return = np.mean(returns)
        volatility = np.std(returns)

        # Annualize
        annual_return = avg_return * 252
        annual_vol = volatility * np.sqrt(252)

        if annual_vol == 0:
            return 0
        sharpe = (annual_return - rf_rate * 100) / annual_vol
        return sharpe

    def _compute_sortino(self, daily_returns_pct: List[float], rf_rate: float) -> float:
        """Sortino ratio = (return - rf) / downside_volatility."""
        if not daily_returns_pct or len(daily_returns_pct) < 2:
            return 0

        returns = np.array(daily_returns_pct)
        avg_return = np.mean(returns)
        downside_returns = returns[returns < 0]

        if len(downside_returns) == 0:
            # No losing days; Sortino = infinity (or very high)
            return 100.0

        downside_vol = np.std(downside_returns)

        # Annualize
        annual_return = avg_return * 252
        annual_downside_vol = downside_vol * np.sqrt(252)

        if annual_downside_vol == 0:
            return 0
        sortino = (annual_return - rf_rate * 100) / annual_downside_vol
        return sortino

    def _compute_calmar(self, annual_return_pct: float, max_dd_pct: float) -> float:
        """Calmar ratio = annual return / max drawdown."""
        if max_dd_pct >= 0 or max_dd_pct == 0:
            return 0
        calmar = annual_return_pct / abs(max_dd_pct)
        return calmar

    def _compute_streaks(self, trades: List[Dict]) -> Tuple[int, int]:
        """Find longest consecutive winners and losers."""
        if not trades:
            return (0, 0)

        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0

        for trade in trades:
            if trade["pnl"] > 0:
                current_wins += 1
                max_wins = max(max_wins, current_wins)
                current_losses = 0
            else:
                current_losses += 1
                max_losses = max(max_losses, current_losses)
                current_wins = 0

        return (max_wins, max_losses)

    def _empty_metrics(self) -> PerformanceMetrics:
        """Return empty/default metrics."""
        return PerformanceMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0,
            avg_winner=0,
            avg_loser=0,
            largest_winner=0,
            largest_loser=0,
            payoff_ratio=0,
            gross_profit=0,
            gross_loss=0,
            net_profit=0,
            profit_factor=0,
            total_return_pct=0,
            annual_return_pct=0,
            cumulative_return_pct=0,
            volatility_pct=0,
            downside_volatility_pct=0,
            max_drawdown_pct=0,
            max_drawdown_dollar=0,
            max_drawdown_date_range=(0, 0),
            recovery_days=0,
            avg_drawdown_pct=0,
            sharpe_ratio=0,
            sortino_ratio=0,
            calmar_ratio=0,
            recovery_factor=0,
            monthly_sharpe=0,
            consecutive_winners=0,
            consecutive_losers=0,
            avg_hold_time_minutes=0,
        )


def example_usage():
    """Demonstrate metrics calculation."""
    logging.basicConfig(level=logging.INFO)

    analyzer = PerformanceAnalyzer(risk_free_rate=0.05)

    # Example: 10 trades
    trades = [
        {"pnl": 500, "pnl_pct": 2.5, "hold_duration_minutes": 45},
        {"pnl": -200, "pnl_pct": -1.0, "hold_duration_minutes": 30},
        {"pnl": 800, "pnl_pct": 4.0, "hold_duration_minutes": 120},
        {"pnl": -150, "pnl_pct": -0.75, "hold_duration_minutes": 15},
        {"pnl": 600, "pnl_pct": 3.0, "hold_duration_minutes": 90},
        {"pnl": 300, "pnl_pct": 1.5, "hold_duration_minutes": 60},
        {"pnl": -100, "pnl_pct": -0.5, "hold_duration_minutes": 20},
        {"pnl": 400, "pnl_pct": 2.0, "hold_duration_minutes": 75},
        {"pnl": 700, "pnl_pct": 3.5, "hold_duration_minutes": 110},
        {"pnl": -250, "pnl_pct": -1.25, "hold_duration_minutes": 40},
    ]

    metrics = analyzer.analyze_trades(trades)

    print(f"\n=== Performance Metrics ===")
    print(f"Total Trades: {metrics.total_trades}")
    print(f"Win Rate: {metrics.win_rate*100:.1f}%")
    print(f"Avg Winner: ${metrics.avg_winner:.0f}")
    print(f"Avg Loser: ${metrics.avg_loser:.0f}")
    print(f"Payoff Ratio: {metrics.payoff_ratio:.2f}")
    print(f"Profit Factor: {metrics.profit_factor:.2f}")
    print(f"Net Profit: ${metrics.net_profit:.0f}")
    print(f"Return: {metrics.total_return_pct:.2f}%")
    print(f"Max Drawdown: {metrics.max_drawdown_pct*100:.2f}%")
    print(f"Sharpe Ratio: {metrics.sharpe_ratio:.2f}")
    print(f"Sortino Ratio: {metrics.sortino_ratio:.2f}")
    print(f"Calmar Ratio: {metrics.calmar_ratio:.2f}")
    print(f"Recovery Factor: {metrics.recovery_factor:.2f}")
    print(f"Longest Win Streak: {metrics.consecutive_winners}")
    print(f"Longest Lose Streak: {metrics.consecutive_losers}")


if __name__ == "__main__":
    example_usage()
