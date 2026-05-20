#!/usr/bin/env python3
"""
Real-Time Technical Indicators

Computes RSI, MACD, Bollinger Bands, ATR, OBV on streaming bar data.
Efficient rolling calculations without storing full history.
"""

import logging
from typing import Dict, Optional
from dataclasses import dataclass, field
from collections import deque

import numpy as np

log = logging.getLogger("streaming_indicators")


@dataclass
class IndicatorValues:
    """Snapshot of all indicators at a point in time."""
    timestamp: int
    rsi_14: float = 0.0
    rsi_7: float = 0.0
    rsi_21: float = 0.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    bb_upper_20: float = 0.0
    bb_middle_20: float = 0.0
    bb_lower_20: float = 0.0
    bb_position: float = 0.0  # -1 to 1, where 0 = middle band
    atr_14: float = 0.0
    atr_7: float = 0.0
    obv: float = 0.0
    roc_5: float = 0.0  # Rate of Change
    roc_10: float = 0.0
    sma_20: float = 0.0  # Simple Moving Average
    sma_50: float = 0.0
    sma_200: float = 0.0
    ema_12: float = 0.0  # Exponential Moving Average
    ema_26: float = 0.0


class StreamingIndicator:
    """Computes a single technical indicator on streaming data."""

    def __init__(self, period: int):
        self.period = period
        self.values = deque(maxlen=period)

    def add_value(self, value: float) -> None:
        """Add a value to the rolling window."""
        self.values.append(value)

    def is_ready(self) -> bool:
        """Check if we have enough data."""
        return len(self.values) >= self.period

    def get_mean(self) -> float:
        """Get simple moving average."""
        return sum(self.values) / len(self.values) if self.values else 0.0

    def get_std(self) -> float:
        """Get standard deviation."""
        if len(self.values) < 2:
            return 0.0
        mean = self.get_mean()
        variance = sum((x - mean) ** 2 for x in self.values) / len(self.values)
        return variance ** 0.5

    def get_ema(self, alpha: Optional[float] = None) -> float:
        """
        Get exponential moving average.
        If alpha not provided, use standard EMA formula.
        """
        if not self.values:
            return 0.0

        if alpha is None:
            alpha = 2 / (self.period + 1)

        ema = self.values[0]
        for i in range(1, len(self.values)):
            ema = alpha * self.values[i] + (1 - alpha) * ema

        return ema


class RSICalculator:
    """Compute RSI on streaming closes."""

    def __init__(self, period: int = 14):
        self.period = period
        self.gains = deque(maxlen=period)
        self.losses = deque(maxlen=period)
        self.closes = deque(maxlen=period + 1)

    def add_close(self, close: float) -> Optional[float]:
        """Add a close and return current RSI, or None if not ready."""
        self.closes.append(close)

        if len(self.closes) < 2:
            return None

        # Calculate gain/loss
        change = close - self.closes[-2]
        if change > 0:
            self.gains.append(change)
            self.losses.append(0)
        else:
            self.gains.append(0)
            self.losses.append(-change)

        if len(self.gains) < self.period:
            return None

        avg_gain = sum(self.gains) / len(self.gains)
        avg_loss = sum(self.losses) / len(self.losses)

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi


class MACDCalculator:
    """Compute MACD on streaming closes."""

    def __init__(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9):
        self.fast_ema = StreamingIndicator(fast_period)
        self.slow_ema = StreamingIndicator(slow_period)
        self.signal_line = deque(maxlen=signal_period)

        self.fast_alpha = 2 / (fast_period + 1)
        self.slow_alpha = 2 / (slow_period + 1)
        self.signal_alpha = 2 / (signal_period + 1)

        self.macd_values = deque(maxlen=signal_period)
        self.ema_fast_val = None
        self.ema_slow_val = None

    def add_close(self, close: float) -> Optional[Dict[str, float]]:
        """Add a close and return MACD values, or None if not ready."""
        # Compute EMAs
        if self.ema_fast_val is None:
            self.ema_fast_val = close
        else:
            self.ema_fast_val = self.fast_alpha * close + (1 - self.fast_alpha) * self.ema_fast_val

        if self.ema_slow_val is None:
            self.ema_slow_val = close
        else:
            self.ema_slow_val = self.slow_alpha * close + (1 - self.slow_alpha) * self.ema_slow_val

        # MACD line
        macd_line = self.ema_fast_val - self.ema_slow_val
        self.macd_values.append(macd_line)

        if len(self.macd_values) < 2:
            return None

        # Signal line (EMA of MACD)
        if len(self.signal_line) == 0:
            signal = macd_line
        else:
            signal = self.signal_alpha * macd_line + (1 - self.signal_alpha) * self.signal_line[-1]

        self.signal_line.append(signal)

        histogram = macd_line - signal

        return {
            "macd_line": macd_line,
            "signal_line": signal,
            "histogram": histogram,
        }


