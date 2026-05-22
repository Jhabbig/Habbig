"""DCA (dollar-cost-average) backtest simulator.

Replays a "buy $X every N days" strategy against a coin's historical
klines (Binance) and computes:
  - total USD invested
  - total tokens accumulated
  - current value (using last close)
  - profit/loss + ROI %
  - average buy price (cost basis)
  - max drawdown vs DCA cost basis

Pure math + relies on the existing /api/binance/klines endpoint for the
price series. No new upstream calls.
"""
from __future__ import annotations

from typing import Optional


def simulate(klines: list[dict], buy_usd: float = 100.0,
             every_n_days: int = 7, lookback_days: int = 365) -> dict:
    """Run a DCA backtest over the provided kline series.

    Args:
      klines: list of {open_ms, open, high, low, close, volume} - we use
              close. Assumes daily-ish bars; for 1d klines, every_n_days
              maps directly to the buy cadence.
      buy_usd: USD amount purchased each cadence step
      every_n_days: cadence in days (e.g. 7 = weekly)
      lookback_days: trim klines to the last N days

    Returns:
      Dict with totals + cost basis + per-buy breakdown.
    """
    if not klines or buy_usd <= 0 or every_n_days <= 0:
        return {"error": "missing klines or invalid params"}
    bars = sorted(klines, key=lambda b: b.get("open_ms", 0))
    if lookback_days > 0:
        bars = bars[-lookback_days:]
    if not bars:
        return {"error": "no bars in window"}
    buys = []
    total_usd = 0.0
    total_tokens = 0.0
    for i in range(0, len(bars), every_n_days):
        bar = bars[i]
        price = bar.get("close")
        if not price or price <= 0:
            continue
        tokens = buy_usd / price
        total_usd += buy_usd
        total_tokens += tokens
        buys.append({
            "open_ms": bar["open_ms"],
            "price": round(price, 6),
            "tokens": round(tokens, 8),
            "running_tokens": round(total_tokens, 8),
            "running_usd_invested": round(total_usd, 2),
            "running_cost_basis": round(total_usd / total_tokens, 6) if total_tokens else None,
        })
    if not buys:
        return {"error": "no valid buys"}
    last_close = bars[-1].get("close") or 0
    current_value = total_tokens * last_close
    cost_basis = total_usd / total_tokens
    pnl_usd = current_value - total_usd
    roi_pct = (current_value / total_usd - 1) * 100 if total_usd else 0
    # Max drawdown vs cost basis over the buy timeline
    max_dd = 0.0
    for b in buys:
        value_at_buy = b["running_tokens"] * b["price"]
        invested = b["running_usd_invested"]
        if invested > 0:
            dd = (value_at_buy - invested) / invested
            if dd < max_dd:
                max_dd = dd
    return {
        "buy_usd_per_step": buy_usd,
        "cadence_days": every_n_days,
        "lookback_days": lookback_days,
        "steps": len(buys),
        "total_usd_invested": round(total_usd, 2),
        "total_tokens": round(total_tokens, 8),
        "last_close": round(last_close, 6),
        "current_value_usd": round(current_value, 2),
        "average_cost_basis": round(cost_basis, 6),
        "pnl_usd": round(pnl_usd, 2),
        "roi_pct": round(roi_pct, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "buys": buys,
    }


def _sma(values: list[float], period: int) -> list:
    out = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def _rsi(values: list[float], period: int = 14) -> list:
    out = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g = gains / period
    avg_l = losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0)
        loss = max(-d, 0)
        avg_g = (avg_g * (period - 1) + gain) / period
        avg_l = (avg_l * (period - 1) + loss) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def sma_crossover(
    klines: list[dict], *, fast: int = 50, slow: int = 200,
    starting_usd: float = 10_000, fee_pct: float = 0.1,
) -> dict:
    """Buy when fast SMA crosses ABOVE slow SMA; sell on the reverse cross.
    All-in / all-out. Returns realised PnL + trade log."""
    if not klines or len(klines) < slow + 2:
        return {"error": "not enough bars"}
    closes = [b["close"] for b in klines]
    fast_s = _sma(closes, fast)
    slow_s = _sma(closes, slow)
    cash = starting_usd
    tokens = 0.0
    trades: list[dict] = []
    in_position = False
    for i in range(slow + 1, len(closes)):
        if fast_s[i] is None or slow_s[i] is None or fast_s[i - 1] is None or slow_s[i - 1] is None:
            continue
        prev_diff = fast_s[i - 1] - slow_s[i - 1]
        cur_diff = fast_s[i] - slow_s[i]
        price = closes[i]
        if not in_position and prev_diff <= 0 < cur_diff:
            tokens = (cash * (1 - fee_pct / 100)) / price
            trades.append({"side": "BUY", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "cash_before": cash})
            cash = 0.0
            in_position = True
        elif in_position and prev_diff >= 0 > cur_diff:
            cash = tokens * price * (1 - fee_pct / 100)
            trades.append({"side": "SELL", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "cash_after": cash})
            tokens = 0.0
            in_position = False
    final_value = cash + tokens * closes[-1] if tokens else cash
    return _summarize("SMA crossover", final_value, starting_usd, trades, klines, fast=fast, slow=slow)


