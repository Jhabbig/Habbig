#!/usr/bin/env python3
"""
Polymarket 5-Minute Up/Down Trading Bot — Multi-Coin Paper Trading

Trades rolling 5-minute Up/Down markets on Polymarket for:
BTC, ETH, SOL, DOGE, XRP, BNB

Uses observation-based signals as "better shoes" — a small edge finder.
Each coin evaluated independently. $100 per trade, per coin.

Run: python3 polymarket_bot.py [--reset]
"""

import json
import os
import time
import requests
import argparse
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "crypto-dashboard"))
from ml_predictor import get_ml_prediction

# ─── BTC lead signal cache (refreshed once per cycle) ─────────────────
_btc_lead_cache = {"time": 0, "mom_3m": 0, "mom_1m": 0}

# ─── Config ───────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com/api/v3"
LOCAL_API = "http://localhost:8000"

TRADE_LOG = Path(__file__).parent / "poly_trades.json"
BOT_LOG = Path(__file__).parent / "poly_bot_activity.log"

# ─── Coins to trade ──────────────────────────────────────────────────
COINS = {
    "btc":  {"binance": "BTCUSDT",  "slug_prefix": "btc-updown-5m"},
    "eth":  {"binance": "ETHUSDT",  "slug_prefix": "eth-updown-5m"},
    "sol":  {"binance": "SOLUSDT",  "slug_prefix": "sol-updown-5m"},
    "doge": {"binance": "DOGEUSDT", "slug_prefix": "doge-updown-5m"},
    "xrp":  {"binance": "XRPUSDT",  "slug_prefix": "xrp-updown-5m"},
    "bnb":  {"binance": "BNBUSDT",  "slug_prefix": "bnb-updown-5m"},
}

# ─── Trading Parameters ──────────────────────────────────────────────
BET_AMOUNT = 100.0
STARTING_BALANCE = 10000.0
MIN_EDGE = 0.06


# ─── State ────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.peak_balance = STARTING_BALANCE
        self.trades = []
        self.pending = {}  # coin -> pending bet dict


MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB


