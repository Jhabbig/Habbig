"""
Options Scanner

Monitors options chains for unusual activity.
Detects: unusual volume, IV spikes, skew shifts
"""

import math
import sys
import os
from typing import List, Dict, Optional
from dataclasses import dataclass
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'stock-dashboard'))

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """Unusual options activity result."""
    ticker: str
    strike: float
    option_type: str  # call, put
    signal: str  # unusual_volume, iv_spike, skew_shift
    severity: str  # low, medium, high
    value: float  # volume, IV %, skew delta
    timestamp: int


class OptionsScanEngine:
    """Scan options chains for unusual activity."""

    def __init__(self):
        # Historical baselines (in production, compute from real data)
        self.volume_baselines = {}  # ticker -> avg_daily_volume
        self.iv_baselines = {}      # ticker -> avg_iv

    def scan_unusual_volume(
        self,
        ticker: str,
        calls: List[Dict],
        puts: List[Dict],
        timestamp: int,
        volume_multiplier: float = 2.0
    ) -> List[ScanResult]:
        """
        Detect unusual options volume.
        Flag if volume > multiplier * average.
        """
        results = []
        baseline = self.volume_baselines.get(ticker, 10000)

        # Scan calls
        for strike_data in calls:
            strike = strike_data.get('strike', 0)
            volume = strike_data.get('volume', 0)

            if volume > baseline * volume_multiplier:
                severity = 'high' if volume > baseline * 5 else 'medium'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=strike,
                    option_type='call',
                    signal='unusual_volume',
                    severity=severity,
                    value=volume,
                    timestamp=timestamp
                ))

        # Scan puts
        for strike_data in puts:
            strike = strike_data.get('strike', 0)
            volume = strike_data.get('volume', 0)

            if volume > baseline * volume_multiplier:
                severity = 'high' if volume > baseline * 5 else 'medium'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=strike,
                    option_type='put',
                    signal='unusual_volume',
                    severity=severity,
                    value=volume,
                    timestamp=timestamp
                ))

        return results

    def scan_iv_spikes(
        self,
        ticker: str,
        calls: List[Dict],
        puts: List[Dict],
        timestamp: int,
        iv_threshold: float = 80.0
    ) -> List[ScanResult]:
        """
        Detect IV spikes.
        Flag if IV percentile > threshold.
        """
        results = []

        # Scan calls
        for strike_data in calls:
            strike = strike_data.get('strike', 0)
            iv_pct = strike_data.get('iv_percentile', 0)

            if iv_pct > iv_threshold:
                severity = 'high' if iv_pct > 95 else 'medium'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=strike,
                    option_type='call',
                    signal='iv_spike',
                    severity=severity,
                    value=iv_pct,
                    timestamp=timestamp
                ))

        # Scan puts
        for strike_data in puts:
            strike = strike_data.get('strike', 0)
            iv_pct = strike_data.get('iv_percentile', 0)

            if iv_pct > iv_threshold:
                severity = 'high' if iv_pct > 95 else 'medium'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=strike,
                    option_type='put',
                    signal='iv_spike',
                    severity=severity,
                    value=iv_pct,
                    timestamp=timestamp
                ))

        return results

    def scan_skew_shifts(
        self,
        ticker: str,
        calls: List[Dict],
        puts: List[Dict],
        spot_price: float,
        timestamp: int,
        skew_threshold: float = 0.05
    ) -> List[ScanResult]:
        """
        Detect skew shifts (put/call imbalance).
        High put skew = tail risk concerns (bearish).
        High call skew = euphoria (bullish).
        """
        results = []

        # Calculate ATM skew
        atm_call_iv: Optional[float] = None
        atm_put_iv: Optional[float] = None

        for call in calls:
            if abs(call.get('strike', 0) - spot_price) < 1:
                atm_call_iv = call.get('iv', 0.2)
                break

        for put in puts:
            if abs(put.get('strike', 0) - spot_price) < 1:
                atm_put_iv = put.get('iv', 0.2)
                break

        if atm_call_iv is not None and atm_put_iv is not None:
            skew = atm_put_iv - atm_call_iv  # Positive = put skew (bearish)

            if abs(skew) > skew_threshold:
                severity = 'high' if abs(skew) > 0.10 else 'medium'
                signal_type = 'put_skew' if skew > 0 else 'call_skew'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=spot_price,
                    option_type='skew',
                    signal=signal_type,
                    severity=severity,
                    value=skew,
                    timestamp=timestamp
                ))

        return results

    def scan_earnings_move(
        self,
        ticker: str,
        calls: List[Dict],
        puts: List[Dict],
        spot_price: float,
        timestamp: int,
        days_to_expiration: float = 30.0,
        implied_move_threshold: float = 0.03,
    ) -> List[ScanResult]:
        """
        Detect implied earnings move.

        The implied 1-period move of an ATM straddle is approximately:
            implied_move ≈ IV * sqrt(DTE / 365)
        where IV is the average of ATM call/put implied volatility.

        High implied move = market expects a large earnings reaction.
        """
        results = []

        atm_call_iv: Optional[float] = None
        atm_put_iv: Optional[float] = None

        for call in calls:
            if abs(call.get('strike', 0) - spot_price) < 1:
                atm_call_iv = call.get('iv')
                if atm_call_iv is None:
                    atm_call_iv = 0.2
                break

        for put in puts:
            if abs(put.get('strike', 0) - spot_price) < 1:
                atm_put_iv = put.get('iv')
                if atm_put_iv is None:
                    atm_put_iv = 0.2
                break

        if atm_call_iv is not None and atm_put_iv is not None and days_to_expiration > 0:
            avg_iv = (atm_call_iv + atm_put_iv) / 2
            implied_move_pct = avg_iv * math.sqrt(days_to_expiration / 365.0)

            if implied_move_pct > implied_move_threshold:
                severity = 'high' if implied_move_pct > 0.08 else 'medium'
                results.append(ScanResult(
                    ticker=ticker,
                    strike=spot_price,
                    option_type='straddle',
                    signal='earnings_move',
                    severity=severity,
                    value=implied_move_pct,
                    timestamp=timestamp
                ))

        return results

    def scan_all(
        self,
        ticker: str,
        calls: List[Dict],
        puts: List[Dict],
        spot_price: float,
        timestamp: int
    ) -> List[ScanResult]:
        """Run all scans."""
        results = []
        results.extend(self.scan_unusual_volume(ticker, calls, puts, timestamp))
        results.extend(self.scan_iv_spikes(ticker, calls, puts, timestamp))
        results.extend(self.scan_skew_shifts(ticker, calls, puts, spot_price, timestamp))
        results.extend(self.scan_earnings_move(ticker, calls, puts, spot_price, timestamp))
        return results


def demo():
    """Demo options scanning."""
    logging.basicConfig(level=logging.INFO)

    # Demo option chain data
    calls = [
        {'strike': 145, 'volume': 50000, 'iv': 0.22, 'iv_percentile': 65},
        {'strike': 150, 'volume': 150000, 'iv': 0.20, 'iv_percentile': 58},
        {'strike': 155, 'volume': 80000, 'iv': 0.19, 'iv_percentile': 52},
    ]

    puts = [
        {'strike': 145, 'volume': 120000, 'iv': 0.25, 'iv_percentile': 85},
        {'strike': 150, 'volume': 60000, 'iv': 0.23, 'iv_percentile': 75},
        {'strike': 155, 'volume': 40000, 'iv': 0.22, 'iv_percentile': 68},
    ]

    scanner = OptionsScanEngine()
    results = scanner.scan_all('AAPL', calls, puts, spot_price=150.0, timestamp=1716241234)

    print("\n=== Options Scan Results ===")
    for result in results:
        print(f"{result.ticker} {result.strike} {result.option_type.upper()}: "
              f"{result.signal} ({result.severity}) - Value: {result.value:.4f}")


if __name__ == "__main__":
    demo()
