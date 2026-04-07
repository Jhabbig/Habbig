#!/usr/bin/env python3
"""
Polymarket Stock Up/Down Prediction Bot

Discovers daily stock Up/Down markets on Polymarket, predicts direction
using technical analysis + ML signals, and paper-trades.

Stocks tracked: AAPL, MSFT, AMZN, GOOGL, META, TSLA, NVDA, NFLX,
                PLTR, COIN, HOOD, RKLB, ABNB, OPEN, SPY, QQQ, EWY

Resolution: Pyth oracle 1-min candle close prices.
  - "Up" if today's close > previous close
  - "Down" if today's close < previous close

Run: python3 stock_predictor_bot.py [--reset] [--once]
"""

import json
import os
import tempfile
import time
import math
import argparse
import requests
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

# ML model integration
try:
    from stock_ml_model import get_ml_stock_prediction
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

# Smart betting integration
try:
    from smart_betting import (
        SignalConcordanceFilter, AdaptiveKelly, DynamicEdgeThreshold,
        PerformanceTracker, RiskManager, evaluate_bet_enhanced,
    )
    SMART_BETTING = True
except ImportError:
    SMART_BETTING = False

# Enhanced data integration
try:
    from enhanced_data import build_enhanced_features
    ENHANCED_DATA = True
except ImportError:
    ENHANCED_DATA = False

# Sentiment integration
try:
    from sentiment_signals import build_sentiment_features
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    import subprocess
    subprocess.check_call(["pip3", "install", "yfinance", "-q"])
    import yfinance as yf

try:
    import numpy as np
except ImportError:
    print("Installing numpy...")
    import subprocess
    subprocess.check_call(["pip3", "install", "numpy", "-q"])
    import numpy as np

# ─── Config ───────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"

TRADE_LOG = Path(__file__).parent / "stock_trades.json"
BOT_LOG = Path(__file__).parent / "stock_bot_activity.log"

# Stocks that Polymarket lists for daily Up/Down markets
STOCKS = {
    "aapl":  {"yf": "AAPL",  "name": "Apple"},
    "msft":  {"yf": "MSFT",  "name": "Microsoft"},
    "amzn":  {"yf": "AMZN",  "name": "Amazon"},
    "googl": {"yf": "GOOGL", "name": "Google"},
    "meta":  {"yf": "META",  "name": "Meta"},
    "tsla":  {"yf": "TSLA",  "name": "Tesla"},
    "nvda":  {"yf": "NVDA",  "name": "NVIDIA"},
    "nflx":  {"yf": "NFLX",  "name": "Netflix"},
    "pltr":  {"yf": "PLTR",  "name": "Palantir"},
    "coin":  {"yf": "COIN",  "name": "Coinbase"},
    "hood":  {"yf": "HOOD",  "name": "Robinhood"},
    "rklb":  {"yf": "RKLB",  "name": "Rocket Lab"},
    "abnb":  {"yf": "ABNB",  "name": "Airbnb"},
    "open":  {"yf": "OPEN",  "name": "Opendoor"},
    "spy":   {"yf": "SPY",   "name": "SPY ETF"},
    "qqq":   {"yf": "QQQ",   "name": "QQQ ETF"},
    "ewy":   {"yf": "EWY",   "name": "EWY ETF"},
}

# ─── Trading Parameters ──────────────────────────────────────────────
BET_AMOUNT = 100.0
STARTING_BALANCE = 10000.0
MIN_EDGE = 0.08          # Minimum edge (predicted prob - market prob) to bet
MIN_CONFIDENCE = 0.55    # Minimum prediction confidence to consider

# ─── Logging ──────────────────────────────────────────────────────────
def log(msg):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    with open(BOT_LOG, "a") as f:
        f.write(line + "\n")
    print(f"  {msg}")


# ─── State Management ────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.balance = STARTING_BALANCE
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.peak_balance = STARTING_BALANCE
        self.trades = []
        self.pending = {}   # ticker -> pending bet dict
        self.daily_bets = 0
        self.last_date = ""