def log(msg):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    # Rotate log if it exceeds MAX_LOG_SIZE: keep last half
    if os.path.exists(BOT_LOG) and os.path.getsize(BOT_LOG) > MAX_LOG_SIZE:
        with open(BOT_LOG, "r") as f:
            lines = f.readlines()
        with open(BOT_LOG, "w") as f:
            f.writelines(lines[len(lines)//2:])
    with open(BOT_LOG, "a") as f:
        f.write(line + "\n")
    print(f"  {msg}")


def save_state(state):
    data = {
        "balance": round(state.balance, 2),
        "total_trades": state.total_trades,
        "wins": state.wins,
        "losses": state.losses,
        "total_pnl": round(state.total_pnl, 2),
        "peak_balance": round(state.peak_balance, 2),
        "pending": state.pending,
        "trades": state.trades[-500:],
    }
    tmp = TRADE_LOG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, TRADE_LOG)


def load_state():
    state = BotState()
    if TRADE_LOG.exists():
        try:
            with open(TRADE_LOG) as f:
                data = json.load(f)
            state.balance = data.get("balance", STARTING_BALANCE)
            state.total_trades = data.get("total_trades", 0)
            state.wins = data.get("wins", 0)
            state.losses = data.get("losses", 0)
            state.total_pnl = data.get("total_pnl", 0)
            state.peak_balance = data.get("peak_balance", STARTING_BALANCE)
            state.pending = data.get("pending", {})
            state.trades = data.get("trades", [])
            # Migrate old single pending to dict
            if isinstance(state.pending, dict) and "slug" in state.pending:
                old = state.pending
                state.pending = {"btc": old}
        except Exception:
            pass
    return state


# ─── Market Discovery ─────────────────────────────────────────────────

def get_next_market(coin):
    """Find the next upcoming 5-min market for a given coin."""
    cfg = COINS[coin]
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    current_boundary = now_ts - (now_ts % 300)

    for offset in range(1, 6):
        target_ts = current_boundary + (offset * 300)
        slug = f"{cfg['slug_prefix']}-{target_ts}"

        try:
            resp = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
            if resp.ok:
                data = resp.json()
                if data:
                    ev = data[0]
                    markets = ev.get("markets", [])
                    if not markets:
                        continue
                    market = markets[0]
                    prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
                    if len(prices) < 2:
                        continue
                    tokens = json.loads(market.get("clobTokenIds", "[]")) if market.get("clobTokenIds") else []

                    end_dt = datetime.fromisoformat(ev["endDate"].replace("Z", "+00:00"))
                    start_dt = end_dt - timedelta(minutes=5)

                    seconds_until_start = (start_dt - now).total_seconds()
                    if seconds_until_start < 60:
                        continue

                    return {
                        "slug": slug,
                        "coin": coin,
                        "title": ev.get("title", ""),
                        "start_dt": start_dt.isoformat(),
                        "end_dt": end_dt.isoformat(),
                        "up_price": float(prices[0]),
                        "down_price": float(prices[1]),
                        "condition_id": market.get("conditionId", ""),
                        "up_token_id": tokens[0] if len(tokens) > 0 else "",
                        "down_token_id": tokens[1] if len(tokens) > 1 else "",
                        "seconds_until_start": seconds_until_start,
                    }
        except Exception:
            continue

    return None


# ─── Binance Data ─────────────────────────────────────────────────────

def get_binance_klines(symbol, interval="1m", limit=30):
    """Fetch klines for any Binance symbol."""
    try:
        resp = requests.get(f"{BINANCE_API}/klines",
                            params={"symbol": symbol, "interval": interval, "limit": limit},
                            timeout=10)
        if resp.ok:
            return [{
                "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "taker_buy_vol": float(k[9]),
            } for k in resp.json() if len(k) >= 10]
    except Exception:
        pass
    return []


# ─── CLOB Order Book ──────────────────────────────────────────────────

def get_clob_book(token_id):
    """Fetch the real order book from Polymarket CLOB for a token."""
    try:
        resp = requests.get(f"{CLOB_API}/book",
                            params={"token_id": token_id}, timeout=10)
        if resp.ok:
            book = resp.json()
            bids = sorted(book.get("bids", []),
                          key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(book.get("asks", []),
                          key=lambda x: float(x["price"]))
            return {"bids": bids, "asks": asks}
    except Exception:
        pass
    return {"bids": [], "asks": []}


def simulate_market_buy(book, spend_amount):
    """
    Simulate a market buy order by walking the ask side of the order book.
    Returns (avg_fill_price, total_shares, fills_detail) or None if can't fill.
    """
    asks = book.get("asks", [])
    if not asks:
        return None

    remaining = spend_amount
    total_shares = 0.0
    fills = []

    for level in asks:
        price = float(level["price"])
        available = float(level["size"])
        if price <= 0 or price >= 1.0:
            continue

        # How many shares can we buy at this price with remaining funds?
        max_shares_at_price = remaining / price
        filled = min(max_shares_at_price, available)

        cost = filled * price
        total_shares += filled
        remaining -= cost
        fills.append({"price": price, "shares": round(filled, 2), "cost": round(cost, 2)})

        if remaining < 0.01:
            break

    if total_shares == 0:
        return None

    spent = spend_amount - remaining
    avg_price = spent / total_shares

    return {
        "avg_price": round(avg_price, 4),
        "total_shares": round(total_shares, 2),
        "total_cost": round(spent, 2),
        "unfilled": round(remaining, 2),
        "levels_hit": len(fills),
        "fills": fills,
        "best_ask": float(asks[0]["price"]) if asks else 0,
        "slippage": round(avg_price - float(asks[0]["price"]), 4) if asks else 0,
    }


POLYMARKET_FEE_RATE = 0.02  # 2% fee on net winnings


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ema(values, period):
    if not values:
        return 0
    mult = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = (v - ema) * mult + ema
    return ema


def get_realtime_signals(coin):
    """Build fresh signals from Binance data for a specific coin."""
    symbol = COINS[coin]["binance"]
    candles_1m = get_binance_klines(symbol, "1m", 30)
    candles_5m = get_binance_klines(symbol, "5m", 24)

    if not candles_1m or len(candles_1m) < 10:
        return {}

    closes_1m = [c["close"] for c in candles_1m]
    volumes_1m = [c["volume"] for c in candles_1m]
    highs_1m = [c["high"] for c in candles_1m]
    lows_1m = [c["low"] for c in candles_1m]
    closes_5m = [c["close"] for c in candles_5m] if candles_5m else closes_1m

    current_price = closes_1m[-1]
    rsi_1m = compute_rsi(closes_1m, 14)
    ema_5 = compute_ema(closes_1m, 5)
    ema_12 = compute_ema(closes_1m, 12)
    ema_26 = compute_ema(closes_1m, 26) if len(closes_1m) >= 26 else ema_12

    macd_line = ema_12 - ema_26
    # Build a series of MACD values for signal line calculation
    _macd_series = []
    for i in range(min(26, len(closes_1m)), len(closes_1m) + 1):
        _e12 = compute_ema(closes_1m[:i], 12)
        _e26 = compute_ema(closes_1m[:i], 26)
        _macd_series.append(_e12 - _e26)
    macd_signal = compute_ema(_macd_series, 9) if _macd_series else 0
    macd_hist = macd_line - macd_signal

    avg_vol = sum(volumes_1m[:-1]) / max(len(volumes_1m) - 1, 1)
    vol_ratio = volumes_1m[-1] / avg_vol if avg_vol > 0 else 1.0

    recent_candle = candles_1m[-1]
    taker_buy_ratio = recent_candle["taker_buy_vol"] / recent_candle["volume"] if recent_candle["volume"] > 0 else 0.5

    momentum_5 = (closes_1m[-1] - closes_1m[-6]) / closes_1m[-6] * 100 if len(closes_1m) >= 6 and closes_1m[-6] != 0 else 0
    momentum_1 = (closes_1m[-1] - closes_1m[-2]) / closes_1m[-2] * 100 if len(closes_1m) >= 2 and closes_1m[-2] != 0 else 0

    sma_20 = sum(closes_1m[-20:]) / min(len(closes_1m), 20)
    price_vs_sma = (current_price - sma_20) / sma_20 * 100 if sma_20 != 0 else 0

    consecutive_up = consecutive_down = 0
    for i in range(len(closes_1m) - 1, 0, -1):
        if closes_1m[i] > closes_1m[i - 1]:
            if consecutive_down > 0: break
            consecutive_up += 1
        elif closes_1m[i] < closes_1m[i - 1]:
            if consecutive_up > 0: break
            consecutive_down += 1
        else:
            break

    ema_5m_short = compute_ema(closes_5m, 5) if len(closes_5m) >= 5 else current_price
    ema_5m_long = compute_ema(closes_5m, 12) if len(closes_5m) >= 12 else current_price
    trend_5m = "up" if ema_5m_short > ema_5m_long else "down"

    recent_bodies = []
    for c in candles_1m[-5:]:
        body = abs(c["close"] - c["open"])
        wick = c["high"] - c["low"]
        recent_bodies.append(body / wick if wick > 0 else 0.5)

    # Order book imbalance
    ob_imbalance = get_order_book_imbalance(coin)

    # BTC lead signal (for altcoins)
    btc_lead = get_btc_lead_signal(coin)

    return {
        "rsi_1m": rsi_1m, "ema_5": ema_5, "ema_12": ema_12,
        "macd_hist": macd_hist, "vol_ratio": vol_ratio,
        "taker_buy_ratio": taker_buy_ratio,
        "momentum_5": momentum_5, "momentum_1": momentum_1,
        "price_vs_sma": price_vs_sma,
        "consecutive_up": consecutive_up, "consecutive_down": consecutive_down,
        "trend_5m": trend_5m,
        "avg_body_ratio": sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0.5,
        "current_price": current_price,
        "ob_imbalance": ob_imbalance,
        "btc_lead_3m": btc_lead[0],
        "btc_lead_1m": btc_lead[1],
    }


def get_order_book_imbalance(coin):
    """Fetch order book depth and compute bid/ask imbalance. Returns [-1, +1]."""
    try:
        symbol = COINS[coin]["binance"]
        resp = requests.get(f"{BINANCE_API}/depth",
                            params={"symbol": symbol, "limit": 20}, timeout=5)
        if resp.ok:
            book = resp.json()
            total_bids = sum(float(b[1]) for b in book["bids"])
            total_asks = sum(float(a[1]) for a in book["asks"])
            total = total_bids + total_asks
            if total > 0:
                return (total_bids - total_asks) / total
    except Exception:
        pass
    return 0.0


def get_btc_lead_signal(coin):
    """Get BTC's recent momentum as a leading indicator for altcoins.
    Returns (mom_3m, mom_1m). For BTC itself returns (0, 0)."""
    global _btc_lead_cache
    if coin == "btc":
        return (0.0, 0.0)

    now = time.time()
    if now - _btc_lead_cache["time"] < 30:  # cache for 30s
        return (_btc_lead_cache["mom_3m"], _btc_lead_cache["mom_1m"])

    try:
        candles = get_binance_klines("BTCUSDT", "1m", 5)
        if candles and len(candles) >= 4:
            closes = [c["close"] for c in candles]
            mom_3m = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] != 0 else 0
            mom_1m = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] != 0 else 0
            _btc_lead_cache = {"time": now, "mom_3m": mom_3m, "mom_1m": mom_1m}
            return (mom_3m, mom_1m)
        else:
            # Not enough candles — cache the zero result to avoid repeated stale lookups
            _btc_lead_cache = {"time": now, "mom_3m": 0.0, "mom_1m": 0.0}
    except Exception:
        pass
    return (0.0, 0.0)


