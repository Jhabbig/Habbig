"""
AI Signal Engine

Generates buy/sell signals using ensemble of indicators.
Returns confidence scores and explainability.
"""

import sys
import os
from typing import Dict, List, Optional
from dataclasses import dataclass
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'stock-dashboard'))

from data.streaming_indicators import IndicatorValues

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """Trading signal with confidence and reasoning."""
    timestamp: int
    ticker: str
    signal: str  # BUY, SELL, HOLD
    confidence: float  # 0-100
    price: float
    reasoning: List[str]  # Why this signal


class EnsembleSignalEngine:
    """Generate signals using ensemble of indicators."""

    @staticmethod
    def generate_signal(
        indicators: IndicatorValues,
        price: float,
        timestamp: int,
        ticker: str = "AAPL"
    ) -> Signal:
        """
        Generate signal from indicator values.

        Ensemble approach:
        - RSI: Oversold (<30) = bullish, Overbought (>70) = bearish
        - MACD: Histogram + > 0 = bullish, < 0 = bearish
        - Bollinger Bands: Price near lower band = bullish, near upper = bearish
        - Moving averages: Price above SMA200 = bullish context
        """
        if not indicators:
            return Signal(
                timestamp=timestamp,
                ticker=ticker,
                signal="HOLD",
                confidence=0,
                price=price,
                reasoning=["No indicators available"]
            )

        signals = []
        reasoning = []
        weights = []

        # ====== RSI Signal ======
        rsi = indicators.rsi_14
        if rsi < 30:
            signals.append(1.0)  # Strong buy
            reasoning.append(f"RSI({rsi:.1f}) oversold - reversal likely")
            weights.append(0.25)
        elif rsi < 40:
            signals.append(0.5)  # Mild buy
            reasoning.append(f"RSI({rsi:.1f}) approaching oversold")
            weights.append(0.15)
        elif rsi > 70:
            signals.append(-1.0)  # Strong sell
            reasoning.append(f"RSI({rsi:.1f}) overbought - pullback likely")
            weights.append(0.25)
        elif rsi > 60:
            signals.append(-0.5)  # Mild sell
            reasoning.append(f"RSI({rsi:.1f}) approaching overbought")
            weights.append(0.15)
        else:
            signals.append(0.0)  # Neutral
            weights.append(0.10)

        # ====== MACD Signal ======
        macd_hist = indicators.macd_histogram
        if macd_hist > 0 and indicators.macd_line > indicators.macd_signal:
            signals.append(0.8)  # Bullish crossover
            reasoning.append(f"MACD positive histogram ({macd_hist:.4f}) - bullish")
            weights.append(0.25)
        elif macd_hist < 0 and indicators.macd_line < indicators.macd_signal:
            signals.append(-0.8)  # Bearish crossover
            reasoning.append(f"MACD negative histogram ({macd_hist:.4f}) - bearish")
            weights.append(0.25)
        else:
            signals.append(0.0)  # Neutral
            weights.append(0.10)

        # ====== Bollinger Bands Signal ======
        bb_pos = indicators.bb_position  # -1 (lower), 0 (middle), 1 (upper)
        if bb_pos < -0.5:
            signals.append(0.6)  # Price at lower band - oversold
            reasoning.append(f"Price near lower Bollinger Band ({bb_pos:.2f}) - oversold")
            weights.append(0.20)
        elif bb_pos > 0.5:
            signals.append(-0.6)  # Price at upper band - overbought
            reasoning.append(f"Price near upper Bollinger Band ({bb_pos:.2f}) - overbought")
            weights.append(0.20)
        else:
            signals.append(0.0)  # Neutral
            weights.append(0.10)

        # ====== Moving Average Trend ======
        # Bullish if price above SMA200, bearish if below
        trend_bias = 0.0
        if price > indicators.sma_200 > 0:
            trend_bias = 0.3
            reasoning.append(f"Price above SMA(200) - long-term uptrend")
        elif price < indicators.sma_200 > 0:
            trend_bias = -0.3
            reasoning.append(f"Price below SMA(200) - long-term downtrend")

        signals.append(trend_bias)
        weights.append(0.15)

        # ====== Calculate Ensemble Signal ======
        # Weighted average of all signals
        if not signals or not weights:
            return Signal(
                timestamp=timestamp,
                ticker=ticker,
                signal="HOLD",
                confidence=0,
                price=price,
                reasoning=["Insufficient data"]
            )

        total_weight = sum(weights)
        weighted_signal = sum(s * w for s, w in zip(signals, weights)) / total_weight

        # Convert to signal with confidence
        if weighted_signal > 0.3:
            final_signal = "BUY"
            confidence = min(100, abs(weighted_signal) * 100)
        elif weighted_signal < -0.3:
            final_signal = "SELL"
            confidence = min(100, abs(weighted_signal) * 100)
        else:
            final_signal = "HOLD"
            confidence = 50

        return Signal(
            timestamp=timestamp,
            ticker=ticker,
            signal=final_signal,
            confidence=confidence,
            price=price,
            reasoning=reasoning
        )

    @staticmethod
    def generate_batch_signals(
        indicator_history: List[IndicatorValues],
        price_history: List[float],
        timestamps: List[int],
        ticker: str = "AAPL"
    ) -> List[Signal]:
        """Generate signals for a batch of historical data."""
        signals = []
        for ind, price, ts in zip(indicator_history, price_history, timestamps):
            signal = EnsembleSignalEngine.generate_signal(ind, price, ts, ticker)
            signals.append(signal)
        return signals


def demo():
    """Demo signal generation."""
    logging.basicConfig(level=logging.INFO)

    # Create demo indicators
    from stock_dashboard.data.streaming_indicators import IndicatorValues

    ind = IndicatorValues(
        timestamp=1716241234,
        rsi_14=25.0,  # Oversold
        rsi_7=20.0,
        rsi_21=30.0,
        macd_line=0.5,
        macd_signal=0.3,
        macd_histogram=0.2,  # Positive
        bb_upper_20=155.0,
        bb_middle_20=150.0,
        bb_lower_20=145.0,
        bb_position=-0.6,  # Near lower
        atr_14=1.2,
        atr_7=1.0,
        obv=1000000000,
        roc_5=1.5,
        roc_10=2.1,
        sma_20=150.5,
        sma_50=149.8,
        sma_200=148.0,
        ema_12=150.2,
        ema_26=149.5
    )

    signal = EnsembleSignalEngine.generate_signal(ind, price=147.5, timestamp=1716241234)

    print("\n=== Generated Signal ===")
    print(f"Signal: {signal.signal} ({signal.confidence:.1f}% confidence)")
    print(f"Price: ${signal.price:.2f}")
    print(f"Reasoning:")
    for reason in signal.reasoning:
        print(f"  - {reason}")


if __name__ == "__main__":
    demo()
