"""
Backtest Engine

Runs historical backtests using Tier 1 modules.
Supports multiple strategies with configurable parameters.
"""

import sys
import os
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'stock-dashboard'))

from data.streaming_indicators import StreamingIndicators
from analytics.metrics import PerformanceAnalyzer
from risk.sizing import PositionSizer, SizingParams

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """A single trade during backtest."""
    entry_index: int
    entry_price: float
    entry_time: int
    exit_index: int
    exit_price: float
    exit_time: int
    quantity: int
    pnl: float
    pnl_pct: float
    reason: str


@dataclass
class BacktestResult:
    """Complete backtest results."""
    ticker: str
    strategy: str
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    total_return_pct: float

    # Metrics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float

    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    avg_drawdown_pct: float

    # Equity curve
    equity_curve: List[Dict]  # [{"time": ts, "value": equity}, ...]
    trades: List[Dict]  # Trade details

    # Additional
    bar_count: int
    bars: List[Dict]  # Price bars


class SimpleBacktestEngine:
    """Simple backtest engine using streaming indicators."""

    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.current_equity = initial_capital
        self.open_position = None  # (entry_price, entry_index)
        self.trades: List[BacktestTrade] = []
        self.equity_history = [(0, initial_capital)]

    def run_rsi_strategy(
        self,
        bars: List[Dict],
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        rsi_period: int = 14,
        position_size_pct: float = 0.1,
    ) -> BacktestResult:
        """
        Run RSI-based strategy:
        - Buy when RSI < oversold
        - Sell when RSI > overbought (or exit with 2% stop)
        """
        self.open_position = None
        self.trades = []
        self.equity_history = [(0, self.initial_capital)]
        current_equity = self.initial_capital

        indicators = StreamingIndicators()
        sizing = PositionSizer()

        for idx, bar in enumerate(bars):
            indicator_vals = indicators.add_bar(
                open=bar['open'],
                high=bar['high'],
                low=bar['low'],
                close=bar['close'],
                volume=bar['volume'],
                timestamp=bar['timestamp']
            )

            rsi = indicator_vals.rsi_14 if indicator_vals else 0

            # Exit signal
            if self.open_position:
                entry_idx, entry_price = self.open_position
                current_price = bar['close']
                pnl = (current_price - entry_price) * self.open_position_qty

                # Exit conditions
                exit_signal = False
                reason = ""

                if rsi > rsi_overbought:
                    exit_signal = True
                    reason = f"RSI overbought ({rsi:.1f})"

                if current_price < entry_price * 0.98:  # 2% stop loss
                    exit_signal = True
                    reason = "Stop loss hit"

                if exit_signal:
                    self.trades.append(
                        BacktestTrade(
                            entry_index=entry_idx,
                            entry_price=entry_price,
                            entry_time=bars[entry_idx]['timestamp'],
                            exit_index=idx,
                            exit_price=current_price,
                            exit_time=bar['timestamp'],
                            quantity=self.open_position_qty,
                            pnl=pnl,
                            pnl_pct=(pnl / (entry_price * self.open_position_qty)) * 100,
                            reason=reason
                        )
                    )
                    current_equity += pnl
                    self.open_position = None
                    self.open_position_qty = 0

            # Entry signal
            if not self.open_position and rsi < rsi_oversold:
                entry_price = bar['close']
                position_value = current_equity * position_size_pct
                qty = int(position_value / entry_price)

                if qty > 0:
                    self.open_position = (idx, entry_price)
                    self.open_position_qty = qty

            # Track equity
            if self.open_position:
                current_price = bar['close']
                unrealized_pnl = (current_price - self.open_position[1]) * self.open_position_qty
                self.equity_history.append((idx, current_equity + unrealized_pnl))
            else:
                self.equity_history.append((idx, current_equity))

        # Close final position if open
        if self.open_position:
            final_bar = bars[-1]
            entry_idx, entry_price = self.open_position
            exit_price = final_bar['close']
            pnl = (exit_price - entry_price) * self.open_position_qty

            self.trades.append(
                BacktestTrade(
                    entry_index=entry_idx,
                    entry_price=entry_price,
                    entry_time=bars[entry_idx]['timestamp'],
                    exit_index=len(bars) - 1,
                    exit_price=exit_price,
                    exit_time=final_bar['timestamp'],
                    quantity=self.open_position_qty,
                    pnl=pnl,
                    pnl_pct=(pnl / (entry_price * self.open_position_qty)) * 100,
                    reason="End of backtest"
                )
            )
            current_equity += pnl

        # Calculate metrics
        analyzer = PerformanceAnalyzer()
        trade_list = [
            {
                'pnl': t.pnl,
                'pnl_pct': t.pnl_pct,
                'hold_duration_minutes': (t.exit_time - t.entry_time) // 60
            }
            for t in self.trades
        ]
        metrics = analyzer.analyze_trades(trade_list) if trade_list else None

        # Build equity curve
        equity_curve = [
            {
                'time': bars[idx]['timestamp'],
                'value': equity
            }
            for idx, equity in self.equity_history
        ]

        # Build trade list
        trades_output = [
            {
                'entry_time': t.entry_time,
                'entry_price': t.entry_price,
                'exit_time': t.exit_time,
                'exit_price': t.exit_price,
                'quantity': t.quantity,
                'pnl': t.pnl,
                'pnl_pct': t.pnl_pct,
                'reason': t.reason
            }
            for t in self.trades
        ]

        return BacktestResult(
            ticker="AAPL",
            strategy="RSI",
            start_date=datetime.fromtimestamp(bars[0]['timestamp']).isoformat(),
            end_date=datetime.fromtimestamp(bars[-1]['timestamp']).isoformat(),
            initial_capital=self.initial_capital,
            final_equity=current_equity,
            total_return_pct=((current_equity - self.initial_capital) / self.initial_capital) * 100,
            total_trades=len(self.trades),
            winning_trades=sum(1 for t in self.trades if t.pnl > 0),
            losing_trades=sum(1 for t in self.trades if t.pnl < 0),
            win_rate=metrics.win_rate if metrics else 0,
            avg_win=metrics.avg_winner if metrics else 0,
            avg_loss=metrics.avg_loser if metrics else 0,
            profit_factor=metrics.profit_factor if metrics else 0,
            sharpe_ratio=metrics.sharpe_ratio if metrics else 0,
            sortino_ratio=metrics.sortino_ratio if metrics else 0,
            calmar_ratio=metrics.calmar_ratio if metrics else 0,
            max_drawdown_pct=metrics.max_drawdown_pct if metrics else 0,
            avg_drawdown_pct=metrics.avg_drawdown_pct if metrics else 0,
            equity_curve=equity_curve,
            trades=trades_output,
            bar_count=len(bars),
            bars=[
                {
                    'timestamp': b['timestamp'],
                    'open': b['open'],
                    'high': b['high'],
                    'low': b['low'],
                    'close': b['close'],
                    'volume': b['volume']
                }
                for b in bars
            ]
        )

    def run_ma_crossover_strategy(
        self,
        bars: List[Dict],
        fast_period: int = 12,
        slow_period: int = 26,
        position_size_pct: float = 0.1,
    ) -> BacktestResult:
        """Moving average crossover strategy."""
        # TODO: Implement MA crossover
        pass


def demo():
    """Demo: Run a simple backtest."""
    logging.basicConfig(level=logging.INFO)

    # Generate demo bars
    import random
    bars = []
    price = 150.0
    ts = int((datetime.now() - timedelta(days=30)).timestamp())

    for i in range(200):
        change = random.gauss(0, 1)
        price += change
        price = max(price, 100)

        bars.append({
            'timestamp': ts + (i * 300),  # 5-minute bars
            'open': price,
            'high': price + abs(random.gauss(0, 0.5)),
            'low': price - abs(random.gauss(0, 0.5)),
            'close': price + random.gauss(0, 0.3),
            'volume': random.randint(100000, 1000000)
        })

    engine = SimpleBacktestEngine(initial_capital=100000)
    result = engine.run_rsi_strategy(bars, rsi_oversold=35, rsi_overbought=65)

    print("\n=== Backtest Results ===")
    print(f"Total Return: {result.total_return_pct:.2f}%")
    print(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"Max Drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"Trades: {result.total_trades} ({result.winning_trades}W/{result.losing_trades}L)")
    print(f"Win Rate: {result.win_rate:.1f}%")


if __name__ == "__main__":
    demo()