def get_pattern_prediction(coin):
    """Pattern-matching predictor for any coin."""
    try:
        symbol = COINS[coin]["binance"]
        candles = get_binance_klines(symbol, "5m", 72)
        if len(candles) < 20:
            return 0.0

        def make_features(sl):
            closes = [c["close"] for c in sl]
            volumes = [c["volume"] for c in sl]
            mom = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] != 0 else 0
            rsi = compute_rsi(closes, min(14, len(closes) - 1))
            avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
            vol_trend = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
            green_count = sum(1 for c in sl[-5:] if c["close"] > c["open"])
            return (mom, rsi, vol_trend, green_count)

        outcomes = []
        for i in range(10, len(candles) - 1):
            features = make_features(candles[i - 10:i])
            went_up = candles[i]["close"] > candles[i]["open"]
            outcomes.append((features, went_up))

        if not outcomes:
            return 0.0

        current = make_features(candles[-10:])

        def dist(f1, f2):
            return (abs(f1[0]-f2[0])*10 + abs(f1[1]-f2[1])*0.02 +
                    abs(f1[2]-f2[2])*0.5 + abs(f1[3]-f2[3])*0.3)

        scored = sorted([(dist(current, f), up) for f, up in outcomes])
        k = min(15, len(scored))
        ups = sum(1 for _, up in scored[:k] if up)
        return (ups / k - 0.5) * 0.08
    except Exception:
        return 0.0