def save_state(state):
    data = {
        "balance": round(state.balance, 2),
        "total_trades": state.total_trades,
        "wins": state.wins,
        "losses": state.losses,
        "total_pnl": round(state.total_pnl, 2),
        "peak_balance": round(state.peak_balance, 2),
        "pending": state.pending,
        "daily_bets": state.daily_bets,
        "last_date": state.last_date,
        "trades": state.trades[-500:],
    }
    # Atomic write: write to temp file then rename, so readers never see
    # a partially-written file.
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=TRADE_LOG.parent, suffix=".tmp", prefix="stock_trades_"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, TRADE_LOG)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_state():
    state = BotState()
    if TRADE_LOG.exists():
        try:
            data = json.loads(TRADE_LOG.read_text())
            state.balance = data.get("balance", STARTING_BALANCE)
            state.total_trades = data.get("total_trades", 0)
            state.wins = data.get("wins", 0)
            state.losses = data.get("losses", 0)
            state.total_pnl = data.get("total_pnl", 0)
            state.peak_balance = data.get("peak_balance", STARTING_BALANCE)
            state.pending = data.get("pending", {})
            state.daily_bets = data.get("daily_bets", 0)
            state.last_date = data.get("last_date", "")
            state.trades = data.get("trades", [])
        except Exception:
            pass
    return state


# ─── Market Discovery ────────────────────────────────────────────────
def find_stock_markets():
    """Find all active stock Up/Down markets on Polymarket."""
    markets = {}
    try:
        all_events = []
        for offset in range(0, 300, 50):
            resp = requests.get(f"{GAMMA_API}/events", params={
                "limit": 50,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "tag_slug": "stocks",
            }, timeout=15)
            events = resp.json()
            if not events:
                break
            all_events.extend(events)

        for e in all_events:
            slug = e.get("slug", "").lower()
            title = e.get("title", "")
            if "up-or-down" not in slug:
                continue

            # Extract ticker from slug: e.g. "aapl-up-or-down-on-april-6-2026"
            ticker = slug.split("-up-or-down")[0]
            if ticker not in STOCKS:
                continue

            event_markets = e.get("markets", [])
            if not event_markets:
                continue

            m = event_markets[0]
            outcomes = m.get("outcomes", [])
            prices = m.get("outcomePrices", [])

            # Both fields may be JSON strings
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)

            if len(outcomes) >= 2 and len(prices) >= 2:
                up_price = float(prices[0]) if outcomes[0] == "Up" else float(prices[1])
                down_price = float(prices[1]) if outcomes[0] == "Up" else float(prices[0])

                markets[ticker] = {
                    "title": title,
                    "slug": slug,
                    "event_slug": e.get("slug", ""),
                    "market_id": m.get("id", ""),
                    "condition_id": m.get("condition_id", ""),
                    "up_price": up_price,
                    "down_price": down_price,
                    "market_prob": up_price,
                    "volume": float(m.get("volume", 0) or 0),
                }

    except Exception as ex:
        log(f"Error fetching markets: {ex}")

    return markets


# ─── Stock Data & Technical Analysis ─────────────────────────────────
def fetch_stock_data(ticker_yf, period="3mo", interval="1d"):
    """Fetch historical OHLCV data from Yahoo Finance."""
    try:
        stock = yf.Ticker(ticker_yf)
        df = stock.history(period=period, interval=interval)
        if df.empty:
            return None
        return df
    except Exception as ex:
        log(f"Error fetching {ticker_yf}: {ex}")
        return None


