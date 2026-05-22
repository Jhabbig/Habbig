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


if __name__ == "__main__":
    # Synthetic test - upward trend
    klines = [{"open_ms": i, "close": 100 + i * 0.5} for i in range(0, 365)]
    out = simulate(klines, buy_usd=100, every_n_days=7, lookback_days=365)
    import json
    print(json.dumps({k: v for k, v in out.items() if k != "buys"}, indent=2))