def get_dashboard_signals(coin):
    """Get dashboard signals for a specific coin."""
    ticker_map = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "doge": "DOGE", "xrp": "XRP", "bnb": "BNB"}
    try:
        resp = requests.get(f"{LOCAL_API}/_internal/bot/signals", timeout=5)
        if resp.ok:
            return resp.json().get(ticker_map.get(coin, ""), {})
    except Exception:
        pass
    return {}


# ─── Edge Calculation ──────────────────────────────────────────────────

def estimate_up_probability(rt_signals, dash_signals, coin):
    """Estimate probability of coin going up in next 5 minutes."""
    base_prob = 0.50
    nudge = 0.0

    if not rt_signals:
        return base_prob

    # 1. SHORT-TERM MOMENTUM ±3%
    mom5 = rt_signals.get("momentum_5", 0)
    if mom5 > 0.03:
        nudge += min(0.03, mom5 * 0.5)
    elif mom5 < -0.03:
        nudge -= min(0.03, abs(mom5) * 0.5)

    # 2. RSI EXTREMES ±3%
    rsi = rt_signals.get("rsi_1m", 50)
    if rsi > 75:
        nudge -= min(0.03, (rsi - 75) * 0.001)
    elif rsi < 25:
        nudge += min(0.03, (25 - rsi) * 0.001)
    elif rsi > 55:
        nudge += 0.01
    elif rsi < 45:
        nudge -= 0.01

    # 3. EMA CROSSOVER ±2%
    ema5 = rt_signals.get("ema_5", 0)
    ema12 = rt_signals.get("ema_12", 0)
    if ema5 > 0 and ema12 > 0:
        spread = (ema5 - ema12) / ema12 * 100
        if spread > 0.01: nudge += 0.02
        elif spread < -0.01: nudge -= 0.02

    # 4. MACD ±2%
    macd_h = rt_signals.get("macd_hist", 0)
    if macd_h > 0: nudge += min(0.02, abs(macd_h) * 0.1)
    elif macd_h < 0: nudge -= min(0.02, abs(macd_h) * 0.1)

    # 5. VOLUME + DIRECTION ±3%
    vol_ratio = rt_signals.get("vol_ratio", 1.0)
    taker_buy = rt_signals.get("taker_buy_ratio", 0.5)
    if vol_ratio > 1.5:
        if taker_buy > 0.55: nudge += min(0.03, (vol_ratio - 1) * 0.02)
        elif taker_buy < 0.45: nudge -= min(0.03, (vol_ratio - 1) * 0.02)

    # 6. TAKER BUY ±2%
    if vol_ratio <= 1.5:
        if taker_buy > 0.55: nudge += 0.02
        elif taker_buy < 0.45: nudge -= 0.02

    # 7. MEAN REVERSION ±2%
    pvsma = rt_signals.get("price_vs_sma", 0)
    if pvsma > 0.1: nudge -= min(0.02, pvsma * 0.1)
    elif pvsma < -0.1: nudge += min(0.02, abs(pvsma) * 0.1)

    # 8. CONSECUTIVE CANDLES ±2%
    if rt_signals.get("consecutive_up", 0) >= 4: nudge -= 0.02
    elif rt_signals.get("consecutive_up", 0) in (2, 3): nudge += 0.01
    if rt_signals.get("consecutive_down", 0) >= 4: nudge += 0.02
    elif rt_signals.get("consecutive_down", 0) in (2, 3): nudge -= 0.01

    # 9. TREND ±2%
    if rt_signals.get("trend_5m") == "up": nudge += 0.02
    elif rt_signals.get("trend_5m") == "down": nudge -= 0.02

    # 10. CANDLE CONVICTION ±1%
    br = rt_signals.get("avg_body_ratio", 0.5)
    mom = rt_signals.get("momentum_1", 0)
    if br > 0.6 and mom > 0: nudge += 0.01
    elif br > 0.6 and mom < 0: nudge -= 0.01

    # 11-12. DASHBOARD SIGNALS (if available) ±2% each
    if dash_signals:
        g = dash_signals.get("avg_gain_per_sec", 0)
        l = dash_signals.get("avg_loss_per_sec", 0)
        if g > 0 and l > 0:
            sr = g / l
            if sr > 1.1: nudge += 0.02
            elif sr < 0.9: nudge -= 0.02

        cs = dash_signals.get("last_cross_sec")
        cd = dash_signals.get("last_cross_direction")
        if cs is not None and cd:
            if cd == "positive":
                tl = dash_signals.get("avg_time_to_peak", 150) - cs
                if tl > 60: nudge += 0.02
                elif tl < -30: nudge -= 0.01
            elif cd == "negative":
                tl = dash_signals.get("avg_time_to_trough", 150) - cs
                if tl > 60: nudge -= 0.02
                elif tl < -30: nudge += 0.01

    # 13. ML ENSEMBLE ±5% (BTC has LSTM+NN+GBT, others have NN+GBT if data exists)
    ml_prob, ml_info = get_ml_prediction(coin)
    ml_nudge = (ml_prob - 0.5) * 0.10
    conf = ml_info.get("confidence", "low")
    if conf == "high": ml_nudge *= 1.5
    elif conf == "low": ml_nudge *= 0.3
    nudge += ml_nudge

    # 14. PATTERN MATCH ±4%
    nudge += get_pattern_prediction(coin)

    # 15. ORDER BOOK IMBALANCE ±3%
    ob_imb = rt_signals.get("ob_imbalance", 0)
    if abs(ob_imb) > 0.1:
        nudge += ob_imb * 0.03  # linear: max ±3%

    # 16. BTC CROSS-CORRELATION ±3% (altcoins only)
    btc_lead = rt_signals.get("btc_lead_3m", 0)
    if coin != "btc" and abs(btc_lead) > 0.02:
        beta = {"eth": 1.0, "sol": 1.3, "doge": 1.5, "xrp": 1.2, "bnb": 0.8}.get(coin, 1.0)
        nudge += max(-0.03, min(0.03, btc_lead * 0.15 * beta))

    nudge = max(-0.20, min(0.20, nudge))
    return max(0.30, min(0.70, 0.50 + nudge))