class BollingerBandsCalculator:
    """Compute Bollinger Bands on streaming closes."""

    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev
        self.closes = deque(maxlen=period)

    def add_close(self, close: float) -> Optional[Dict[str, float]]:
        """Add a close and return BB values, or None if not ready."""
        self.closes.append(close)

        if len(self.closes) < self.period:
            return None

        mean = sum(self.closes) / len(self.closes)
        variance = sum((x - mean) ** 2 for x in self.closes) / len(self.closes)
        std = variance ** 0.5

        upper = mean + (std * self.std_dev)
        lower = mean - (std * self.std_dev)

        # BB position: -1 (at lower), 0 (at middle), 1 (at upper)
        if upper == lower:
            bb_position = 0
        else:
            bb_position = 2 * ((close - lower) / (upper - lower)) - 1

        return {
            "upper": upper,
            "middle": mean,
            "lower": lower,
            "position": bb_position,
        }


class ATRCalculator:
    """Compute ATR on streaming OHLC."""

    def __init__(self, period: int = 14):
        self.period = period
        self.trs = deque(maxlen=period)
        self.prev_close = None

    def add_bar(self, high: float, low: float, close: float) -> Optional[float]:
        """Add OHLC bar and return current ATR, or None if not ready."""
        if self.prev_close is not None:
            tr = max(
                high - low,
                abs(high - self.prev_close),
                abs(low - self.prev_close),
            )
        else:
            tr = high - low

        self.trs.append(tr)
        self.prev_close = close

        if len(self.trs) < self.period:
            return None

        atr = sum(self.trs) / len(self.trs)
        return atr


class OBVCalculator:
    """Compute OBV (On-Balance Volume) on streaming data."""

    def __init__(self):
        self.obv = 0.0
        self.prev_close = None

    def add_bar(self, close: float, volume: int) -> float:
        """Add bar and return current OBV."""
        if self.prev_close is not None:
            if close > self.prev_close:
                self.obv += volume
            elif close < self.prev_close:
                self.obv -= volume
            # If close == prev_close, OBV unchanged

        self.prev_close = close
        return self.obv


class RateOfChangeCalculator:
    """Compute Rate of Change (ROC) on streaming closes."""

    def __init__(self, period: int = 5):
        self.period = period
        self.closes = deque(maxlen=period + 1)

    def add_close(self, close: float) -> Optional[float]:
        """Add close and return ROC, or None if not ready."""
        self.closes.append(close)

        if len(self.closes) < self.period + 1:
            return None

        roc = ((close - self.closes[0]) / self.closes[0]) * 100
        return roc


class SMACalculator:
    """Compute Simple Moving Average on streaming closes."""

    def __init__(self, period: int = 20):
        self.period = period
        self.closes = deque(maxlen=period)

    def add_close(self, close: float) -> Optional[float]:
        """Add close and return SMA, or None if not ready."""
        self.closes.append(close)

        if len(self.closes) < self.period:
            return None

        return sum(self.closes) / len(self.closes)


class EMACalculator:
    """Compute Exponential Moving Average on streaming closes."""

    def __init__(self, period: int = 12):
        self.period = period
        self.alpha = 2 / (period + 1)
        self.ema = None

    def add_close(self, close: float) -> Optional[float]:
        """Add close and return EMA, or None if not ready."""
        if self.ema is None:
            self.ema = close
            return None

        self.ema = self.alpha * close + (1 - self.alpha) * self.ema
        return self.ema