def compute_rsi(closes, period=14):
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram."""
    if len(closes) < slow + signal:
        return 0, 0, 0

    def ema(data, span):
        alpha = 2 / (span + 1)
        result = [data[0]]
        for d in data[1:]:
            result.append(alpha * d + (1 - alpha) * result[-1])
        return np.array(result)

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line[-1], signal_line[-1], histogram[-1]


def compute_bollinger(closes, period=20, num_std=2):
    """Bollinger Band position: -1 (below lower) to +1 (above upper)."""
    if len(closes) < period:
        return 0.0
    sma = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    if std == 0:
        return 0.0
    upper = sma + num_std * std
    lower = sma - num_std * std
    current = closes[-1]
    # Normalize to [-1, 1]
    band_width = upper - lower
    if band_width == 0:
        return 0.0
    position = (current - lower) / band_width * 2 - 1
    return np.clip(position, -1, 1)


def compute_volume_signal(volumes, period=20):
    """Volume relative to average: >1 means above-average volume."""
    if len(volumes) < period:
        return 1.0
    avg = np.mean(volumes[-period:])
    if avg == 0:
        return 1.0
    return volumes[-1] / avg


def compute_momentum(closes, periods=[5, 10, 20]):
    """Multi-period momentum signals."""
    signals = []
    for p in periods:
        if len(closes) > p:
            mom = (closes[-1] - closes[-p]) / closes[-p]
            signals.append(mom)
        else:
            signals.append(0)
    return signals


def compute_consecutive_days(closes):
    """Count consecutive up or down days. Positive = up streak, negative = down."""
    if len(closes) < 2:
        return 0
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            if streak >= 0:
                streak += 1
            else:
                break
        elif closes[i] < closes[i - 1]:
            if streak <= 0:
                streak -= 1
            else:
                break
        else:
            break
    return streak


def compute_mean_reversion_signal(closes, period=20):
    """How far price is from moving average (z-score)."""
    if len(closes) < period:
        return 0.0
    sma = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    if std == 0:
        return 0.0
    return (closes[-1] - sma) / std


def day_of_week_bias(closes, dates=None):
    """Historical day-of-week return tendency using actual calendar days.

    If *dates* (a DatetimeIndex from yfinance) is provided, use the real
    weekdays.  Otherwise fall back to backward estimation (less accurate
    around market holidays).
    """
    from zoneinfo import ZoneInfo
    _et = ZoneInfo("US/Eastern")
    today_dow = min(datetime.now(_et).weekday(), 4)  # Clamp weekends to Friday
    if len(closes) < 60:
        return 0.0

    returns = np.diff(closes) / closes[:-1]
    if len(returns) < 20:
        return 0.0

    # Use actual dates when available (avoids holiday mis-mapping)
    if dates is not None and len(dates) >= len(returns) + 1:
        # returns[i] corresponds to dates[i+1] (the day the return was realised)
        dow_returns = []
        for i, ret in enumerate(returns):
            try:
                dow = dates[i + 1].weekday()
            except Exception:
                continue
            if dow == today_dow:
                dow_returns.append(ret)
    else:
        # Fallback: estimate weekdays by walking backwards
        dow_returns = []
        n = len(returns)
        day_map = [0] * n
        d = today_dow
        for i in range(n - 1, -1, -1):
            d = (d - 1) % 5
            day_map[i] = d
        for i, dow in enumerate(day_map):
            if dow == today_dow:
                dow_returns.append(returns[i])

    if not dow_returns:
        return 0.0
    return np.mean(dow_returns)


def get_premarket_signal(ticker_yf):
    """Get pre-market/after-hours price movement if available."""
    try:
        stock = yf.Ticker(ticker_yf)
        info = stock.info
        current = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose", 0)
        if current and prev_close and prev_close > 0:
            return (current - prev_close) / prev_close
    except Exception:
        pass
    return None


def get_market_correlation_signal(spy_data, stock_data):
    """Correlation between stock and SPY (market beta signal)."""
    if spy_data is None or stock_data is None:
        return 0.0, 0.0

    try:
        spy_closes = spy_data["Close"].values
        stock_closes = stock_data["Close"].values
        min_len = min(len(spy_closes), len(stock_closes), 60)
        if min_len < 10:
            return 0.0, 0.0

        spy_returns = np.diff(spy_closes[-min_len:]) / spy_closes[-min_len:-1]
        stock_returns = np.diff(stock_closes[-min_len:]) / stock_closes[-min_len:-1]

        correlation = np.corrcoef(spy_returns, stock_returns)[0, 1]
        # Beta = covariance / variance of market
        beta = np.cov(stock_returns, spy_returns)[0, 1] / np.var(spy_returns)
        return correlation, beta
    except Exception:
        return 0.0, 0.0


# ─── Prediction Engine ───────────────────────────────────────────────
def predict_direction(ticker, ticker_yf, spy_data=None):
    """
    Predict whether a stock will close up or down today.
    Returns (direction, confidence, signals_dict).
    direction: "up" or "down"
    confidence: 0.0 to 1.0
    """
    df = fetch_stock_data(ticker_yf)
    if df is None or len(df) < 30:
        return None, 0.0, {}

    closes = df["Close"].values.astype(float)
    volumes = df["Volume"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)

    signals = {}
    scores = []  # (signal_name, score, weight) — positive = bullish

    # 1. RSI
    rsi = compute_rsi(closes)
    signals["rsi"] = round(rsi, 1)
    if rsi < 30:
        scores.append(("rsi_oversold", 0.3, 1.5))   # Oversold → likely bounce
    elif rsi > 70:
        scores.append(("rsi_overbought", -0.3, 1.5)) # Overbought → likely drop
    elif rsi < 45:
        scores.append(("rsi_low", 0.1, 0.8))
    elif rsi > 55:
        scores.append(("rsi_high", -0.1, 0.8))

    # 2. MACD
    macd_line, signal_line, histogram = compute_macd(closes)
    signals["macd_hist"] = round(histogram, 4)
    if histogram > 0:
        scores.append(("macd_bullish", 0.2, 1.2))
    else:
        scores.append(("macd_bearish", -0.2, 1.2))
    # MACD crossover
    if macd_line > signal_line:
        scores.append(("macd_cross_up", 0.15, 1.0))
    else:
        scores.append(("macd_cross_down", -0.15, 1.0))

    # 3. Bollinger Band position
    bb_pos = compute_bollinger(closes)
    signals["bollinger"] = round(bb_pos, 3)
    if bb_pos < -0.8:
        scores.append(("bb_oversold", 0.25, 1.3))  # Near lower band → bounce
    elif bb_pos > 0.8:
        scores.append(("bb_overbought", -0.25, 1.3))
    else:
        scores.append(("bb_neutral", bb_pos * -0.05, 0.5))  # Mild mean reversion

    # 4. Volume signal
    vol_signal = compute_volume_signal(volumes)
    signals["volume_ratio"] = round(vol_signal, 2)
    # High volume on down day = bearish continuation, on up day = bullish
    last_return = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0
    if vol_signal > 1.5 and last_return > 0:
        scores.append(("volume_bull", 0.15, 1.0))
    elif vol_signal > 1.5 and last_return < 0:
        scores.append(("volume_bear", -0.15, 1.0))

    # 5. Multi-period momentum
    mom_5, mom_10, mom_20 = compute_momentum(closes)
    signals["mom_5d"] = round(mom_5 * 100, 2)
    signals["mom_10d"] = round(mom_10 * 100, 2)
    signals["mom_20d"] = round(mom_20 * 100, 2)

    # Short-term momentum continuation
    if mom_5 > 0.02:
        scores.append(("mom5_up", 0.15, 1.0))
    elif mom_5 < -0.02:
        scores.append(("mom5_down", -0.15, 1.0))

    # Medium momentum divergence (mean reversion)
    if mom_20 > 0.10:
        scores.append(("mom20_stretched_up", -0.1, 0.7))
    elif mom_20 < -0.10:
        scores.append(("mom20_stretched_down", 0.1, 0.7))

    # 6. Consecutive days (mean reversion)
    streak = compute_consecutive_days(closes)
    signals["streak"] = streak
    if streak >= 4:
        scores.append(("streak_up_long", -0.2, 1.2))  # Due for reversal
    elif streak <= -4:
        scores.append(("streak_down_long", 0.2, 1.2))
    elif streak >= 2:
        scores.append(("streak_up", -0.05, 0.6))
    elif streak <= -2:
        scores.append(("streak_down", 0.05, 0.6))

    # 7. Mean reversion z-score
    zscore = compute_mean_reversion_signal(closes)
    signals["zscore"] = round(zscore, 2)
    if zscore > 2:
        scores.append(("zscore_high", -0.2, 1.3))
    elif zscore < -2:
        scores.append(("zscore_low", 0.2, 1.3))
    elif zscore > 1:
        scores.append(("zscore_mild_high", -0.1, 0.8))
    elif zscore < -1:
        scores.append(("zscore_mild_low", 0.1, 0.8))

    # 8. Day-of-week bias
    dow_bias = day_of_week_bias(closes, dates=df.index)
    signals["dow_bias"] = round(dow_bias * 100, 3)
    if abs(dow_bias) > 0.001:
        scores.append(("dow_bias", np.sign(dow_bias) * 0.05, 0.5))

    # 9. Pre-market signal (strong when available)
    premarket = get_premarket_signal(ticker_yf)
    if premarket is not None:
        signals["premarket_pct"] = round(premarket * 100, 3)
        if abs(premarket) > 0.001:
            # Pre-market direction is very informative
            direction_score = np.sign(premarket) * min(abs(premarket) * 5, 0.4)
            scores.append(("premarket", direction_score, 2.5))

    # 10. Market correlation (SPY beta)
    correlation, beta = get_market_correlation_signal(spy_data, df)
    signals["spy_corr"] = round(correlation, 3)
    signals["beta"] = round(beta, 2)

    # If SPY data available and correlation is high, use SPY momentum
    if spy_data is not None and correlation > 0.5:
        spy_closes = spy_data["Close"].values.astype(float)
        spy_mom = (spy_closes[-1] - spy_closes[-2]) / spy_closes[-2] if len(spy_closes) >= 2 else 0
        if abs(spy_mom) > 0.002:
            spy_score = np.sign(spy_mom) * min(abs(spy_mom) * 3, 0.2) * correlation
            scores.append(("spy_momentum", spy_score, 1.5))
            signals["spy_mom"] = round(spy_mom * 100, 3)

    # 11. Intraday range analysis (volatility)
    if len(highs) >= 5 and len(lows) >= 5:
        avg_range = np.mean((highs[-5:] - lows[-5:]) / closes[-5:])
        signals["avg_range_pct"] = round(avg_range * 100, 2)
        # High volatility → mean reversion more likely
        if avg_range > 0.03:  # >3% daily range
            scores.append(("high_vol_reversion", -np.sign(last_return) * 0.1, 0.8))

    # 12. Gap analysis (open vs previous close)
    if len(df) >= 2:
        opens = df["Open"].values.astype(float)
        gap = (opens[-1] - closes[-2]) / closes[-2]
        signals["gap_pct"] = round(gap * 100, 3)
        # Gap fill tendency
        if abs(gap) > 0.01:
            scores.append(("gap_fill", -np.sign(gap) * 0.1, 0.7))

    # 13. ML Model prediction (trained on 5 years of data)
    ml_direction = None
    ml_confidence = 0.5
    if ML_AVAILABLE:
        try:
            ml_direction, ml_confidence, ml_details = get_ml_stock_prediction(
                ticker, spy_data)
            if ml_direction:
                signals["ml_direction"] = ml_direction
                signals["ml_confidence"] = round(ml_confidence, 4)
                signals["ml_ensemble_up"] = ml_details.get("ensemble_up_prob", 0.5)
                signals["ml_val_accuracy"] = ml_details.get("val_accuracy", 0)
                # ML model gets heavy weight (trained on 5 years)
                ml_score = (ml_confidence - 0.5) * 2  # map [0.5, 1] → [0, 1]
                if ml_direction == "down":
                    ml_score = -ml_score
                scores.append(("ml_model", ml_score * 0.4, 3.0))
        except Exception:
            pass

    # ─── Aggregate scores ────────────────────────────────────────────
    if not scores:
        return None, 0.0, signals

    weighted_sum = sum(s * w for _, s, w in scores)
    total_weight = sum(w for _, _, w in scores)
    raw_score = weighted_sum / total_weight if total_weight > 0 else 0

    # Convert to probability using sigmoid-like function
    # raw_score range roughly [-0.3, 0.3] → map to [0.35, 0.65]
    up_probability = 1 / (1 + math.exp(-raw_score * 8))

    signals["raw_score"] = round(raw_score, 4)
    signals["up_probability"] = round(up_probability, 4)

    if up_probability >= 0.5:
        return "up", up_probability, signals
    else:
        return "down", 1 - up_probability, signals


# ─── Betting Logic ───────────────────────────────────────────────────
def evaluate_bet(ticker, prediction, confidence, market_info, state):
    """Decide whether to place a bet based on edge over market price."""
    if prediction is None or confidence < MIN_CONFIDENCE:
        return None

    if prediction == "up":
        market_prob = market_info["up_price"]
        our_prob = confidence
    else:
        market_prob = market_info["down_price"]
        our_prob = confidence

    edge = our_prob - market_prob
    if edge < MIN_EDGE:
        return None

    # Kelly fraction (fractional Kelly for safety)
    if market_prob > 0 and market_prob < 1:
        odds = (1 / market_prob) - 1
        kelly = (our_prob * odds - (1 - our_prob)) / odds
        kelly_fraction = max(0, kelly * 0.25)  # Quarter Kelly
    else:
        kelly_fraction = 0

    bet_size = min(BET_AMOUNT, state.balance * kelly_fraction, state.balance * 0.05)
    bet_size = max(bet_size, 0)

    if bet_size < 5:  # Minimum $5 bet
        return None

    return {
        "ticker": ticker,
        "direction": prediction,
        "confidence": round(confidence, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "kelly": round(kelly_fraction, 4),
        "bet_size": round(bet_size, 2),
    }


# ─── Resolution ──────────────────────────────────────────────────────
def resolve_pending_bets(state):
    """Check if any pending bets can be resolved using yesterday's data."""
    resolved = []
    expired = []
    _et = ZoneInfo("America/New_York")
    today = datetime.now(_et).strftime("%Y-%m-%d")
    for ticker, bet in list(state.pending.items()):
        bet_date = bet.get("date", "")

        if bet_date >= today:
            continue  # Not yet resolvable

        # Expire bets older than 30 days that couldn't be resolved
        try:
            bet_dt = datetime.strptime(bet_date, "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            if (today_dt - bet_dt).days > 30:
                state.losses += 1
                state.total_trades += 1
                state.total_pnl -= bet["bet_size"]
                trade_record = {
                    **bet,
                    "actual": "expired",
                    "won": False,
                    "pnl": round(-bet["bet_size"], 2),
                    "balance_after": round(state.balance, 2),
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                state.trades.append(trade_record)
                expired.append(ticker)
                log(f"  ⏰ EXPIRED {ticker.upper()}: bet from {bet_date} "
                    f"older than 30 days | -${bet['bet_size']:.2f}")
                continue
        except (ValueError, KeyError):
            pass

        ticker_yf = STOCKS.get(ticker, {}).get("yf", ticker.upper())
        try:
            stock = yf.Ticker(ticker_yf)
            hist = stock.history(period="1mo", interval="1d")
            if len(hist) < 2:
                continue

            # Find the bet date in history
            for i in range(1, len(hist)):
                hist_date = hist.index[i].strftime("%Y-%m-%d")
                if hist_date == bet_date:
                    prev_close = float(hist["Close"].iloc[i - 1])
                    actual_close = float(hist["Close"].iloc[i])
                    actual_direction = "up" if actual_close > prev_close else "down"

                    won = actual_direction == bet["direction"]
                    if won:
                        # Net profit = full_payout - stake = stake * (1/market_prob - 1)
                        net_profit = bet["bet_size"] * (1 / bet["market_prob"] - 1)
                        # Stake was deducted at placement; return stake + net profit
                        state.balance += bet["bet_size"] + net_profit
                        state.wins += 1
                        state.total_pnl += net_profit
                        log(f"  ✓ WON {ticker.upper()}: bet {bet['direction']}, "
                            f"actual {actual_direction} | +${net_profit:.2f}")
                    else:
                        # Stake was already deducted at placement; no further deduction
                        state.losses += 1
                        state.total_pnl -= bet["bet_size"]
                        log(f"  ✗ LOST {ticker.upper()}: bet {bet['direction']}, "
                            f"actual {actual_direction} | -${bet['bet_size']:.2f}")

                    state.total_trades += 1
                    state.peak_balance = max(state.peak_balance, state.balance)

                    trade_record = {
                        **bet,
                        "actual": actual_direction,
                        "won": won,
                        "pnl": round(net_profit if won else -bet["bet_size"], 2),
                        "balance_after": round(state.balance, 2),
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    }
                    state.trades.append(trade_record)
                    resolved.append(ticker)
                    break
        except Exception as ex:
            log(f"Error resolving {ticker}: {ex}")

    for t in resolved + expired:
        del state.pending[t]


# ─── Main Loop ───────────────────────────────────────────────────────
def run_cycle(state):
    """Run one prediction cycle."""
    _et = ZoneInfo("America/New_York")
    today = datetime.now(_et).strftime("%Y-%m-%d")
    now_et = datetime.now(_et)

    print()
    log("=" * 60)
    log(f"Stock Prediction Bot — Cycle Start")
    log(f"Balance: ${state.balance:.2f} | W/L: {state.wins}/{state.losses} | "
        f"PnL: ${state.total_pnl:+.2f}")
    log("=" * 60)

    # Initialize smart betting components
    perf_tracker = None
    risk_mgr = None
    if SMART_BETTING:
        try:
            perf_tracker = PerformanceTracker(
                tracker_path=str(Path(__file__).parent / "signal_performance.json"))
            perf_tracker.load()
            risk_mgr = RiskManager()
        except Exception:
            pass

    # Resolve any pending bets from previous days
    if state.pending:
        num_pending_before = len(state.pending)
        log(f"Resolving {num_pending_before} pending bet(s)...")
        resolve_pending_bets(state)
        # Record resolved trades in performance tracker
        if perf_tracker and state.trades:
            for t in state.trades[-(num_pending_before + 5):]:
                if t.get("resolved_at") and "recorded" not in t:
                    try:
                        perf_tracker.record_trade(
                            t.get("ticker", ""), t.get("signals", {}),
                            t.get("direction", ""), t.get("won", False),
                            t.get("pnl", 0))
                        t["recorded"] = True
                    except Exception:
                        pass
            perf_tracker.save()
        save_state(state)

    # Reset daily counter on new day
    if state.last_date != today:
        state.daily_bets = 0
        state.last_date = today

    # Discover active markets
    log("Discovering stock Up/Down markets on Polymarket...")
    markets = find_stock_markets()
    if not markets:
        log("No active stock Up/Down markets found. Markets may not be open yet.")
        return

    modules_active = []
    if ML_AVAILABLE: modules_active.append("ML")
    if SMART_BETTING: modules_active.append("SmartBet")
    if ENHANCED_DATA: modules_active.append("EnhData")
    if SENTIMENT_AVAILABLE: modules_active.append("Sentiment")
    log(f"Found {len(markets)} active stock markets | Modules: {', '.join(modules_active) or 'basic'}")

    # Fetch SPY data once for correlation analysis
    spy_data = fetch_stock_data("SPY")

    # Analyze each stock
    bets_placed = 0
    bets_skipped_reasons = {}
    for ticker, market_info in sorted(markets.items()):
        if ticker in state.pending:
            log(f"  {ticker.upper()}: already have pending bet, skipping")
            continue

        # Check if performance tracker says to skip this ticker
        if perf_tracker and perf_tracker.should_skip_ticker(ticker):
            log(f"  {ticker.upper()}: skipped (poor historical performance)")
            continue

        stock_info = STOCKS.get(ticker, {})
        ticker_yf = stock_info.get("yf", ticker.upper())

        prediction, confidence, signals = predict_direction(ticker, ticker_yf, spy_data)

        # Add enhanced data signals
        if ENHANCED_DATA:
            try:
                enh = build_enhanced_features(ticker, ticker_yf)
                for k, v in enh.items():
                    signals[k] = v
            except Exception:
                pass

        # Add sentiment signals
        if SENTIMENT_AVAILABLE:
            try:
                sent = build_sentiment_features(ticker, ticker_yf)
                for k, v in sent.items():
                    signals[k] = v
            except Exception:
                pass

        # Format signal summary
        key_signals = []
        if "rsi" in signals:
            key_signals.append(f"RSI={signals['rsi']}")
        if "macd_hist" in signals:
            key_signals.append(f"MACD={'↑' if signals['macd_hist'] > 0 else '↓'}")
        if "streak" in signals:
            key_signals.append(f"streak={signals['streak']}")
        if "premarket_pct" in signals:
            key_signals.append(f"pre={signals['premarket_pct']:+.2f}%")
        if "ml_direction" in signals:
            key_signals.append(f"ML={signals['ml_direction'].upper()}@{signals.get('ml_confidence',0):.0%}")
        if "up_probability" in signals:
            key_signals.append(f"P(up)={signals['up_probability']:.1%}")
        if "news_sentiment_score" in signals:
            s = signals['news_sentiment_score']
            if abs(s) > 0.1:
                key_signals.append(f"sent={'+'if s>0 else ''}{s:.2f}")

        sig_str = " | ".join(key_signals)

        if prediction is None:
            log(f"  {ticker.upper()}: insufficient data")
            continue

        log(f"  {ticker.upper()}: predict {prediction.upper()} @ {confidence:.1%} "
            f"(market: Up={market_info['up_price']:.1%} Down={market_info['down_price']:.1%}) "
            f"[{sig_str}]")

        # Evaluate bet using smart betting or fallback
        bet = None
        if SMART_BETTING:
            try:
                daily_pnl = sum(t.get("pnl", 0) for t in state.trades[-20:]
                               if (t.get("resolved_at") or "")[:10] == today)
                pending_list = [
                    {"ticker": t, **v} for t, v in state.pending.items()
                ]
                state_dict = {
                    "balance": state.balance, "total_trades": state.total_trades,
                    "wins": state.wins, "losses": state.losses,
                    "total_pnl": state.total_pnl, "peak_balance": state.peak_balance,
                    "daily_bets": state.daily_bets,
                    "pending_bets": pending_list,
                    "daily_pnl": daily_pnl,
                }
                bet_result = evaluate_bet_enhanced(
                    ticker, prediction, confidence, market_info, state_dict,
                    signals, state.trades[-50:])
                if bet_result and bet_result.get("should_bet"):
                    conc = bet_result.get("concordance", {})
                    conc_score = conc.get("concordance_score", 0) if isinstance(conc, dict) else conc
                    bet = {
                        "ticker": ticker,
                        "direction": prediction,
                        "confidence": round(confidence, 4),
                        "market_prob": market_info.get("market_prob", 0.5),
                        "edge": bet_result.get("edge", 0),
                        "kelly": bet_result.get("kelly_fraction", 0),
                        "bet_size": bet_result.get("bet_size", 0),
                        "concordance": conc_score,
                    }
                    # Check risk manager
                    if risk_mgr:
                        daily_pnl = sum(t.get("pnl", 0) for t in state.trades[-20:]
                                       if t.get("date") == today)
                        pending_list = [
                            {"ticker": t, **v} for t, v in state.pending.items()
                        ]
                        allowed, reason = risk_mgr.can_place_bet(
                            ticker, prediction, bet["bet_size"],
                            state.balance, pending_list, daily_pnl)
                        if not allowed:
                            log(f"    → BLOCKED by risk manager: {reason}")
                            bet = None
                elif bet_result:
                    reason = bet_result.get("reason", "insufficient edge")
                    bets_skipped_reasons[reason] = bets_skipped_reasons.get(reason, 0) + 1
            except Exception:
                bet = evaluate_bet(ticker, prediction, confidence, market_info, state)
        else:
            bet = evaluate_bet(ticker, prediction, confidence, market_info, state)

        if bet:
            concordance_str = f", concordance={bet.get('concordance', 0):.0%}" if 'concordance' in bet else ""
            log(f"    → BET ${bet['bet_size']:.0f} on {bet['direction'].upper()} "
                f"(edge={bet['edge']:+.1%}, kelly={bet.get('kelly', 0):.1%}{concordance_str})")

            # Deduct stake from balance at placement time
            state.balance -= bet["bet_size"]
            state.pending[ticker] = {
                "date": today,
                "direction": bet["direction"],
                "confidence": bet["confidence"],
                "market_prob": bet["market_prob"],
                "edge": bet["edge"],
                "bet_size": bet["bet_size"],
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "signals": {k: v for k, v in signals.items()
                            if k in ["rsi", "macd_hist", "bollinger", "streak",
                                     "zscore", "premarket_pct", "up_probability",
                                     "raw_score", "spy_mom", "ml_direction",
                                     "ml_confidence", "news_sentiment_score",
                                     "pm_premarket_change_pct", "earn_days_to_earnings"]},
            }
            state.daily_bets += 1
            bets_placed += 1

    # Summary
    log("-" * 40)
    log(f"Bets placed this cycle: {bets_placed} | "
        f"Pending: {len(state.pending)} | "
        f"Daily total: {state.daily_bets}")

    if bets_skipped_reasons:
        skip_summary = ", ".join(f"{r}: {c}" for r, c in bets_skipped_reasons.items())
        log(f"Skipped: {skip_summary}")

    if state.total_trades > 0:
        win_rate = state.wins / state.total_trades
        drawdown = (state.peak_balance - state.balance) / state.peak_balance
        log(f"Lifetime: {state.total_trades} trades | "
            f"Win rate: {win_rate:.1%} | "
            f"Drawdown: {drawdown:.1%}")

    # Show performance tracker insights
    if perf_tracker and state.total_trades >= 5:
        try:
            best = perf_tracker.get_best_signals(3)
            if best:
                log(f"Best signals: {', '.join(f'{s[0]}({s[1]:.0%})' for s in best)}")
            worst = perf_tracker.get_worst_tickers(2)
            if worst:
                log(f"Worst tickers: {', '.join(f'{t[0]}({t[1]:.0%})' for t in worst)}")
        except Exception:
            pass

    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Polymarket Stock Up/Down Prediction Bot")
    parser.add_argument("--reset", action="store_true", help="Reset all state")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    args = parser.parse_args()

    if args.reset and TRADE_LOG.exists():
        TRADE_LOG.unlink()
        log("State reset")

    state = load_state()

    log("╔══════════════════════════════════════════════════╗")
    log("║   Polymarket Stock Up/Down Prediction Bot        ║")
    log("║   Paper Trading Mode                             ║")
    log("╚══════════════════════════════════════════════════╝")

    if args.once:
        run_cycle(state)
        return

    # Main loop: run at market-relevant times
    while True:
        try:
            now_et = datetime.now(ZoneInfo("America/New_York"))
            hour = now_et.hour
            weekday = now_et.weekday()  # 0=Monday, 6=Sunday

            # Only run during market-relevant hours (6 AM - 5 PM ET, weekdays)
            if weekday < 5 and 6 <= hour <= 17:
                run_cycle(state)
                # Run every 2 hours during market hours
                sleep_seconds = 7200
            elif weekday < 5 and hour < 6:
                # Before market: sleep until 6 AM ET
                wake_at = now_et.replace(hour=6, minute=0, second=0)
                sleep_seconds = (wake_at - now_et).total_seconds()
                log(f"Pre-market. Sleeping until 6 AM ET ({sleep_seconds/3600:.1f}h)")
            else:
                # After hours or weekend
                if weekday >= 5:
                    days_until_monday = 7 - weekday
                    wake_at = (now_et + timedelta(days=days_until_monday)).replace(
                        hour=6, minute=0, second=0)
                else:
                    wake_at = (now_et + timedelta(days=1)).replace(
                        hour=6, minute=0, second=0)
                sleep_seconds = (wake_at - now_et).total_seconds()
                log(f"Market closed. Sleeping until {wake_at.strftime('%A %H:%M ET')} "
                    f"({sleep_seconds/3600:.1f}h)")

            time.sleep(max(sleep_seconds, 60))

        except KeyboardInterrupt:
            log("Bot stopped by user")
            save_state(state)
            break
        except Exception as ex:
            log(f"Error in main loop: {ex}")
            time.sleep(300)


if __name__ == "__main__":
    main()