def evaluate_trade(market, rt_signals, dash_signals, coin):
    """Evaluate trade using real CLOB order book prices."""
    our_up = estimate_up_probability(rt_signals, dash_signals, coin)
    our_down = 1.0 - our_up

    # Fetch real order books from CLOB
    up_token = market.get("up_token_id", "")
    down_token = market.get("down_token_id", "")

    up_fill = None
    down_fill = None

    if up_token:
        up_book = get_clob_book(up_token)
        up_fill = simulate_market_buy(up_book, BET_AMOUNT)
    if down_token:
        down_book = get_clob_book(down_token)
        down_fill = simulate_market_buy(down_book, BET_AMOUNT)

    # Use real fill prices if available, fall back to Gamma prices
    if up_fill:
        real_up_price = up_fill["avg_price"]
    else:
        real_up_price = market["up_price"]

    if down_fill:
        real_down_price = down_fill["avg_price"]
    else:
        real_down_price = market["down_price"]

    up_edge = our_up - real_up_price
    down_edge = our_down - real_down_price

    if up_edge > down_edge and up_edge >= MIN_EDGE:
        return "up", up_edge, our_up, up_fill
    elif down_edge > up_edge and down_edge >= MIN_EDGE:
        return "down", down_edge, our_down, down_fill
    return None, max(up_edge, down_edge), our_up, None


