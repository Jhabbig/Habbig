#!/usr/bin/env python3
"""
Resolved Market Analysis — Retroactive Insider Detection.

The strongest insider signal we can compute: wallets that *repeatedly* win
long-shot bets on markets that have already resolved.

Pipeline:
  1. Fetch recently closed markets from gamma-api.
  2. For each closed market, identify the winning outcome (price ≈ 1.0).
  3. Pull all trades on that market.
  4. Find trades that bought the winning outcome at a low price (≤25%).
  5. Group winners by wallet to identify repeat winners — those are the
     near-ground-truth insider candidates.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RATE_PAUSE = 0.06

# A "long-shot win" is a buy at ≤25% that resolved YES.
LONGSHOT_PRICE_MAX = 0.25
# Minimum buy price ignored — penny dust trades are noise.
LONGSHOT_PRICE_MIN = 0.005
# Minimum trade USD to count as a serious bet.
LONGSHOT_MIN_USD = 200
# How many days of resolved markets to scan.
RESOLVED_LOOKBACK_DAYS = 60


def _api_get(url: str, params: dict | None = None, retries: int = 3) -> Any:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(1)
    return None


def _parse_ts(raw) -> int:
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            pass
    return 0


def _parse_outcome_prices(raw) -> list[float]:
    """outcomePrices comes back as a JSON-encoded string sometimes."""
    if isinstance(raw, list):
        return [float(x) for x in raw if x is not None]
    if isinstance(raw, str):
        try:
            import json
            parsed = json.loads(raw)
            return [float(x) for x in parsed if x is not None]
        except (ValueError, TypeError):
            return []
    return []


def _parse_outcomes(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            import json
            return [str(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return []
    return []


def fetch_resolved_markets(days: int = RESOLVED_LOOKBACK_DAYS, limit: int = 500) -> list[dict]:
    """Fetch recently resolved markets from gamma API.

    Returns a list of normalized markets with the winning outcome already
    identified.
    """
    print(f"  Fetching resolved markets (last {days}d)...")
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    all_markets = []
    offset = 0
    page_size = 100
    while offset < limit:
        data = _api_get(f"{GAMMA_API}/markets", {
            "closed": "true",
            "limit": min(page_size, limit - offset),
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
        })
        if not data:
            break
        if not isinstance(data, list):
            break

        kept_any = False
        for m in data:
            end_ts = _parse_ts(m.get("endDate", ""))
            if end_ts > 0 and end_ts < cutoff_ts:
                continue  # too old

            prices = _parse_outcome_prices(m.get("outcomePrices"))
            outcomes = _parse_outcomes(m.get("outcomes"))
            if not prices or not outcomes:
                continue

            # Find the winning outcome (price closest to 1.0)
            max_price = max(prices)
            if max_price < 0.95:
                continue  # void or unresolved
            winner_idx = prices.index(max_price)
            winning_outcome = outcomes[winner_idx] if winner_idx < len(outcomes) else None
            if not winning_outcome:
                continue

            cid = m.get("conditionId") or m.get("condition_id", "")
            if not cid:
                continue

            all_markets.append({
                "condition_id": cid,
                "question": m.get("question", "Unknown"),
                "slug": m.get("slug", ""),
                "outcomes": outcomes,
                "outcome_prices": prices,
                "winning_outcome": winning_outcome,
                "winning_price": max_price,
                "end_date": m.get("endDate", ""),
                "end_ts": end_ts,
                "volume": float(m.get("volume", 0) or 0),
            })
            kept_any = True

        offset += len(data)
        if len(data) < page_size or not kept_any:
            break
        time.sleep(RATE_PAUSE)

    print(f"    Found {len(all_markets)} resolved markets in window.")
    return all_markets


def fetch_market_trades(condition_id: str, max_trades: int = 2000) -> list[dict]:
    """Fetch trade history for a single market."""
    trades = []
    offset = 0
    page = 500
    while len(trades) < max_trades:
        data = _api_get(f"{DATA_API}/trades", {
            "market": condition_id,
            "limit": min(page, max_trades - len(trades)),
            "offset": offset,
        })
        if not data or not isinstance(data, list):
            break
        trades.extend(data)
        offset += len(data)
        if len(data) < page:
            break
        time.sleep(RATE_PAUSE)
    return trades


def find_longshot_winners(markets: list[dict], max_markets: int | None = None) -> list[dict]:
    """For each resolved market, find trades that bought the winning outcome
    at long odds. These are confirmed insider candidates."""
    winners = []
    scan_set = markets[:max_markets] if max_markets else markets
    print(f"  Analyzing {len(scan_set)} resolved markets for long-shot winners...")

    for i, m in enumerate(scan_set):
        if i % 10 == 0:
            print(f"    [{i}/{len(scan_set)}] {m['question'][:60]}")

        trades = fetch_market_trades(m["condition_id"])
        if not trades:
            continue

        winning_outcome = m["winning_outcome"]
        end_ts = m["end_ts"]

        for t in trades:
            outcome = t.get("outcome", "")
            if outcome != winning_outcome:
                continue
            side = (t.get("side") or "").upper()
            if side and side != "BUY":
                continue

            size = float(t.get("size", 0) or 0)
            price = float(t.get("price", 0) or 0)
            if price < LONGSHOT_PRICE_MIN or price > LONGSHOT_PRICE_MAX:
                continue
            usd = size * price if price > 0 else size
            if usd < LONGSHOT_MIN_USD:
                continue

            ts = _parse_ts(t.get("timestamp", 0))
            # Skip trades placed AFTER market closed (data quirks)
            if end_ts and ts > end_ts:
                continue

            # Realized profit = shares * (1 - buy_price), where shares = size/price
            shares = size / price if price > 0 else 0
            realized = shares * (1.0 - price)
            hours_before_close = (end_ts - ts) / 3600 if end_ts and ts else None

            winners.append({
                "wallet": t.get("proxyWallet", t.get("maker_address", "")),
                "name": t.get("name", ""),
                "pseudonym": t.get("pseudonym", ""),
                "market_id": m["condition_id"],
                "market_question": m["question"],
                "outcome": outcome,
                "buy_price": price,
                "size_usd": usd,
                "shares": shares,
                "realized_profit": realized,
                "trade_ts": ts,
                "trade_time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "",
                "end_ts": end_ts,
                "hours_before_close": hours_before_close,
            })
        time.sleep(RATE_PAUSE)

    print(f"    Total long-shot wins found: {len(winners)}")
    return winners


def aggregate_repeat_winners(winners: list[dict]) -> list[dict]:
    """Group winners by wallet and rank by repeat behavior.

    A wallet that wins ONE long-shot might be lucky.
    A wallet that wins MULTIPLE long-shots across DIFFERENT markets is suspicious.
    """
    by_wallet = defaultdict(list)
    for w in winners:
        if w["wallet"]:
            by_wallet[w["wallet"]].append(w)

    profiles = []
    for wallet, wins in by_wallet.items():
        if not wins:
            continue
        unique_markets = {w["market_id"] for w in wins}
        total_realized = sum(w["realized_profit"] for w in wins)
        total_staked = sum(w["size_usd"] for w in wins)
        avg_buy_price = sum(w["buy_price"] for w in wins) / len(wins)

        # Late bets (placed within 24h of close) are extra suspicious
        late_bets = sum(
            1 for w in wins
            if w["hours_before_close"] is not None and 0 < w["hours_before_close"] <= 24
        )

        # Insider score for this profile
        score = 0
        reasons = []

        if len(unique_markets) >= 5:
            score += 50
            reasons.append(f"Won {len(unique_markets)} long-shot markets")
        elif len(unique_markets) >= 3:
            score += 30
            reasons.append(f"Won {len(unique_markets)} long-shot markets")
        elif len(unique_markets) >= 2:
            score += 15
            reasons.append(f"Won {len(unique_markets)} long-shot markets")

        if avg_buy_price <= 0.05:
            score += 25
            reasons.append(f"Average buy price {avg_buy_price:.1%} (extreme long-shots)")
        elif avg_buy_price <= 0.10:
            score += 15
            reasons.append(f"Average buy price {avg_buy_price:.1%}")
        elif avg_buy_price <= 0.15:
            score += 8
            reasons.append(f"Average buy price {avg_buy_price:.1%}")

        if total_realized >= 100000:
            score += 30
            reasons.append(f"Realized ${total_realized:,.0f} on long-shots")
        elif total_realized >= 25000:
            score += 18
            reasons.append(f"Realized ${total_realized:,.0f} on long-shots")
        elif total_realized >= 5000:
            score += 8
            reasons.append(f"Realized ${total_realized:,.0f} on long-shots")

        if late_bets >= 3:
            score += 20
            reasons.append(f"{late_bets} bets placed in final 24h before resolution")
        elif late_bets >= 1:
            score += 8
            reasons.append(f"{late_bets} bet(s) placed in final 24h before resolution")

        profiles.append({
            "wallet": wallet,
            "name": wins[0].get("name", ""),
            "pseudonym": wins[0].get("pseudonym", ""),
            "win_count": len(wins),
            "unique_markets_won": len(unique_markets),
            "total_staked": round(total_staked, 2),
            "total_realized_profit": round(total_realized, 2),
            "avg_buy_price": round(avg_buy_price, 4),
            "late_bet_count": late_bets,
            "insider_score": score,
            "reasons": reasons,
            "wins": wins[:20],  # cap to avoid bloating output
        })

    profiles.sort(key=lambda p: (p["insider_score"], p["total_realized_profit"]), reverse=True)
    return profiles


def run_retroactive_scan(days: int = RESOLVED_LOOKBACK_DAYS, max_markets: int = 100) -> dict:
    """Full retroactive insider scan. Returns dict for dashboard."""
    print("=" * 60)
    print("  Retroactive Insider Scan")
    print("=" * 60)

    markets = fetch_resolved_markets(days=days, limit=max_markets * 2)
    if not markets:
        return {"profiles": [], "total_winners": 0, "markets_scanned": 0}

    winners = find_longshot_winners(markets, max_markets=max_markets)
    profiles = aggregate_repeat_winners(winners)

    return {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "markets_scanned": min(len(markets), max_markets),
        "total_winners": len(winners),
        "unique_winning_wallets": len(profiles),
        "profiles": profiles[:50],  # top 50 most suspicious
    }


if __name__ == "__main__":
    result = run_retroactive_scan(days=30, max_markets=50)
    print(f"\n  Top 10 retroactive insider candidates:")
    for i, p in enumerate(result["profiles"][:10]):
        print(f"    {i+1}. [{p['insider_score']}] {p['wallet'][:12]}... ({p.get('pseudonym') or p.get('name') or 'anon'})")
        print(f"       Won {p['win_count']} bets across {p['unique_markets_won']} markets")
        print(f"       Avg odds: {p['avg_buy_price']:.1%} | Realized: ${p['total_realized_profit']:,.0f}")
        for r in p["reasons"][:3]:
            print(f"       - {r}")