def rsi_mean_reversion(
    klines: list[dict], *, period: int = 14, oversold: float = 30, overbought: float = 70,
    starting_usd: float = 10_000, fee_pct: float = 0.1,
) -> dict:
    """Buy when RSI crosses up through `oversold`; sell when RSI crosses
    down through `overbought`. Single position at a time."""
    if not klines or len(klines) < period + 2:
        return {"error": "not enough bars"}
    closes = [b["close"] for b in klines]
    rsi_vals = _rsi(closes, period)
    cash = starting_usd
    tokens = 0.0
    trades: list[dict] = []
    in_position = False
    for i in range(period + 1, len(closes)):
        prev = rsi_vals[i - 1]
        cur  = rsi_vals[i]
        if prev is None or cur is None:
            continue
        price = closes[i]
        if not in_position and prev <= oversold < cur:
            tokens = (cash * (1 - fee_pct / 100)) / price
            trades.append({"side": "BUY", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "rsi": cur, "cash_before": cash})
            cash = 0.0
            in_position = True
        elif in_position and prev >= overbought > cur:
            cash = tokens * price * (1 - fee_pct / 100)
            trades.append({"side": "SELL", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "rsi": cur, "cash_after": cash})
            tokens = 0.0
            in_position = False
    final_value = cash + tokens * closes[-1] if tokens else cash
    return _summarize("RSI mean-reversion", final_value, starting_usd, trades, klines,
                       period=period, oversold=oversold, overbought=overbought)


def breakout(
    klines: list[dict], *, lookback: int = 20,
    starting_usd: float = 10_000, fee_pct: float = 0.1,
) -> dict:
    """Buy at new N-bar high; sell at new N-bar low."""
    if not klines or len(klines) < lookback + 2:
        return {"error": "not enough bars"}
    cash = starting_usd
    tokens = 0.0
    trades: list[dict] = []
    in_position = False
    for i in range(lookback, len(klines)):
        window = klines[i - lookback:i]
        hi = max(b["high"] for b in window)
        lo = min(b["low"] for b in window)
        price = klines[i]["close"]
        if not in_position and price > hi * 1.0001:
            tokens = (cash * (1 - fee_pct / 100)) / price
            trades.append({"side": "BUY", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "cash_before": cash})
            cash = 0.0
            in_position = True
        elif in_position and price < lo * 0.9999:
            cash = tokens * price * (1 - fee_pct / 100)
            trades.append({"side": "SELL", "ts": klines[i]["open_ms"], "price": price, "tokens": tokens, "cash_after": cash})
            tokens = 0.0
            in_position = False
    final_value = cash + tokens * klines[-1]["close"] if tokens else cash
    return _summarize("Breakout", final_value, starting_usd, trades, klines, lookback=lookback)


def _summarize(strategy_name: str, final_value: float, starting_usd: float,
                trades: list[dict], klines: list[dict], **params) -> dict:
    pnl = final_value - starting_usd
    roi = (final_value / starting_usd - 1) * 100 if starting_usd else 0
    # Round-trip win-rate
    pairs = []
    for i in range(0, len(trades) - 1, 2):
        if trades[i]["side"] == "BUY" and i + 1 < len(trades) and trades[i + 1]["side"] == "SELL":
            entry = trades[i]["price"]
            exit_ = trades[i + 1]["price"]
            pairs.append({"entry": entry, "exit": exit_, "pnl_pct": (exit_ / entry - 1) * 100})
    wins = [p for p in pairs if p["pnl_pct"] > 0]
    losses = [p for p in pairs if p["pnl_pct"] <= 0]
    win_rate = (len(wins) / len(pairs) * 100) if pairs else 0
    avg_win = (sum(p["pnl_pct"] for p in wins) / len(wins)) if wins else 0
    avg_loss = (sum(p["pnl_pct"] for p in losses) / len(losses)) if losses else 0
    # Buy-and-hold comparison
    bh_value = starting_usd * (klines[-1]["close"] / klines[0]["close"])
    bh_roi = (bh_value / starting_usd - 1) * 100
    return {
        "strategy": strategy_name,
        "params": params,
        "starting_usd": starting_usd,
        "final_value_usd": round(final_value, 2),
        "pnl_usd": round(pnl, 2),
        "roi_pct": round(roi, 2),
        "buy_hold_roi_pct": round(bh_roi, 2),
        "alpha_pct": round(roi - bh_roi, 2),
        "trade_count": len(trades),
        "round_trip_count": len(pairs),
        "win_rate_pct": round(win_rate, 1),
        "avg_winner_pct": round(avg_win, 2),
        "avg_loser_pct": round(avg_loss, 2),
        "trades_tail": trades[-12:],
    }


if __name__ == "__main__":
    import json
    klines = [{"open_ms": i, "open": 100, "high": 100, "low": 100, "close": 100 + i * 0.5, "volume": 1}
              for i in range(0, 365)]
    out = simulate(klines, buy_usd=100, every_n_days=7, lookback_days=365)
    print(json.dumps({k: v for k, v in out.items() if k != "buys"}, indent=2))
