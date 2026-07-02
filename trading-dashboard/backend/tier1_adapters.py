"""
Adapter layer for Tier 1 modules.
Provides clean async/sync interfaces to realtime, indicators, and Greeks.
"""

import sys
import os
from typing import Dict, List, Optional
from dataclasses import asdict
import asyncio
import logging

# Add stock-dashboard to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'stock-dashboard'))

from data.realtime import RealtimeDataPipeline, BarInterval, Bar
from data.streaming_indicators import StreamingIndicators, IndicatorValues
from options.greeks import BlackScholesCalculator, GreeksResult

log = logging.getLogger(__name__)


class RealtimeFacade:
    """Unified facade for real-time market data, indicators, and Greeks."""

    def __init__(self, api_key: str = "demo"):
        """Initialize facade with Tier 1 modules."""
        self.pipeline = RealtimeDataPipeline(api_key=api_key)
        self.indicators: Dict[str, StreamingIndicators] = {}
        self.greeks_calc = BlackScholesCalculator(risk_free_rate=0.05)
        self.bar_callbacks: List[callable] = []

    async def connect(self) -> None:
        """Connect pipeline."""
        await self.pipeline.connect()
        log.info("RealtimeFacade connected")

    async def disconnect(self) -> None:
        """Disconnect pipeline."""
        await self.pipeline.disconnect()
        log.info("RealtimeFacade disconnected")

    def subscribe(self, ticker: str) -> None:
        """Subscribe to ticker updates."""
        self.pipeline.subscribe(ticker)
        if ticker not in self.indicators:
            self.indicators[ticker] = StreamingIndicators()
        log.info(f"Subscribed to {ticker}")

    def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe from ticker."""
        self.pipeline.unsubscribe(ticker)
        if ticker in self.indicators:
            del self.indicators[ticker]
        log.info(f"Unsubscribed from {ticker}")

    def add_bar_callback(self, callback: callable) -> None:
        """Register callback for bar completions."""
        self.bar_callbacks.append(callback)
        self.pipeline.add_bar_callback(self._wrap_callback(callback))

    def _wrap_callback(self, callback):
        """Wrap bar callback with indicator computation."""
        def wrapped(bar: Bar):
            # Update indicators if we have this ticker
            if bar.ticker in self.indicators:
                indicators = self.indicators[bar.ticker]
                indicator_values = indicators.add_bar(
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    timestamp=bar.timestamp
                )
            else:
                indicator_values = None

            # Fire callback with bar + indicators
            callback(bar, indicator_values)

        return wrapped

    def get_bars(self, ticker: str, interval: str = "1m", limit: int = 100) -> List[Dict]:
        """Get historical bars for a ticker."""
        bar_interval = BarInterval(interval)
        bars = self.pipeline.get_bars(ticker, bar_interval, limit)
        return [asdict(bar) for bar in bars]

    def get_indicators(self, ticker: str) -> Optional[Dict]:
        """Get latest indicator values for a ticker."""
        if ticker not in self.indicators:
            return None
        latest = self.indicators[ticker].get_latest()
        return asdict(latest) if latest else None

    def compute_greeks_chain(
        self,
        ticker: str,
        spot_price: float,
        expiration_days: float = 30,
        strikes: Optional[List[float]] = None
    ) -> List[Dict]:
        """
        Compute Greeks for an option chain.
        If strikes not provided, generate ATM ± 5 strikes.
        """
        if strikes is None:
            # Generate ATM strikes
            strike_step = spot_price * 0.05  # 5% apart
            atm_strike = int(spot_price)
            strikes = [
                atm_strike + i * strike_step
                for i in range(-5, 6)
            ]

        # Compute time to expiration
        time_to_expiry = expiration_days / 365.0

        # Assume 20% implied volatility
        iv = 0.20

        results = []
        for strike in strikes:
            call = self.greeks_calc.greeks_call(spot_price, strike, time_to_expiry, iv)
            put = self.greeks_calc.greeks_put(spot_price, strike, time_to_expiry, iv)

            results.append({
                "strike": strike,
                "call": asdict(call),
                "put": asdict(put),
            })

        return results

    def process_tick(self, ticker: str, price: float, size: int, timestamp: int) -> None:
        """Process a tick (for demo/testing)."""
        self.pipeline.process_tick(ticker, price, size, timestamp)

    async def demo_stream(self, tickers: List[str], duration_sec: float = float('inf')) -> None:
        """
        Start demo streaming for testing.
        Simulates random walk for given tickers.
        """
        for ticker in tickers:
            self.subscribe(ticker)

        try:
            task = asyncio.create_task(self.pipeline.demo_stream(tickers))
            await asyncio.sleep(duration_sec)
            task.cancel()
        except asyncio.CancelledError:
            pass


# Global instance
facade: Optional[RealtimeFacade] = None


async def get_facade() -> RealtimeFacade:
    """Get or create global facade instance."""
    global facade
    if facade is None:
        api_key = os.environ.get("MARKET_DATA_API_KEY", "demo")
        facade = RealtimeFacade(api_key=api_key)
        await facade.connect()
    return facade


def demo():
    """Demo: Test facade."""
    logging.basicConfig(level=logging.INFO)

    async def run():
        facade = await get_facade()
        facade.subscribe("AAPL")

        # Callback
        def on_bar(bar, indicators):
            print(f"\n[{bar.ticker}] {bar.interval.value} bar: C=${bar.close:.2f}")
            if indicators:
                print(f"  RSI(14): {indicators['rsi_14']:.2f}")
                print(f"  MACD: {indicators['macd_line']:.4f}")

        facade.add_bar_callback(on_bar)

        # Run demo for 10 seconds
        await facade.demo_stream(["AAPL"], duration_sec=10)
        await facade.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    demo()