class StreamingIndicators:
    """
    All indicators for a single ticker.
    Efficiently computes all indicators on streaming bars.
    """

    def __init__(self):
        self.rsi_14 = RSICalculator(14)
        self.rsi_7 = RSICalculator(7)
        self.rsi_21 = RSICalculator(21)

        self.macd = MACDCalculator()
        self.bb = BollingerBandsCalculator()

        self.atr_14 = ATRCalculator(14)
        self.atr_7 = ATRCalculator(7)

        self.obv = OBVCalculator()

        self.roc_5 = RateOfChangeCalculator(5)
        self.roc_10 = RateOfChangeCalculator(10)

        self.sma_20 = SMACalculator(20)
        self.sma_50 = SMACalculator(50)
        self.sma_200 = SMACalculator(200)

        self.ema_12 = EMACalculator(12)
        self.ema_26 = EMACalculator(26)

        self.latest = None

    def add_bar(self, open: float, high: float, low: float, close: float, volume: int, timestamp: int) -> IndicatorValues:
        """
        Add a bar and compute all indicators.

        Returns: IndicatorValues snapshot
        """
        # RSI values
        rsi_14_val = self.rsi_14.add_close(close) or 0.0
        rsi_7_val = self.rsi_7.add_close(close) or 0.0
        rsi_21_val = self.rsi_21.add_close(close) or 0.0

        # MACD
        macd_dict = self.macd.add_close(close) or {}
        macd_line = macd_dict.get("macd_line", 0.0)
        macd_signal = macd_dict.get("signal_line", 0.0)
        macd_hist = macd_dict.get("histogram", 0.0)

        # Bollinger Bands
        bb_dict = self.bb.add_close(close) or {}
        bb_upper = bb_dict.get("upper", 0.0)
        bb_middle = bb_dict.get("middle", 0.0)
        bb_lower = bb_dict.get("lower", 0.0)
        bb_pos = bb_dict.get("position", 0.0)

        # ATR
        atr_14_val = self.atr_14.add_bar(high, low, close) or 0.0
        atr_7_val = self.atr_7.add_bar(high, low, close) or 0.0

        # OBV
        obv_val = self.obv.add_bar(close, volume)

        # ROC
        roc_5_val = self.roc_5.add_close(close) or 0.0
        roc_10_val = self.roc_10.add_close(close) or 0.0

        # SMA
        sma_20_val = self.sma_20.add_close(close) or 0.0
        sma_50_val = self.sma_50.add_close(close) or 0.0
        sma_200_val = self.sma_200.add_close(close) or 0.0

        # EMA
        ema_12_val = self.ema_12.add_close(close) or 0.0
        ema_26_val = self.ema_26.add_close(close) or 0.0

        self.latest = IndicatorValues(
            timestamp=timestamp,
            rsi_14=rsi_14_val,
            rsi_7=rsi_7_val,
            rsi_21=rsi_21_val,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_histogram=macd_hist,
            bb_upper_20=bb_upper,
            bb_middle_20=bb_middle,
            bb_lower_20=bb_lower,
            bb_position=bb_pos,
            atr_14=atr_14_val,
            atr_7=atr_7_val,
            obv=obv_val,
            roc_5=roc_5_val,
            roc_10=roc_10_val,
            sma_20=sma_20_val,
            sma_50=sma_50_val,
            sma_200=sma_200_val,
            ema_12=ema_12_val,
            ema_26=ema_26_val,
        )

        return self.latest

    def get_latest(self) -> Optional[IndicatorValues]:
        """Get latest indicator values."""
        return self.latest


def demo():
    """Demo: Compute indicators on sample bars."""
    logging.basicConfig(level=logging.INFO)

    indicators = StreamingIndicators()

    # Sample bars (simulated)
    bars = [
        (150.0, 151.0, 149.5, 150.5, 1000000),
        (150.5, 151.5, 150.0, 151.0, 1200000),
        (151.0, 152.0, 150.8, 151.5, 950000),
        (151.5, 152.5, 151.2, 152.0, 1100000),
        (152.0, 152.8, 151.5, 152.3, 1050000),
    ]

    for i, (o, h, l, c, v) in enumerate(bars):
        values = indicators.add_bar(o, h, l, c, v, timestamp=1000 + i)
        print(f"\nBar {i+1}: O=${o:.2f} H=${h:.2f} L=${l:.2f} C=${c:.2f} V={v}")
        print(f"  RSI(14): {values.rsi_14:.2f}")
        print(f"  MACD: line={values.macd_line:.4f} signal={values.macd_signal:.4f} hist={values.macd_histogram:.4f}")
        print(f"  BB: upper={values.bb_upper_20:.2f} middle={values.bb_middle_20:.2f} lower={values.bb_lower_20:.2f}")
        print(f"  ATR(14): {values.atr_14:.2f}")
        print(f"  OBV: {values.obv:.0f}")


if __name__ == "__main__":
    demo()