# ─── Resolution ────────────────────────────────────────────────────────

def resolve_pending(state, coin):
    """Check if a pending bet for a coin has resolved."""
    if coin not in state.pending:
        return

    pending = state.pending[coin]
    end_dt = datetime.fromisoformat(pending["end_dt"])
    now = datetime.now(timezone.utc)

    if now < end_dt + timedelta(seconds=30):
        return

    slug = pending["slug"]
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
        if resp.ok:
            data = resp.json()
            if data:
                market = data[0].get("markets", [{}])[0]
                prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
                if len(prices) < 2:
                    return
                up_final = float(prices[0])
                down_final = float(prices[1])

                if up_final > 0.9:
                    winner = "up"
                elif down_final > 0.9:
                    winner = "down"
                else:
                    if now > end_dt + timedelta(minutes=5):
                        log(f"[{coin.upper()}] Market {slug} didn't resolve, skipping")
                        state.balance += pending["amount"]
                        del state.pending[coin]
                        save_state(state)
                    return

                bet_side = pending["side"]
                buy_price = pending["buy_price"]
                bet_amount = pending["amount"]
                shares = pending.get("shares", bet_amount / buy_price)

                if bet_side == winner:
                    # Winning: shares pay $1 each
                    gross_return = shares * 1.0
                    gross_pnl = gross_return - bet_amount
                    fee = gross_pnl * POLYMARKET_FEE_RATE if gross_pnl > 0 else 0
                    pnl = gross_pnl - fee
                    state.wins += 1
                    result = "WIN"
                else:
                    # Losing: shares worth $0, lose entire cost
                    pnl = -bet_amount
                    fee = 0
                    state.losses += 1
                    result = "LOSS"

                # Balance was already reduced by bet_amount; add back what we get
                state.balance += (bet_amount + pnl)  # win: cost+profit, loss: 0
                state.total_pnl += pnl
                state.total_trades += 1
                if state.balance > state.peak_balance:
                    state.peak_balance = state.balance

                state.trades.append({
                    "slug": slug, "coin": coin, "side": bet_side,
                    "buy_price": buy_price, "amount": bet_amount,
                    "shares": round(shares, 4), "winner": winner,
                    "pnl": round(pnl, 2), "fee": round(fee, 2),
                    "pnl_pct": round(pnl / bet_amount * 100, 2),
                    "edge": pending.get("edge", 0),
                    "our_prob": pending.get("our_prob", 0.5),
                    "slippage": pending.get("slippage", 0),
                    "time": pending.get("time", ""), "result": result,
                })
                del state.pending[coin]
                save_state(state)

                fee_str = f" | Fee: ${fee:.2f}" if fee > 0 else ""
                log(f"[{coin.upper()}] RESOLVED [{result}] {slug} | "
                    f"Bet {bet_side.upper()} @ ${buy_price:.3f} | "
                    f"Winner: {winner.upper()} | PnL: ${pnl:+.2f}{fee_str} | "
                    f"Balance: ${state.balance:,.2f}")
    except Exception as e:
        log(f"[{coin.upper()}] Resolution error: {e}")


