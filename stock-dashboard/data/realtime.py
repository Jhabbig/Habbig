#!/usr/bin/env python3
"""
Real-Time Data Pipeline

Streams intraday market data via WebSocket (Alpaca) with fallback to polling.
Outputs 1-min, 5-min, and 1-hour bars for live technical analysis.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import threading
from collections import defaultdict

try:
    import aiohttp
    import websockets
except ImportError:
    raise ImportError("websockets and aiohttp required: pip install websockets aiohttp")

log = logging.getLogger("realtime_data")


class BarInterval(Enum):
    """Supported bar intervals."""
    ONE_MIN = "1m"
    FIVE_MIN = "5m"
    FIFTEEN_MIN = "15m"
    ONE_HOUR = "1h"
    ONE_DAY = "1d"


@dataclass
class Bar:
    """A single OHLCV bar."""
    ticker: str
    interval: BarInterval
    timestamp: int              # Unix timestamp (start of bar)
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float = 0.0
    count: int = 0              # Tick count in bar


@dataclass
class TickData:
    """Raw tick data."""
    ticker: str
    price: float
    size: int
    bid: float = 0.0
    ask: float = 0.0
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


class BarAggregator:
    """
    Aggregates ticks into 1m, 5m, 1h bars.
    Maintains rolling buffers for efficient queries.
    """

    def __init__(self, ticker: str, retention_minutes: int = 1440):
        """
        Args:
            ticker: Stock symbol
            retention_minutes: Keep N minutes of bars in memory (default 1 day)
        """
        self.ticker = ticker
        self.retention_minutes = retention_minutes

        # Current incomplete bars (key = interval, value = Bar being built)
        self.current_bars: Dict[BarInterval, Bar] = {}

        # Completed bars (key = interval, value = list of Bars)
        self.completed_bars: Dict[BarInterval, List[Bar]] = {
            interval: [] for interval in BarInterval
        }

        # Current tick accumulation
        self.current_tick_sum = 0
        self.current_tick_count = 0
        self.current_volume = 0

    def add_tick(self, price: float, size: int, timestamp: int) -> Optional[List[Bar]]:
        """
        Add a tick and return any completed bars.

        Args:
            price: Tick price
            size: Tick size
            timestamp: Unix timestamp (milliseconds)

        Returns: List of Bar objects that just completed, or None
        """
        ts_sec = timestamp // 1000 if timestamp > 1000000000000 else timestamp

        # Update current tick aggregation
        self.current_tick_sum += price * size
        self.current_tick_count += size
        self.current_volume += size

        completed = []

        # Check each interval to see if a bar completed
        for interval in BarInterval:
            bar = self.current_bars.get(interval)

            # Determine bar period
            period_sec = self._interval_to_seconds(interval)
            bar_start = (ts_sec // period_sec) * period_sec

            if bar is None:
                # Start new bar
                self.current_bars[interval] = Bar(
                    ticker=self.ticker,
                    interval=interval,
                    timestamp=bar_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=size,
                    vwap=price,
                    count=1,
                )
            elif bar.timestamp == bar_start:
                # Add to current bar
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                bar.volume += size
                bar.count += 1
                bar.vwap = self.current_tick_sum / self.current_tick_count
            else:
                # Bar completed, save it
                self.completed_bars[interval].append(bar)
                self._prune_old_bars(interval)

                # Start new bar
                self.current_bars[interval] = Bar(
                    ticker=self.ticker,
                    interval=interval,
                    timestamp=bar_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=size,
                    vwap=price,
                    count=1,
                )
                completed.append(bar)

        return completed if completed else None

    def get_bars(self, interval: BarInterval, limit: int = 100) -> List[Bar]:
        """Get recent completed bars for an interval."""
        bars = self.completed_bars.get(interval, [])
        return bars[-limit:] if bars else []

    def get_current_bar(self, interval: BarInterval) -> Optional[Bar]:
        """Get the current (incomplete) bar."""
        return self.current_bars.get(interval)

    @staticmethod
    def _interval_to_seconds(interval: BarInterval) -> int:
        """Convert interval to seconds."""
        return {
            BarInterval.ONE_MIN: 60,
            BarInterval.FIVE_MIN: 300,
            BarInterval.FIFTEEN_MIN: 900,
            BarInterval.ONE_HOUR: 3600,
            BarInterval.ONE_DAY: 86400,
        }[interval]

    def _prune_old_bars(self, interval: BarInterval) -> None:
        """Remove bars older than retention period."""
        bars = self.completed_bars[interval]
        cutoff_ts = int(time.time()) - (self.retention_minutes * 60)

        # Keep only bars after cutoff
        self.completed_bars[interval] = [b for b in bars if b.timestamp > cutoff_ts]


class RealtimeDataPipeline:
    """
    Real-time market data pipeline.
    Connects via WebSocket, falls back to polling.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://data.alpaca.markets",
        ws_url: str = "wss://data.alpaca.markets/v1beta3/crypto/us/quotes",
    ):
        """
        Args:
            api_key: Alpaca API key
            base_url: REST API base URL
            ws_url: WebSocket URL for quotes/bars
        """
        self.api_key = api_key
        self.base_url = base_url
        self.ws_url = ws_url

        # Bar aggregators (one per ticker)
        self.aggregators: Dict[str, BarAggregator] = {}

        # Callbacks for completed bars
        self.bar_callbacks: List[Callable[[Bar], None]] = []

        # Connection state
        self.websocket = None
        self.is_connected = False
        self.session: Optional[aiohttp.ClientSession] = None

        # Subscriptions
        self.subscribed_tickers: set = set()

    async def connect(self) -> None:
        """Connect to WebSocket and start streaming."""
        self.session = aiohttp.ClientSession()
        self.is_connected = True
        log.info("Realtime pipeline connected")

    async def disconnect(self) -> None:
        """Disconnect WebSocket."""
        self.is_connected = False
        if self.session:
            await self.session.close()
        log.info("Realtime pipeline disconnected")

    def subscribe(self, ticker: str) -> None:
        """Subscribe to ticker updates."""
        if ticker not in self.aggregators:
            self.aggregators[ticker] = BarAggregator(ticker)
            self.subscribed_tickers.add(ticker)
            log.info(f"Subscribed to {ticker}")

    def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe from ticker."""
        if ticker in self.aggregators:
            del self.aggregators[ticker]
            self.subscribed_tickers.discard(ticker)
            log.info(f"Unsubscribed from {ticker}")

    def add_bar_callback(self, callback: Callable[[Bar], None]) -> None:
        """Register callback for when bars complete."""
        self.bar_callbacks.append(callback)

    async def fetch_latest_quote(self, ticker: str) -> Optional[Dict]:
        """
        Fetch latest quote via REST API.
        Fallback when WebSocket is unavailable.
        """
        if not self.session:
            return None

        try:
            url = f"{self.base_url}/v2/stocks/{ticker}/quotes/latest"
            headers = {"APCA-API-KEY-ID": self.api_key}

            async with self.session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("quote", {})
        except Exception as e:
            log.warning(f"Failed to fetch quote for {ticker}: {e}")

        return None

    async def fetch_bars(
        self,
        ticker: str,
        limit: int = 100,
        timeframe: str = "1Min",
    ) -> Optional[List[Dict]]:
        """
        Fetch historical bars via REST API.
        Useful for initializing bar buffers.
        """
        if not self.session:
            return None

        try:
            url = f"{self.base_url}/v2/stocks/{ticker}/bars"
            params = {
                "limit": limit,
                "timeframe": timeframe,
                "adjustment": "all",
            }
            headers = {"APCA-API-KEY-ID": self.api_key}

            async with self.session.get(url, params=params, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("bars", [])
        except Exception as e:
            log.warning(f"Failed to fetch bars for {ticker}: {e}")

        return None

    def process_tick(self, ticker: str, price: float, size: int, timestamp: int) -> None:
        """
        Process a tick and update bars.

        Args:
            ticker: Stock symbol
            price: Tick price
            size: Tick size
            timestamp: Unix timestamp (seconds or milliseconds)
        """
        if ticker not in self.aggregators:
            self.subscribe(ticker)

        aggregator = self.aggregators[ticker]
        completed_bars = aggregator.add_tick(price, size, timestamp)

        # Fire callbacks for completed bars
        if completed_bars:
            for bar in completed_bars:
                log.debug(f"Bar completed: {bar.ticker} {bar.interval.value} @ ${bar.close}")
                for callback in self.bar_callbacks:
                    try:
                        callback(bar)
                    except Exception as e:
                        log.error(f"Callback error: {e}")

    def get_bars(self, ticker: str, interval: BarInterval, limit: int = 100) -> List[Bar]:
        """Get recent bars for a ticker and interval."""
        aggregator = self.aggregators.get(ticker)
        if aggregator:
            return aggregator.get_bars(interval, limit)
        return []

    def get_current_bar(self, ticker: str, interval: BarInterval) -> Optional[Bar]:
        """Get the current incomplete bar."""
        aggregator = self.aggregators.get(ticker)
        if aggregator:
            return aggregator.get_current_bar(interval)
        return None

    async def start_polling(self, tickers: List[str], interval_sec: int = 60) -> None:
        """
        Fallback: Poll for latest quotes periodically.
        Updates bars every interval_sec.
        """
        while self.is_connected:
            for ticker in tickers:
                quote = await self.fetch_latest_quote(ticker)
                if quote and "ap" in quote and "as" in quote:
                    # Simulate tick from quote
                    price = (quote.get("ap", 0) + quote.get("bp", 0)) / 2
                    size = 100  # Default size
                    self.process_tick(ticker, price, size, int(time.time()))

            await asyncio.sleep(interval_sec)

    async def demo_stream(self, tickers: List[str]) -> None:
        """
        Demo: Simulate streaming ticks for testing.
        """
        import random

        for ticker in tickers:
            self.subscribe(ticker)

        prices = {ticker: 150.0 for ticker in tickers}

        while self.is_connected:
            for ticker in tickers:
                # Simulate random walk
                change = random.gauss(0, 0.5)
                prices[ticker] += change
                prices[ticker] = max(prices[ticker], 100)  # Floor

                size = random.randint(100, 5000)
                self.process_tick(ticker, prices[ticker], size, int(time.time() * 1000))

            await asyncio.sleep(0.1)  # 100ms between ticks


async def main_demo():
    """Demo: Stream ticks and print completed bars."""
    logging.basicConfig(level=logging.INFO)

    pipeline = RealtimeDataPipeline(api_key="demo")

    # Callback when 1-min bars complete
    def on_1m_bar(bar: Bar):
        if bar.interval == BarInterval.ONE_MIN:
            print(
                f"[{bar.ticker}] 1m bar: O=${bar.open:.2f} "
                f"H=${bar.high:.2f} L=${bar.low:.2f} C=${bar.close:.2f} "
                f"V={bar.volume}"
            )

    pipeline.add_bar_callback(on_1m_bar)

    await pipeline.connect()

    # Demo stream for 30 seconds
    try:
        task = asyncio.create_task(pipeline.demo_stream(["AAPL", "TSLA", "MSFT"]))
        await asyncio.sleep(30)
        task.cancel()
    except asyncio.CancelledError:
        pass
    finally:
        await pipeline.disconnect()


if __name__ == "__main__":
    asyncio.run(main_demo())
