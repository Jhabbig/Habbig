#!/usr/bin/env python3
"""
Cycle indicators for the long-term lens.

Each indicator is a function that takes a ticker and returns:
  {value, signal, threshold, description, source}

`signal` is in {bullish, neutral, bearish} when relevant — used by the
backtester and by the dashboard for colour-coding. `threshold` exposes the
calibration so users can see why we flagged something.

Indicators implemented:
  - Pi Cycle Top (price-only, BTC-canonical)
  - Mayer Multiple (price/200DMA)        — wraps long_term.mayer_multiple
  - 200WMA distance                       — wraps long_term
  - NUPL (Net Unrealised Profit/Loss) ⛓
  - SOPR proxy (Realized Profit/Loss)  ⛓
  - Puell Multiple (issuance USD)      ⛓
  - Stock-to-Flow (BTC-only)
  - Hash Ribbons (PoW only)            ⛓
  - RHODL proxy                        ⛓
  - Exchange Net-Flow                   ⛓
  - BTC Dominance proxy
  - ETH/BTC ratio
  - Realized Cap HOD                    ⛓

⛓ = needs CoinMetrics on-chain data. BTC + ETH have richest coverage; the
   indicator gracefully returns None for assets without the required series.

Every indicator returns the same shape so the dashboard and backtester can
iterate over them uniformly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

import numpy as np

import database as db
import long_term as lt

log = logging.getLogger("crypto.indicators")


# ─── Shared types ───────────────────────────────────────────────────────────

@dataclass
class IndicatorResult:
    name: str
    ticker: str
    value: Optional[float]
    signal: str  # bullish | neutral | bearish | unavailable
    description: str
    threshold: dict  # {low, high} or {trigger}
    source: str
    extras: dict  # extra fields the UI may render

    def to_dict(self) -> dict:
        d = asdict(self)
        # NaN/Inf → None so JSON serialises.
        if d["value"] is not None and isinstance(d["value"], float):
            if math.isnan(d["value"]) or math.isinf(d["value"]):
                d["value"] = None
        return d


def _na(name: str, ticker: str, reason: str = "no data") -> IndicatorResult:
    return IndicatorResult(
        name=name, ticker=ticker, value=None, signal="unavailable",
        description=reason, threshold={}, source="—", extras={},
    )


def _ma(arr: np.ndarray, window: int) -> Optional[float]:
    if len(arr) < window:
        return None
    return float(np.mean(arr[-window:]))


def _rolling_ma(arr: np.ndarray, window: int) -> np.ndarray:
    """Trailing simple MA series. Length = len(arr); first window-1 entries are NaN."""
    if len(arr) < window:
        return np.full(len(arr), np.nan)
    out = np.full(len(arr), np.nan)
    csum = np.cumsum(np.insert(arr, 0, 0.0))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def _percentile_rank(series: np.ndarray, value: float) -> Optional[float]:
    """Where does `value` sit in the trailing distribution? 0..1."""
    finite = series[np.isfinite(series)]
    if len(finite) < 30:
        return None
    return float(np.searchsorted(np.sort(finite), value) / len(finite))


# ─── Price-only indicators ──────────────────────────────────────────────────

def pi_cycle_top(ticker: str = "BTC") -> IndicatorResult:
    """The Pi Cycle Top indicator. 111DMA × 2 crossing above 350DMA has called
    every major BTC top within 3 days (2013, 2017, 2021). For ETH/altcoins it
    is less canonical but the math still flags overbought conditions."""
    _, closes = lt.get_daily_closes(ticker, days=365 * 5)
    if len(closes) < 350:
        return _na("pi_cycle_top", ticker, "needs 350+ daily bars")

    ma111x2 = _rolling_ma(closes, 111) * 2.0
    ma350 = _rolling_ma(closes, 350)
    cur_ratio = float(ma111x2[-1] / ma350[-1]) if ma350[-1] else float("nan")

    # Detect a fresh crossover in the last 7 days.
    diff = ma111x2 - ma350
    crossed = False
    if len(diff) >= 8 and np.isfinite(diff[-8]) and np.isfinite(diff[-1]):
        # Crossing UP across the last week
        recent = diff[-8:]
        crossed = bool((recent[0] < 0) and (recent[-1] > 0))

    if crossed:
        signal = "bearish"
        desc = "Pi Cycle Top crossover fired in the last week — historic top zone"
    elif cur_ratio > 0.95:
        signal = "bearish"
        desc = f"Approaching Pi Cycle Top ({cur_ratio:.2f})"
    elif cur_ratio < 0.5:
        signal = "bullish"
        desc = f"Far below Pi Cycle Top ({cur_ratio:.2f}) — accumulation zone"
    else:
        signal = "neutral"
        desc = f"Pi Cycle ratio {cur_ratio:.2f}"

    return IndicatorResult(
        name="pi_cycle_top", ticker=ticker, value=round(cur_ratio, 3), signal=signal,
        description=desc,
        threshold={"trigger": 1.0, "approaching": 0.95, "depressed": 0.5},
        source="price (111DMAx2 vs 350DMA)",
        extras={"crossed_last_week": crossed, "ma111x2": round(float(ma111x2[-1]), 2),
                "ma350": round(float(ma350[-1]), 2)},
    )


def two_hundred_week_distance(ticker: str) -> IndicatorResult:
    """How far above/below the 200-week MA the price is. <1.0 historically
    marks the bear-market floor on BTC."""
    _, closes = lt.get_daily_closes(ticker, days=365 * 5)
    twma = lt.two_hundred_week_ma(closes)
    if twma is None or twma <= 0:
        return _na("two_hundred_week_distance", ticker, "needs 1400+ daily bars")
    ratio = float(closes[-1] / twma)
    if ratio < 1.0:
        signal, desc = "bullish", f"Below 200WMA ({ratio:.2f}) — historic accumulation zone"
    elif ratio > 3.0:
        signal, desc = "bearish", f"Far above 200WMA ({ratio:.2f}) — euphoria"
    else:
        signal, desc = "neutral", f"200WMA ratio {ratio:.2f}"
    return IndicatorResult(
        name="two_hundred_week_distance", ticker=ticker, value=round(ratio, 3),
        signal=signal, description=desc,
        threshold={"floor": 1.0, "euphoria": 3.0},
        source="price (200-week SMA)",
        extras={"twma": round(twma, 2)},
    )


# ─── BTC-only price indicators ──────────────────────────────────────────────

# BTC supply schedule. After block 840,000 (April 2024) the reward is 3.125 BTC.
# Pre-defined halving points so we can compute issuance back through history
# even when CoinMetrics issuance series isn't loaded.
_BTC_HALVINGS = [
    (datetime(2012, 11, 28, tzinfo=timezone.utc), 25.0),
    (datetime(2016, 7, 9, tzinfo=timezone.utc), 12.5),
    (datetime(2020, 5, 11, tzinfo=timezone.utc), 6.25),
    (datetime(2024, 4, 19, tzinfo=timezone.utc), 3.125),
    (datetime(2028, 4, 1, tzinfo=timezone.utc), 1.5625),  # estimate
]


def _btc_block_reward_at(date: datetime) -> float:
    """Approximate block subsidy at a given UTC date."""
    reward = 50.0
    for halving_date, new_reward in _BTC_HALVINGS:
        if date >= halving_date:
            reward = new_reward
    return reward


def stock_to_flow(ticker: str = "BTC") -> IndicatorResult:
    """BTC-only. S2F = stock / annualised flow. PlanB's deviation is the
    interesting signal: log price deviation from the S2F regression line."""
    if ticker != "BTC":
        return _na("stock_to_flow", ticker, "BTC-only indicator")
    # Use CoinMetrics current supply if we have it, else estimate from halving schedule.
    _, supply_series = lt.get_onchain_series("BTC", "SplyCur", days=2)
    if len(supply_series) > 0:
        stock = float(supply_series[-1])
    else:
        # Estimate: blocks_since_genesis × current_reward (rough).
        days_since_genesis = (datetime.now(timezone.utc) - datetime(2009, 1, 3, tzinfo=timezone.utc)).days
        stock = min(21_000_000, days_since_genesis * 144 * 6.25)

    # Annualised flow = current block reward × 144 blocks/day × 365.
    now = datetime.now(timezone.utc)
    block_reward = _btc_block_reward_at(now)
    flow = block_reward * 144 * 365
    s2f = stock / flow if flow > 0 else float("nan")

    # PlanB's regression: ln(price) = 3.3 × ln(s2f) - 1.84 (approx, recalibrated 2024).
    # The original regression has broken down post-2022, but the *deviation* is
    # still informative as a "is the model implying we're cheap or expensive."
    model_price = math.exp(3.3 * math.log(s2f) - 1.84) if s2f > 0 else None
    _, closes = lt.get_daily_closes("BTC", days=2)
    actual = float(closes[-1]) if len(closes) else None
    deviation = (actual / model_price - 1.0) if model_price and actual else None

    if deviation is None:
        signal, desc = "unavailable", "no price data"
    elif deviation < -0.5:
        signal, desc = "bullish", f"{deviation:+.0%} below S2F model — historically deep value"
    elif deviation > 1.5:
        signal, desc = "bearish", f"{deviation:+.0%} above S2F model — historically overbought"
    else:
        signal, desc = "neutral", f"{deviation:+.0%} vs S2F model"

    return IndicatorResult(
        name="stock_to_flow", ticker=ticker, value=round(s2f, 1),
        signal=signal, description=desc,
        threshold={"deep_value_dev": -0.5, "overbought_dev": 1.5},
        source="supply schedule + price",
        extras={"deviation_from_model": round(deviation, 3) if deviation is not None else None,
                "model_price": round(model_price, 2) if model_price else None,
                "block_reward": block_reward},
    )


# ─── On-chain indicators (CoinMetrics-backed) ───────────────────────────────

def nupl(ticker: str) -> IndicatorResult:
    """Net Unrealised Profit/Loss = (MarketCap - RealizedCap) / MarketCap.
    > 0.75 = euphoria, < 0 = capitulation (BTC-calibrated)."""
    if ticker not in lt.ONCHAIN_COVERED:
        return _na("nupl", ticker, "on-chain data not available for this asset")
    _, mc = lt.get_onchain_series(ticker, "CapMrktCurUSD", days=2)
    _, rc = lt.get_onchain_series(ticker, "CapRealUSD", days=2)
    if len(mc) == 0 or len(rc) == 0 or mc[-1] <= 0:
        return _na("nupl", ticker, "missing market or realized cap")
    val = float((mc[-1] - rc[-1]) / mc[-1])
    if val < 0:
        signal, desc = "bullish", f"NUPL {val:+.2f} (capitulation — coins underwater on average)"
    elif val < 0.25:
        signal, desc = "bullish", f"NUPL {val:+.2f} (hope/fear zone)"
    elif val < 0.5:
        signal, desc = "neutral", f"NUPL {val:+.2f} (optimism)"
    elif val < 0.75:
        signal, desc = "bearish", f"NUPL {val:+.2f} (belief — late expansion)"
    else:
        signal, desc = "bearish", f"NUPL {val:+.2f} (euphoria — top zone)"
    return IndicatorResult(
        name="nupl", ticker=ticker, value=round(val, 3), signal=signal,
        description=desc,
        threshold={"capitulation": 0.0, "fear": 0.25, "optimism": 0.5, "euphoria": 0.75},
        source="on-chain (CoinMetrics CapMrktCurUSD/CapRealUSD)",
        extras={},
    )


def sopr_proxy(ticker: str) -> IndicatorResult:
    """SOPR proxy: change in Realized Cap normalised by transfer volume.
    Real SOPR is UTXO-level — requires Pro tier on CoinMetrics — but the
    direction of this proxy matches well historically. Smoothed over 7 days."""
    if ticker not in lt.ONCHAIN_COVERED:
        return _na("sopr_proxy", ticker, "on-chain data not available")
    _, rc = lt.get_onchain_series(ticker, "CapRealUSD", days=30)
    _, tv = lt.get_onchain_series(ticker, "TxTfrValAdjUSD", days=30)
    if len(rc) < 8 or len(tv) < 8:
        return _na("sopr_proxy", ticker, "insufficient on-chain history")
    drc = np.diff(rc[-8:])
    avg_tv = float(np.mean(tv[-7:]))
    if avg_tv <= 0:
        return _na("sopr_proxy", ticker, "transfer volume zero")
    proxy = 1.0 + float(np.mean(drc)) / avg_tv
    if proxy < 0.97:
        signal, desc = "bullish", f"SOPR proxy {proxy:.3f} — coins moving at a loss (capitulation)"
    elif proxy < 1.0:
        signal, desc = "bullish", f"SOPR proxy {proxy:.3f} — break-even/loss zone"
    elif proxy < 1.04:
        signal, desc = "neutral", f"SOPR proxy {proxy:.3f}"
    else:
        signal, desc = "bearish", f"SOPR proxy {proxy:.3f} — heavy profit-taking"
    return IndicatorResult(
        name="sopr_proxy", ticker=ticker, value=round(proxy, 4), signal=signal,
        description=desc,
        threshold={"capitulation": 0.97, "fair": 1.0, "profit_taking": 1.04},
        source="on-chain proxy (ΔRealizedCap / TransferVolume)",
        extras={},
    )


def puell_multiple(ticker: str = "BTC") -> IndicatorResult:
    """Daily mined supply (USD) / 365d MA of daily mined supply (USD).
    < 0.5 historically marks every BTC bottom; > 4 every top."""
    if ticker not in ("BTC", "ETH"):
        return _na("puell_multiple", ticker, "only meaningful for PoW/historical-PoW chains")
    _, closes = lt.get_daily_closes(ticker, days=400)
    if len(closes) < 365:
        return _na("puell_multiple", ticker, "needs 365+ daily price bars")
    # Try CoinMetrics IssTotNtv (issuance in native units) — community-tier.
    _, iss = lt.get_onchain_series(ticker, "IssTotNtv", days=400)
    if len(iss) < 365:
        # Fallback for BTC: synthesise issuance from the halving schedule.
        if ticker == "BTC":
            iss = np.array([_btc_block_reward_at(
                datetime.now(timezone.utc) - timedelta(days=int(d))
            ) * 144 for d in range(len(closes) - 1, -1, -1)])
        else:
            return _na("puell_multiple", ticker, "no issuance series available")
    # Align lengths (closes is the canonical length).
    n = min(len(closes), len(iss))
    closes_a, iss_a = closes[-n:], iss[-n:]
    issuance_usd = iss_a * closes_a
    if n < 366:
        return _na("puell_multiple", ticker, "needs 365+ aligned days of issuance")
    ma365 = float(np.mean(issuance_usd[-366:-1]))
    if ma365 <= 0:
        return _na("puell_multiple", ticker, "issuance MA is zero")
    val = float(issuance_usd[-1] / ma365)
    if val < 0.5:
        signal, desc = "bullish", f"Puell {val:.2f} — miner capitulation, historic bottom zone"
    elif val < 1.0:
        signal, desc = "bullish", f"Puell {val:.2f} — under-issuance, value zone"
    elif val < 2.5:
        signal, desc = "neutral", f"Puell {val:.2f}"
    elif val < 4.0:
        signal, desc = "bearish", f"Puell {val:.2f} — issuance hot, late-cycle"
    else:
        signal, desc = "bearish", f"Puell {val:.2f} — top zone"
    return IndicatorResult(
        name="puell_multiple", ticker=ticker, value=round(val, 3), signal=signal,
        description=desc, threshold={"bottom": 0.5, "top": 4.0},
        source="on-chain (issuance × price / 365d MA)",
        extras={"issuance_usd_today": round(float(issuance_usd[-1]), 0)},
    )


def hash_ribbons(ticker: str = "BTC") -> IndicatorResult:
    """30d MA(hash rate) vs 60d MA(hash rate). A 30 < 60 cross marks miner
    capitulation; the *recovery* cross (30 back above 60) has historically
    been a high-conviction long signal."""
    if ticker != "BTC":
        return _na("hash_ribbons", ticker, "PoW-only (BTC)")
    _, hr = lt.get_onchain_series("BTC", "HashRate", days=120)
    if len(hr) < 60:
        return _na("hash_ribbons", ticker, "insufficient hash-rate history")
    ma30 = _rolling_ma(hr, 30)
    ma60 = _rolling_ma(hr, 60)
    # Detect cross in last 10 days
    diff = ma30 - ma60
    crossed_up = False
    crossed_down = False
    if len(diff) >= 11 and np.isfinite(diff[-11]) and np.isfinite(diff[-1]):
        recent = diff[-11:]
        crossed_up = bool((recent[0] < 0) and (recent[-1] > 0))
        crossed_down = bool((recent[0] > 0) and (recent[-1] < 0))
    ratio = float(ma30[-1] / ma60[-1]) if ma60[-1] else float("nan")
    if crossed_up:
        signal, desc = "bullish", "Hash Ribbons recovery — miner capitulation ending"
    elif crossed_down:
        signal, desc = "bearish", "Hash Ribbons capitulation — miners switching off"
    elif ratio < 0.95:
        signal, desc = "neutral", f"Below ribbon by {(1-ratio)*100:.1f}% (capitulating)"
    else:
        signal, desc = "neutral", f"Hash MAs aligned (ratio {ratio:.3f})"
    return IndicatorResult(
        name="hash_ribbons", ticker=ticker, value=round(ratio, 4), signal=signal,
        description=desc, threshold={"recovery_cross": "30>60", "capitulation": "30<60"},
        source="on-chain (CoinMetrics HashRate)",
        extras={"crossed_up": crossed_up, "crossed_down": crossed_down,
                "ma30": float(ma30[-1]), "ma60": float(ma60[-1])},
    )


def exchange_net_flow(ticker: str) -> IndicatorResult:
    """7-day exchange net flow (in - out) as % of 30d transfer volume.
    Net outflows = bullish (coins moving to cold storage); net inflows = bearish."""
    if ticker not in lt.ONCHAIN_COVERED:
        return _na("exchange_net_flow", ticker, "on-chain data not available")
    _, fin = lt.get_onchain_series(ticker, "FlowInExNtv", days=30)
    _, fout = lt.get_onchain_series(ticker, "FlowOutExNtv", days=30)
    if len(fin) < 7 or len(fout) < 7:
        # FlowInExNtv is not on the free tier for all assets — degrade gracefully.
        return _na("exchange_net_flow", ticker, "exchange flow series not available")
    net_ntv = float(np.sum(fin[-7:]) - np.sum(fout[-7:]))
    _, supply = lt.get_onchain_series(ticker, "SplyCur", days=2)
    if len(supply) == 0 or supply[-1] <= 0:
        return _na("exchange_net_flow", ticker, "no supply data")
    pct_of_supply = net_ntv / float(supply[-1])
    # Threshold expressed in basis points of supply.
    bps = pct_of_supply * 10_000
    if bps < -5:
        signal, desc = "bullish", f"7d net outflow {bps:.1f}bp of supply — accumulation"
    elif bps < 0:
        signal, desc = "bullish", f"Net outflow ({bps:.1f}bp)"
    elif bps < 5:
        signal, desc = "neutral", f"Net flow {bps:+.1f}bp"
    else:
        signal, desc = "bearish", f"Net inflow {bps:+.1f}bp — distribution"
    return IndicatorResult(
        name="exchange_net_flow", ticker=ticker, value=round(bps, 2), signal=signal,
        description=desc,
        threshold={"strong_outflow_bp": -5, "strong_inflow_bp": 5},
        source="on-chain (CoinMetrics FlowIn/FlowOut)",
        extras={"net_native": round(net_ntv, 4)},
    )


def rhodl_proxy(ticker: str) -> IndicatorResult:
    """RHODL proxy. The canonical RHODL uses UTXO age bands (Pro tier). We
    proxy with realized-cap velocity: how fast the realized cap is growing
    relative to its 1-year average. Sharp acceleration = late-cycle distribution."""
    if ticker not in lt.ONCHAIN_COVERED:
        return _na("rhodl_proxy", ticker, "on-chain data not available")
    _, rc = lt.get_onchain_series(ticker, "CapRealUSD", days=365 * 2)
    if len(rc) < 365:
        return _na("rhodl_proxy", ticker, "needs 365+ days of realized cap")
    # 7d growth of realized cap vs 365d growth.
    g7 = (rc[-1] / rc[-8] - 1.0) if rc[-8] > 0 else 0.0
    g365 = (rc[-1] / rc[-365] - 1.0) if rc[-365] > 0 else 0.0
    if g365 <= 0:
        return _na("rhodl_proxy", ticker, "yearly growth flat")
    ratio = g7 / (g365 / 52.0)  # 7d growth vs typical-week growth this year
    if ratio > 3.0:
        signal, desc = "bearish", f"Realized cap accelerating {ratio:.1f}× normal — late cycle"
    elif ratio < 0.3:
        signal, desc = "bullish", f"Realized cap dormant ({ratio:.2f}× normal) — accumulation"
    else:
        signal, desc = "neutral", f"Realized cap velocity {ratio:.2f}× normal"
    return IndicatorResult(
        name="rhodl_proxy", ticker=ticker, value=round(ratio, 3), signal=signal,
        description=desc, threshold={"dormant": 0.3, "hot": 3.0},
        source="on-chain proxy (7d vs 1y realized-cap growth)",
        extras={},
    )


# ─── Cross-asset indicators ─────────────────────────────────────────────────

def btc_dominance_proxy() -> IndicatorResult:
    """BTC market-cap share of the assets we track. Not true BTC.D (which is
    against the whole crypto market) but a useful proxy for our universe.
    Falling dominance = alts outperforming (late-cycle rotation)."""
    caps = {}
    for ticker in lt.TICKER_MAP.keys():
        if ticker in lt.ONCHAIN_COVERED:
            _, mc = lt.get_onchain_series(ticker, "CapMrktCurUSD", days=2)
            if len(mc) > 0:
                caps[ticker] = float(mc[-1])
                continue
        # Fallback: price × estimated supply from price stream. Crude but works.
        _, closes = lt.get_daily_closes(ticker, days=2)
        if len(closes) == 0:
            continue
        # Rough circulating-supply estimates (Dec 2024-ish). These drift slowly.
        supply_est = {"SOL": 4.7e8, "DOGE": 1.46e11, "XRP": 5.7e10}.get(ticker)
        if supply_est:
            caps[ticker] = float(closes[-1]) * supply_est
    total = sum(caps.values())
    if total <= 0 or "BTC" not in caps:
        return _na("btc_dominance_proxy", "BTC", "no market-cap data")
    dom = caps["BTC"] / total
    if dom > 0.7:
        signal, desc = "bullish", f"BTC dominance {dom:.0%} — alts depressed"
    elif dom < 0.5:
        signal, desc = "bearish", f"BTC dominance {dom:.0%} — alt rotation underway (late cycle)"
    else:
        signal, desc = "neutral", f"BTC dominance {dom:.0%}"
    return IndicatorResult(
        name="btc_dominance_proxy", ticker="BTC", value=round(dom, 4), signal=signal,
        description=desc, threshold={"alt_rotation": 0.5, "btc_strong": 0.7},
        source="market-cap proxy across tracked universe",
        extras={"market_caps": {k: round(v / 1e9, 2) for k, v in caps.items()}},
    )


def eth_btc_ratio() -> IndicatorResult:
    """ETH/BTC price ratio. The single best proxy for "are alts in season".
    Rising = alt-friendly (risk-on); falling = BTC-only regime."""
    _, btc = lt.get_daily_closes("BTC", days=200)
    _, eth = lt.get_daily_closes("ETH", days=200)
    if len(btc) < 30 or len(eth) < 30:
        return _na("eth_btc_ratio", "ETH", "insufficient history")
    ratio = float(eth[-1] / btc[-1])
    ma50 = _ma(np.asarray([e / b for e, b in zip(eth, btc)]), 50)
    if ma50 is None:
        return _na("eth_btc_ratio", "ETH", "insufficient ma history")
    if ratio > ma50 * 1.05:
        signal, desc = "bullish", f"ETH/BTC {ratio:.4f} — above 50d MA, alt-friendly regime"
    elif ratio < ma50 * 0.95:
        signal, desc = "bearish", f"ETH/BTC {ratio:.4f} — below 50d MA, BTC-only regime"
    else:
        signal, desc = "neutral", f"ETH/BTC {ratio:.4f} (50d MA {ma50:.4f})"
    return IndicatorResult(
        name="eth_btc_ratio", ticker="ETH", value=round(ratio, 5), signal=signal,
        description=desc,
        threshold={"alt_season_pct_above_ma": 0.05},
        source="price",
        extras={"ma50": round(ma50, 5)},
    )


# ─── Registry + bulk evaluation ─────────────────────────────────────────────

# (name, function, applicable_tickers) — None means "all".
# Cross-asset indicators are listed with a sentinel ticker so the UI knows
# where to display them.
INDICATOR_REGISTRY: list[tuple[str, Callable, Optional[list[str]]]] = [
    ("pi_cycle_top", pi_cycle_top, ["BTC", "ETH"]),
    ("two_hundred_week_distance", two_hundred_week_distance, None),
    ("stock_to_flow", stock_to_flow, ["BTC"]),
    ("nupl", nupl, ["BTC", "ETH"]),
    ("sopr_proxy", sopr_proxy, ["BTC", "ETH"]),
    ("puell_multiple", puell_multiple, ["BTC", "ETH"]),
    ("hash_ribbons", hash_ribbons, ["BTC"]),
    ("exchange_net_flow", exchange_net_flow, ["BTC", "ETH"]),
    ("rhodl_proxy", rhodl_proxy, ["BTC", "ETH"]),
]


def evaluate_all(tickers: Optional[list[str]] = None) -> list[dict]:
    """Run every applicable indicator across every ticker.
    Returns a flat list of IndicatorResult dicts."""
    if tickers is None:
        tickers = list(lt.TICKER_MAP.keys())
    out: list[dict] = []
    for name, fn, applicable in INDICATOR_REGISTRY:
        targets = applicable if applicable else tickers
        for ticker in targets:
            if ticker not in tickers:
                continue
            try:
                out.append(fn(ticker).to_dict())
            except Exception as e:
                log.warning("indicator %s for %s failed: %s", name, ticker, e)
                out.append(_na(name, ticker, f"error: {type(e).__name__}").to_dict())
    # Cross-asset (no ticker argument).
    try:
        out.append(btc_dominance_proxy().to_dict())
    except Exception as e:
        log.warning("dominance proxy failed: %s", e)
    try:
        out.append(eth_btc_ratio().to_dict())
    except Exception as e:
        log.warning("eth_btc_ratio failed: %s", e)
    return out


def composite_score(ticker: str) -> dict:
    """Aggregate every indicator's signal into a single 0..1 risk-off-ness score
    for an asset. Bullish indicators push the score toward 0; bearish toward 1.
    Indicators returning 'unavailable' are dropped from the average."""
    results = []
    for name, fn, applicable in INDICATOR_REGISTRY:
        if applicable and ticker not in applicable:
            continue
        try:
            r = fn(ticker)
        except Exception:
            continue
        if r.signal == "bullish":
            results.append((r.name, 0.0))
        elif r.signal == "bearish":
            results.append((r.name, 1.0))
        elif r.signal == "neutral":
            results.append((r.name, 0.5))
        # unavailable → skip
    if not results:
        return {"score": None, "label": "no-data", "components": []}
    score = float(np.mean([s for _, s in results]))
    if score < 0.3:
        label = "accumulate"
    elif score < 0.5:
        label = "lean-bullish"
    elif score < 0.7:
        label = "lean-bearish"
    else:
        label = "defensive"
    return {"score": round(score, 3), "label": label,
            "components": [{"name": n, "score": s} for n, s in results]}