# ─── Main Loop ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    if args.reset:
        TRADE_LOG.unlink(missing_ok=True)
        BOT_LOG.unlink(missing_ok=True)

    state = load_state()
    coins_str = ", ".join(c.upper() for c in COINS)
    log(f"Bot started — PAPER mode | Balance: ${state.balance:,.2f} | Coins: {coins_str}")
    if state.total_trades > 0:
        wr = state.wins / state.total_trades * 100
        log(f"History: {state.total_trades} trades, {state.wins}W/{state.losses}L "
            f"({wr:.1f}%), PnL: ${state.total_pnl:+.2f}")

    while True:
        try:
            # 1. Check all pending resolutions
            for coin in list(state.pending.keys()):
                resolve_pending(state, coin)

            # 2. For each coin, look for trading opportunities
            committed_this_cycle = 0.0
            for coin in COINS:
                # Skip if we already have a pending bet for this coin
                if coin in state.pending:
                    continue

                # Check balance accounting for bets already placed this cycle
                if (state.balance - committed_this_cycle) < BET_AMOUNT:
                    continue

                # Find next market
                market = get_next_market(coin)
                if not market:
                    continue

                # Get signals
                rt_signals = get_realtime_signals(coin)
                dash_signals = get_dashboard_signals(coin)

                # Evaluate using real CLOB order book
                side, edge, our_prob, fill_info = evaluate_trade(
                    market, rt_signals, dash_signals, coin)
                if side is None:
                    continue

                # Use realistic fill from CLOB order book
                if fill_info:
                    buy_price = fill_info["avg_price"]
                    shares = fill_info["total_shares"]
                    actual_cost = fill_info["total_cost"]
                    slippage = fill_info["slippage"]
                    levels_hit = fill_info["levels_hit"]
                    best_ask = fill_info["best_ask"]
                else:
                    # Fallback to Gamma prices if CLOB unavailable
                    buy_price = market["up_price"] if side == "up" else market["down_price"]
                    if buy_price <= 0:
                        log(f"[{coin.upper()}] Skipping trade: buy_price is {buy_price}")
                        continue
                    shares = BET_AMOUNT / buy_price
                    actual_cost = BET_AMOUNT
                    slippage = 0
                    levels_hit = 0
                    best_ask = buy_price

                state.pending[coin] = {
                    "slug": market["slug"], "coin": coin,
                    "title": market["title"], "side": side,
                    "buy_price": buy_price, "amount": actual_cost,
                    "shares": round(shares, 4),
                    "edge": round(edge, 4), "our_prob": round(our_prob, 4),
                    "start_dt": market["start_dt"], "end_dt": market["end_dt"],
                    "time": datetime.now(timezone.utc).isoformat(),
                    "best_ask": best_ask,
                    "slippage": round(slippage, 4),
                    "levels_hit": levels_hit,
                }
                state.balance -= actual_cost
                committed_this_cycle += actual_cost
                save_state(state)

                if rt_signals:
                    ob = rt_signals.get('ob_imbalance', 0)
                    btc_l = rt_signals.get('btc_lead_3m', 0)
                    extras = f" | OB={ob:+.2f}"
                    if coin != "btc":
                        extras += f" | BTClead={btc_l:+.3f}%"
                    log(f"[{coin.upper()}] RSI={rt_signals.get('rsi_1m', 0):.0f} | "
                        f"Mom={rt_signals.get('momentum_5', 0):+.3f}% | "
                        f"Vol={rt_signals.get('vol_ratio', 1):.1f}x | "
                        f"Buy%={rt_signals.get('taker_buy_ratio', 0.5)*100:.0f}% | "
                        f"Trend={rt_signals.get('trend_5m', '?')}{extras}")

                slip_str = f" | Slip=${slippage:+.3f}" if slippage > 0 else ""
                log(f"[{coin.upper()}] BET {side.upper()} @ ${buy_price:.3f} (ask=${best_ask:.3f}) | "
                    f"${actual_cost:.2f} → {shares:.1f} shares | "
                    f"Edge: {edge*100:+.1f}% | Prob: {our_prob*100:.0f}% | "
                    f"Levels: {levels_hit}{slip_str} | {market['title']}")

            # 3. Wait before next scan
            n_pending = len(state.pending)
            if n_pending > 0:
                # Find earliest resolution time
                earliest_end = None
                for p in state.pending.values():
                    end = datetime.fromisoformat(p["end_dt"])
                    if earliest_end is None or end < earliest_end:
                        earliest_end = end
                wait = (earliest_end - datetime.now(timezone.utc)).total_seconds() + 35
                wait = max(10, min(wait, 120))
                time.sleep(wait)
            else:
                time.sleep(30)

        except KeyboardInterrupt:
            log("Bot stopped.")
            save_state(state)
            break
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
