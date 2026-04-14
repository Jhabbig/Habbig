#!/usr/bin/env python3
"""
Polymarket Suspicious / Insider Trades Scanner

Detects potential insider trading on Polymarket using multiple signals:
  1. Potential profit (large payout at long odds)
  2. Timing before market resolution (bets placed right before close)
  3. Volume spikes in normally quiet markets
  4. First-trade wallets (brand new wallet makes a large directional bet)
  5. Coordinated wallets (multiple wallets betting same direction in a short window)
  6. Statistical outliers (z-score vs market baseline)
  7. New account + long-shot combo patterns
"""

import requests
import time
import json
import math
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import numpy as np

# ─── Config ───────────────────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CACHE_DIR = Path(__file__).parent / "cache"
RATE_PAUSE = 0.06  # 200 req/10s limit on trades endpoint

# Thresholds — focused on potential profit, not just trade size
MIN_POTENTIAL_PROFIT = 1000     # flag trades with $1K+ potential profit
MIN_TRADE_USD = 500             # absolute minimum trade size to even consider
ZSCORE_THRESHOLD = 3.0          # standard deviations above market mean
MIN_TRADES_FOR_STATS = 20      # need this many trades to compute stats
SCAN_HOURS = 72                # how far back to scan
MAX_TRADES_PER_FETCH = 1000    # per API call

# Insider-specific thresholds
CLOSE_TO_RESOLUTION_HOURS = 48  # bets placed within 48h of market close
VOLUME_SPIKE_ZSCORE = 2.5       # hourly volume spike threshold
COORDINATION_MIN_WALLETS = 3    # minimum wallets to flag as coordinated


# ═══════════════════════════════════════════════════════════════════════
# API HELPERS
# ═══════════════════════════════════════════════════════════════════════

def api_get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"  API error: {e}")
                return None
            time.sleep(1)
    return None


def price_to_odds_str(price: float) -> str:
    """Convert a probability price to human-readable odds string.
    e.g. 0.05 -> '20:1', 0.50 -> '2:1', 0.90 -> '1.1:1'
    """
    if price <= 0 or price > 1:
        return "N/A"
    if price == 1.0:
        return "1:1"
    odds_against = (1 - price) / price
    if odds_against >= 10:
        return f"{odds_against:.0f}:1"
    elif odds_against >= 2:
        return f"{odds_against:.1f}:1"
    else:
        return f"{odds_against:.1f}:1"


def calc_potential_profit(size: float, price: float) -> float:
    """Calculate potential profit if the bet wins.
    size = amount wagered in USD
    price = probability (0-1), e.g. 0.05 = 5% odds
    Payout = size / price (you get shares worth $1 each at price)
    Profit = payout - cost = size * (1/price - 1)
    """
    if price <= 0 or price > 1:
        return 0
    if price >= 1.0:
        return 0  # no profit at certainty
    return size * (1.0 / price - 1.0)


# ═══════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════════════

def get_active_markets(limit=200):
    """Fetch active Polymarket events sorted by volume."""
    print("  Fetching active markets...")
    events = api_get(f"{GAMMA_API}/events", {
        "active": "true", "closed": "false",
        "limit": limit
    })
    if not events:
        return []

    markets = []
    for event in events:
        for market in event.get("markets", [event]):
            markets.append({
                "condition_id": market.get("conditionId") or market.get("condition_id", ""),
                "question": market.get("question", event.get("title", "Unknown")),
                "slug": market.get("slug", event.get("slug", "")),
                "volume_24h": float(market.get("volume24hr", 0) or 0),
                "volume_total": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "outcomes": market.get("outcomes", ""),
                "end_date": market.get("endDate", ""),
            })
    print(f"    Found {len(markets)} active markets.")
    return markets


# ═══════════════════════════════════════════════════════════════════════
# TRADE SCANNING
# ═══════════════════════════════════════════════════════════════════════

def fetch_recent_trades(hours=SCAN_HOURS, max_total=50000):
    """Fetch all recent trades across all markets using cursor-based pagination."""
    print(f"  Fetching trades from last {hours}h...")
    all_trades = []
    offset = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ts = int(cutoff.timestamp())
    max_offset = 10000  # API hard limit

    while len(all_trades) < max_total and offset < max_offset:
        data = api_get(f"{DATA_API}/trades", {
            "limit": min(MAX_TRADES_PER_FETCH, max_total - len(all_trades)),
            "offset": offset,
        })
        if not data or len(data) == 0:
            break

        batch_added = 0
        hit_cutoff = False
        for t in data:
            ts = _parse_ts(t.get("timestamp", 0))
            if ts > 0 and ts < cutoff_ts:
                hit_cutoff = True
                break
            all_trades.append(t)
            batch_added += 1

        offset += len(data)
        if len(all_trades) % 5000 < MAX_TRADES_PER_FETCH:
            print(f"    {len(all_trades):,} trades fetched...")
        time.sleep(RATE_PAUSE)

        if hit_cutoff or batch_added == 0:
            break

    print(f"    Total: {len(all_trades):,} trades.")
    return all_trades


def _parse_ts(ts_raw) -> int:
    """Parse a timestamp from trade data into unix seconds."""
    if isinstance(ts_raw, (int, float)) and ts_raw > 0:
        return int(ts_raw)
    if isinstance(ts_raw, str):
        try:
            return int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, AttributeError):
            pass
    return 0


def compute_market_stats(trades):
    """Compute per-market trade statistics + insider-detection metadata."""
    market_trades = defaultdict(list)
    for t in trades:
        key = t.get("conditionId") or t.get("slug", "unknown")
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        usd_value = size * price if price > 0 else size
        potential_profit = calc_potential_profit(usd_value, price)
        ts = _parse_ts(t.get("timestamp", 0))
        market_trades[key].append({
            "size": size,
            "usd_value": usd_value,
            "price": price,
            "potential_profit": potential_profit,
            "timestamp": ts,
            "wallet": t.get("proxyWallet", t.get("maker_address", "")),
            "side": t.get("side", ""),
            "outcome": t.get("outcome", ""),
            "trade": t,
        })

    now_ts = int(datetime.now(timezone.utc).timestamp())
    stats = {}
    for market_id, mtrades in market_trades.items():
        sizes = [t["usd_value"] for t in mtrades]
        profits = [t["potential_profit"] for t in mtrades]
        timestamps = [t["timestamp"] for t in mtrades if t["timestamp"] > 0]

        if len(sizes) < MIN_TRADES_FOR_STATS:
            continue

        # Volume-per-hour buckets for spike detection (last 72h)
        hourly_volume = defaultdict(float)
        for t in mtrades:
            if t["timestamp"] > 0:
                hour_bucket = t["timestamp"] // 3600
                hourly_volume[hour_bucket] += t["usd_value"]
        hourly_vols = list(hourly_volume.values()) if hourly_volume else [0]
        vol_mean = np.mean(hourly_vols)
        vol_std = np.std(hourly_vols) if len(hourly_vols) > 1 else 0

        # Recent volume (last 6h) vs baseline
        cutoff_6h = now_ts - 6 * 3600
        recent_vol = sum(t["usd_value"] for t in mtrades if t["timestamp"] >= cutoff_6h)
        recent_count = sum(1 for t in mtrades if t["timestamp"] >= cutoff_6h)

        # Wallet clustering: wallets trading same direction in last 6h
        recent_directional = defaultdict(lambda: defaultdict(set))
        for t in mtrades:
            if t["timestamp"] >= cutoff_6h and t["wallet"]:
                direction = f'{t["outcome"]}_{t["side"]}'
                recent_directional[direction]["wallets"].add(t["wallet"])

        max_coordinated = 0
        for direction, info in recent_directional.items():
            max_coordinated = max(max_coordinated, len(info["wallets"]))

        stats[market_id] = {
            "count": len(sizes),
            "mean": np.mean(sizes),
            "median": np.median(sizes),
            "std": np.std(sizes),
            "p95": np.percentile(sizes, 95),
            "p99": np.percentile(sizes, 99),
            "max": np.max(sizes),
            "mean_profit": np.mean(profits),
            "max_profit": np.max(profits),
            "total_volume": sum(sizes),
            "trades": mtrades,
            # Insider-detection fields
            "hourly_vol_mean": vol_mean,
            "hourly_vol_std": vol_std,
            "recent_6h_volume": recent_vol,
            "recent_6h_count": recent_count,
            "max_coordinated_wallets": max_coordinated,
            # O(1) per-hour lookup so the flagging loop doesn't have to
            # re-iterate every market's trade list per scored trade
            # (was O(N²) for markets with thousands of trades).
            "hourly_volume": dict(hourly_volume),
        }
    return stats


def _build_wallet_trade_counts(trades):
    """Count total trades per wallet to identify first-time / low-activity wallets."""
    counts = defaultdict(int)
    for t in trades:
        w = t.get("proxyWallet", t.get("maker_address", ""))
        if w:
            counts[w] += 1
    return counts


def _build_market_end_dates(markets):
    """Map conditionId → end date timestamp for resolution-proximity detection."""
    end_dates = {}
    for m in markets:
        cid = m.get("condition_id", "")
        end_str = m.get("end_date", "")
        if cid and end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                end_dates[cid] = int(end_dt.timestamp())
            except (ValueError, TypeError):
                pass
    return end_dates


def find_suspicious_trades(trades, market_stats, markets=None):
    """Flag trades using insider-focused signals: timing, coordination,
    wallet age, volume spikes, and profit potential."""
    suspicious = []

    # Pre-compute indices for insider detection
    wallet_counts = _build_wallet_trade_counts(trades)
    market_end_dates = _build_market_end_dates(markets or [])
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for t in trades:
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        usd_value = size * price if price > 0 else size
        potential_profit = calc_potential_profit(usd_value, price)

        # Skip tiny trades
        if usd_value < MIN_TRADE_USD:
            continue

        # Primary filter: potential profit
        if potential_profit < MIN_POTENTIAL_PROFIT and usd_value < 5000:
            continue

        market_id = t.get("conditionId") or t.get("slug", "unknown")
        title = t.get("title", t.get("slug", "Unknown Market"))
        outcome = t.get("outcome", "?")
        wallet = t.get("proxyWallet", t.get("maker_address", "unknown"))
        ts = _parse_ts(t.get("timestamp", 0))

        # Compute z-score if we have market stats
        zscore = 0
        market_mean = 0
        market_std = 0
        market_volume = 0
        ms = market_stats.get(market_id)
        if ms:
            market_mean = ms["mean"]
            market_std = ms["std"]
            market_volume = ms["total_volume"]
            if market_std > 0:
                zscore = (usd_value - market_mean) / market_std

        # ─── Suspicion scoring ───────────────────────────────────
        score = 0
        reasons = []

        # 1. POTENTIAL PROFIT (the killer metric)
        if potential_profit >= 100000:
            score += 50
            reasons.append(f"Massive potential profit (${potential_profit:,.0f})")
        elif potential_profit >= 50000:
            score += 40
            reasons.append(f"Huge potential profit (${potential_profit:,.0f})")
        elif potential_profit >= 20000:
            score += 30
            reasons.append(f"Large potential profit (${potential_profit:,.0f})")
        elif potential_profit >= 10000:
            score += 20
            reasons.append(f"Significant potential profit (${potential_profit:,.0f})")
        elif potential_profit >= 5000:
            score += 12
            reasons.append(f"Notable potential profit (${potential_profit:,.0f})")
        elif potential_profit >= 1000:
            score += 5
            reasons.append(f"Potential profit ${potential_profit:,.0f}")

        # 2. ODDS CONTEXT — all odds levels can be suspicious
        odds_str = price_to_odds_str(price)
        if price <= 0.05:
            score += 30
            reasons.append(f"Extreme long-shot ({odds_str} odds, {price:.0%})")
        elif price <= 0.10:
            score += 22
            reasons.append(f"Long-shot bet ({odds_str} odds, {price:.0%})")
        elif price <= 0.15:
            score += 15
            reasons.append(f"Low-probability bet ({odds_str} odds, {price:.0%})")
        elif price <= 0.25:
            score += 8
            reasons.append(f"Underdog bet ({odds_str} odds, {price:.0%})")
        elif price <= 0.40:
            score += 4
            reasons.append(f"Below-even bet ({odds_str} odds, {price:.0%})")
        elif price <= 0.60:
            score += 2
            reasons.append(f"Medium-odds bet ({odds_str} odds, {price:.0%})")

        # 3. TRADE SIZE (still matters but secondary to profit)
        if usd_value >= 50000:
            score += 20
            reasons.append(f"Very large trade (${usd_value:,.0f})")
        elif usd_value >= 20000:
            score += 12
            reasons.append(f"Large trade (${usd_value:,.0f})")
        elif usd_value >= 10000:
            score += 8
            reasons.append(f"Notable trade size (${usd_value:,.0f})")
        elif usd_value >= 5000:
            score += 4
            reasons.append(f"Above-average trade (${usd_value:,.0f})")

        # 4. STATISTICAL OUTLIER
        if zscore >= 6:
            score += 25
            reasons.append(f"Extreme outlier ({zscore:.1f}σ above market avg)")
        elif zscore >= ZSCORE_THRESHOLD:
            score += 15
            reasons.append(f"Statistical outlier ({zscore:.1f}σ above market avg)")

        # 5. COMBO BONUS — outsized bets at any odds level
        if price <= 0.15 and usd_value >= 5000:
            score += 15
            reasons.append("Big bet on a long-shot — classic insider pattern")
        elif price <= 0.25 and usd_value >= 10000:
            score += 10
            reasons.append("Large bet on underdog")
        elif price <= 0.50 and usd_value >= 25000:
            score += 7
            reasons.append("Heavy bet at medium odds — notable")
        elif usd_value >= 50000:
            score += 5
            reasons.append("Very large position at any odds")

        # ─── INSIDER-SPECIFIC SIGNALS ───────────────────────────

        # 6. TIMING BEFORE RESOLUTION — bets right before market closes
        end_ts = market_end_dates.get(market_id)
        if end_ts and ts > 0:
            hours_to_close = (end_ts - ts) / 3600
            if 0 < hours_to_close <= 6:
                score += 25
                reasons.append(f"Bet placed {hours_to_close:.0f}h before market close")
            elif 0 < hours_to_close <= 24:
                score += 18
                reasons.append(f"Bet placed {hours_to_close:.0f}h before market close")
            elif 0 < hours_to_close <= CLOSE_TO_RESOLUTION_HOURS:
                score += 10
                reasons.append(f"Bet placed {hours_to_close:.0f}h before market close")

        # 7. VOLUME SPIKE — trade landed during an abnormal volume hour
        if ms and ts > 0:
            hour_bucket = ts // 3600
            vol_mean_h = ms.get("hourly_vol_mean", 0)
            vol_std_h = ms.get("hourly_vol_std", 0)
            if vol_std_h > 0 and vol_mean_h > 0:
                # O(1) lookup from the prebuilt hourly_volume map.
                hour_vol = ms.get("hourly_volume", {}).get(hour_bucket, 0)
                vol_zscore = (hour_vol - vol_mean_h) / vol_std_h
                if vol_zscore >= 4:
                    score += 20
                    reasons.append(f"Volume spike ({vol_zscore:.1f}σ above hourly avg)")
                elif vol_zscore >= VOLUME_SPIKE_ZSCORE:
                    score += 12
                    reasons.append(f"Volume spike ({vol_zscore:.1f}σ above hourly avg)")

        # 8. FIRST-TRADE / LOW-ACTIVITY WALLET
        wallet_total = wallet_counts.get(wallet, 0)
        if wallet_total <= 1 and usd_value >= 1000:
            score += 20
            reasons.append("First trade by this wallet — single-purpose account")
        elif wallet_total <= 3 and usd_value >= 2000:
            score += 12
            reasons.append(f"Low-activity wallet (only {wallet_total} trades)")
        elif wallet_total <= 5 and usd_value >= 5000:
            score += 6
            reasons.append(f"Sparse wallet history ({wallet_total} trades)")

        # 9. COORDINATED WALLETS — multiple wallets same direction
        if ms:
            coord = ms.get("max_coordinated_wallets", 0)
            if coord >= 5:
                score += 15
                reasons.append(f"Coordinated activity ({coord} wallets same direction in 6h)")
            elif coord >= COORDINATION_MIN_WALLETS:
                score += 8
                reasons.append(f"Possible coordination ({coord} wallets same direction in 6h)")

        # Only keep if meaningful
        if score < 10:
            continue

        suspicious.append({
            "title": title,
            "outcome": outcome,
            "side": t.get("side", "BUY"),
            "size": size,
            "usd_value": usd_value,
            "price": price,
            "odds_str": odds_str,
            "potential_profit": round(potential_profit, 2),
            "wallet": wallet,
            "timestamp": ts,
            "time_str": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts > 0 else "Unknown",
            "zscore": zscore,
            "market_mean": market_mean,
            "market_std": market_std,
            "market_volume": market_volume,
            "score": score,
            "reasons": reasons,
            "market_id": market_id,
            "name": t.get("name", ""),
            "pseudonym": t.get("pseudonym", ""),
            "tx_hash": t.get("transactionHash", ""),
            "wallet_trade_count": wallet_total,
        })

    # Sort by suspicion score descending, then by potential profit
    suspicious.sort(key=lambda x: (x["score"], x["potential_profit"]), reverse=True)
    return suspicious


# ═══════════════════════════════════════════════════════════════════════
# WALLET INVESTIGATION
# ═══════════════════════════════════════════════════════════════════════

def investigate_wallet(wallet_address):
    """Look up a wallet's profile and trade history on Polymarket."""
    result = {
        "address": wallet_address,
        "name": "",
        "pseudonym": "",
        "profile_image": "",
        "account_age_days": None,
        "account_age_label": "",
        "total_trades": 0,
        "total_volume": 0,
        "large_trades": [],
        "markets_traded": [],
        "avg_trade_size": 0,
        "win_rate": None,
        "first_seen": None,
        "last_seen": None,
        "is_new_account": False,
    }

    # Get profile
    profile = api_get(f"{GAMMA_API}/public-profile", {"address": wallet_address})
    time.sleep(RATE_PAUSE)

    if profile:
        result["name"] = profile.get("name", "")
        result["pseudonym"] = profile.get("pseudonym", "")
        result["profile_image"] = profile.get("profileImage", "")

    # Get trade history
    all_trades = []
    offset = 0
    while True:
        data = api_get(f"{DATA_API}/trades", {
            "user": wallet_address,
            "limit": MAX_TRADES_PER_FETCH,
            "offset": offset,
        })
        time.sleep(RATE_PAUSE)
        if not data or len(data) == 0:
            break
        all_trades.extend(data)
        offset += MAX_TRADES_PER_FETCH
        if len(data) < MAX_TRADES_PER_FETCH:
            break
        if offset > 10000:  # safety cap
            break

    result["total_trades"] = len(all_trades)

    if all_trades:
        timestamps = []
        volumes = []
        for t in all_trades:
            size = float(t.get("size", 0) or 0)
            price = float(t.get("price", 0) or 0)
            usd = size * price if price > 0 else size
            volumes.append(usd)
            result["markets_traded"].append(t.get("title", t.get("slug", "?")))

            ts = _parse_ts(t.get("timestamp", 0))
            if ts > 0:
                timestamps.append(ts)

            if usd >= 1000:  # lower threshold for "large" in wallet context
                result["large_trades"].append({
                    "title": t.get("title", "?"),
                    "outcome": t.get("outcome", "?"),
                    "size": usd,
                    "price": price,
                    "potential_profit": calc_potential_profit(usd, price),
                    "odds_str": price_to_odds_str(price),
                    "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts > 0 else "?",
                })

        result["total_volume"] = sum(volumes)
        result["avg_trade_size"] = np.mean(volumes) if volumes else 0

        if timestamps:
            first_ts = min(timestamps)
            last_ts = max(timestamps)
            result["first_seen"] = datetime.fromtimestamp(first_ts, tz=timezone.utc)
            result["last_seen"] = datetime.fromtimestamp(last_ts, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - result["first_seen"]).days
            result["account_age_days"] = age_days

            # Human-readable age label
            if age_days == 0:
                result["account_age_label"] = "Brand new (today)"
                result["is_new_account"] = True
            elif age_days <= 7:
                result["account_age_label"] = f"{age_days}d old (very new)"
                result["is_new_account"] = True
            elif age_days <= 30:
                result["account_age_label"] = f"{age_days}d old (new)"
                result["is_new_account"] = True
            elif age_days <= 90:
                result["account_age_label"] = f"{age_days}d old"
            elif age_days <= 365:
                result["account_age_label"] = f"{age_days // 30}mo old"
            else:
                result["account_age_label"] = f"{age_days // 365}y {(age_days % 365) // 30}mo old"

    # Distinct field for the count so the original list (if any) stays a list.
    # The previous in-place reassignment silently flipped the field type from
    # list[str] to int — any downstream consumer iterating it would crash with
    # `TypeError: 'int' object is not iterable`.
    unique_markets = set(result.get("markets_traded") or [])
    result["markets_traded"] = sorted(unique_markets)
    result["markets_traded_count"] = len(unique_markets)
    return result


def investigate_top_wallets(suspicious_trades, max_wallets=15):
    """Investigate the wallets behind the most suspicious trades."""
    wallet_scores = defaultdict(lambda: {"score": 0, "trades": [], "total_profit_potential": 0})
    for t in suspicious_trades:
        w = t["wallet"]
        wallet_scores[w]["score"] = max(wallet_scores[w]["score"], t["score"])
        wallet_scores[w]["trades"].append(t)
        wallet_scores[w]["total_profit_potential"] += t["potential_profit"]

    # Sort by score, take top N
    top_wallets = sorted(wallet_scores.items(), key=lambda x: x[1]["score"], reverse=True)[:max_wallets]

    print(f"  Investigating {len(top_wallets)} wallets...")
    investigations = {}
    for i, (addr, info) in enumerate(top_wallets):
        print(f"    [{i+1}/{len(top_wallets)}] {addr[:10]}...")
        inv = investigate_wallet(addr)
        inv["flagged_trades"] = info["trades"]
        inv["max_suspicion_score"] = info["score"]
        inv["total_profit_potential"] = info["total_profit_potential"]

        # BONUS: new account making big bets = extra suspicious
        # Copy trade dicts to avoid mutating the originals in suspicious_trades list
        if inv["is_new_account"]:
            inv["flagged_trades"] = [dict(ft, reasons=list(ft.get("reasons", []))) for ft in inv["flagged_trades"]]
            for ft in inv["flagged_trades"]:
                ft["score"] = min(100, ft["score"] + 15)
                ft["reasons"].append(f"New account ({inv['account_age_label']})")

        investigations[addr] = inv

    return investigations


# ═══════════════════════════════════════════════════════════════════════
# AGGREGATE STATS
# ═══════════════════════════════════════════════════════════════════════

def compute_aggregate_stats(suspicious_trades, wallet_investigations):
    """Compute overview stats about suspicious trading activity."""
    if not suspicious_trades:
        return {}

    sizes = [t["usd_value"] for t in suspicious_trades]
    profits = [t["potential_profit"] for t in suspicious_trades]
    scores = [t["score"] for t in suspicious_trades]

    # Account ages. Wallets where we couldn't determine an age sometimes
    # default to 0 — counting those as "new" inflates the metric and pollutes
    # any insider heuristic that weights by young-wallet count.
    ages = [inv["account_age_days"] for inv in wallet_investigations.values() if inv["account_age_days"] is not None]
    new_accounts = sum(1 for a in ages if 0 < a < 30)

    # Long-shot stats
    longshot_trades = [t for t in suspicious_trades if t["price"] <= 0.15]
    longshot_profit = sum(t["potential_profit"] for t in longshot_trades)

    # Insider-specific stats
    first_trade_wallets = sum(
        1 for t in suspicious_trades if t.get("wallet_trade_count", 99) <= 1
    )
    pre_resolution = sum(
        1 for t in suspicious_trades
        if any("before market close" in r for r in t.get("reasons", []))
    )
    volume_spike_trades = sum(
        1 for t in suspicious_trades
        if any("Volume spike" in r for r in t.get("reasons", []))
    )

    return {
        "total_flagged": len(suspicious_trades),
        "total_volume_flagged": sum(sizes),
        "total_potential_profit": sum(profits),
        "avg_trade_size": np.mean(sizes),
        "median_trade_size": np.median(sizes),
        "max_trade_size": max(sizes),
        "max_potential_profit": max(profits) if profits else 0,
        "avg_suspicion_score": np.mean(scores),
        "unique_wallets": len(set(t["wallet"] for t in suspicious_trades)),
        "unique_markets": len(set(t["title"] for t in suspicious_trades)),
        "avg_account_age": np.mean(ages) if ages else None,
        "new_accounts": new_accounts,
        "total_investigated": len(wallet_investigations),
        "longshot_trades": len(longshot_trades),
        "longshot_total_profit": longshot_profit,
        # Insider-specific
        "first_trade_wallets": first_trade_wallets,
        "pre_resolution_trades": pre_resolution,
        "volume_spike_trades": volume_spike_trades,
    }


# ═══════════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════

def run_scanner(include_retroactive: bool = True, retroactive_days: int = 60, retroactive_max_markets: int = 80):
    """Full scan pipeline. Returns data for dashboard integration."""
    print("=" * 60)
    print("  Polymarket Suspicious Trades Scanner")
    print("=" * 60)

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"suspicious_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}.json"

    # Garbage-collect stale hourly cache files (>24h old). Without this the
    # cache directory grows without bound — 720 files/month, each up to a few
    # MB — eventually filling the disk on long-running deploys.
    try:
        cutoff_ts = time.time() - 24 * 3600
        for old in CACHE_DIR.glob("suspicious_trades_*.json"):
            try:
                if old.stat().st_mtime < cutoff_ts:
                    old.unlink()
            except OSError:
                pass
    except Exception as cleanup_err:
        print(f"  Cache cleanup error (non-fatal): {cleanup_err}")

    # Check cache (valid for 1 hour). A truncated/corrupt cache file used to
    # crash the entire scanner — fall through to a live scan instead.
    if cache_file.exists():
        print("  Loading cached scan results...")
        try:
            with open(cache_file) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as cache_err:
            print(f"  Cache load failed ({cache_err}); refetching live...")
            try:
                cache_file.unlink()
            except OSError:
                pass

    # Step 1: Get markets
    markets = get_active_markets()

    # Step 2: Fetch trades (global + per top market)
    trades = fetch_recent_trades(hours=SCAN_HOURS)

    # Also fetch per-market for top markets to get more coverage
    seen_ids = set()
    for t in trades:
        key = t.get("conditionId", "")
        if key:
            seen_ids.add(key)

    top_markets = [m for m in markets if m["condition_id"] and m["volume_24h"] > 10000][:30]
    for i, m in enumerate(top_markets):
        cid = m["condition_id"]
        if cid in seen_ids:
            continue
        print(f"    Fetching trades for: {m['question'][:50]}...")
        mdata = api_get(f"{DATA_API}/trades", {"market": cid, "limit": 500})
        time.sleep(RATE_PAUSE)
        if mdata:
            trades.extend(mdata)
    print(f"  Total trades after per-market fetch: {len(trades):,}")

    # Step 3: Compute market stats
    print("  Computing market statistics...")
    market_stats = compute_market_stats(trades)
    print(f"    Stats for {len(market_stats)} markets with sufficient data.")

    # Step 4: Find suspicious trades (with insider detection)
    print("  Scanning for suspicious / insider trades...")
    suspicious = find_suspicious_trades(trades, market_stats, markets=markets)
    print(f"    Found {len(suspicious)} suspicious trades.")

    if suspicious:
        top = suspicious[0]
        print(f"    Top score: {top.get('score', 0)} — {top.get('title', 'Unknown')}")
        print(f"    Bet: ${top.get('usd_value', 0):,.0f} at {top.get('odds_str', '-')} odds → potential ${top.get('potential_profit', 0):,.0f} profit")

    # Step 5: Investigate wallets
    investigations = investigate_top_wallets(suspicious)

    # Step 6: Aggregate stats
    agg = compute_aggregate_stats(suspicious, investigations)

    # ─── Step 6.5: Wallet co-trading sybil/coordination clusters ───
    cluster_data: dict = {}
    try:
        from wallet_clusters import detect_clusters, wallets_in_clusters_set
        from cluster_history import record_clusters, history_stats

        print("  Detecting wallet co-trading clusters...")
        cluster_data = detect_clusters(trades)
        print(f"    Found {cluster_data.get('cluster_count', 0)} clusters "
              f"covering {cluster_data.get('wallets_in_clusters', 0)} wallets")

        # Persist clusters to history → enriches each cluster dict with
        # cluster_id, seen_count, first_seen_ts, is_recurring (in place).
        history_update = record_clusters(cluster_data.get("clusters", []))
        print(f"    Cluster history: {history_update}")
        cluster_data["history_summary"] = history_stats()

        # Bump suspicion score for any wallet that ended up in a cluster
        clustered = wallets_in_clusters_set(cluster_data)
        if clustered:
            cluster_lookup: dict[str, dict] = {}
            for c in cluster_data.get("clusters", []):
                # Recurring clusters get an extra in-place score boost
                # (they're a much stronger signal than one-time co-trades).
                if c.get("is_recurring"):
                    c["score"] = min(150, (c.get("score", 0) or 0) + 20)
                    c.setdefault("reasons", []).insert(
                        0,
                        f"RECURRING: same wallets seen in {c.get('seen_count', 0)} scans",
                    )
                for w in c.get("wallets", []):
                    cluster_lookup.setdefault(w.lower(), c)
            # Re-sort clusters since recurring bump may have shuffled order
            cluster_data["clusters"] = sorted(
                cluster_data.get("clusters", []),
                key=lambda c: (c.get("score", 0), c.get("wallet_count", 0)),
                reverse=True,
            )

            for s in suspicious:
                addr = (s.get("wallet") or "").lower()
                if addr in clustered:
                    c = cluster_lookup.get(addr, {})
                    s["in_sybil_cluster"] = True
                    s["sybil_cluster_size"] = c.get("wallet_count", 0)
                    s["sybil_cluster_score"] = c.get("score", 0)
                    s["sybil_cluster_recurring"] = bool(c.get("is_recurring"))
                    base_bump = 15
                    if c.get("is_recurring"):
                        base_bump = 25  # extra weight for repeat offenders
                    s["score"] = min(120, s["score"] + base_bump)
                    label = "RECURRING " if c.get("is_recurring") else ""
                    s["reasons"].append(
                        f"Member of {label}{c.get('wallet_count', 0)}-wallet co-trading cluster"
                    )
    except Exception as e:
        print(f"  Cluster detection failed: {e}")
        import traceback
        traceback.print_exc()

    # ─── Step 7: Retroactive insider analysis + Bayesian + ML enrichment ───
    retro_data = {}
    bayesian_summary = {}
    ml_data = {}
    if include_retroactive:
        try:
            from resolved_markets import (
                fetch_resolved_markets,
                find_longshot_winners,
                aggregate_repeat_winners,
                fetch_market_trades,
            )
            from bayesian_wallets import (
                update_from_winners,
                top_wallets_by_edge,
                stats_summary as bayes_summary,
                score_wallet,
            )
            from wallet_ml import rank_wallets

            print("  Running retroactive insider scan...")
            resolved = fetch_resolved_markets(days=retroactive_days, limit=retroactive_max_markets * 2)
            winners = find_longshot_winners(resolved, max_markets=retroactive_max_markets)
            profiles = aggregate_repeat_winners(winners)

            # Pull all trades for the resolved markets we kept.
            # We need this for both Bayesian losses AND trader-quality scoring.
            # To get a less long-shot-biased sample for the quality scorer we
            # also include the highest-volume resolved markets regardless of
            # whether anyone won a long-shot on them.
            print("  Building wallet trade map for Bayesian + quality updates...")
            scanned_market_ids: set[str] = {
                p["wins"][0]["market_id"]
                for p in profiles[:30]
                if p.get("wins") and isinstance(p["wins"][0], dict) and p["wins"][0].get("market_id")
            }
            high_vol_resolved = sorted(
                resolved, key=lambda m: m.get("volume", 0) or 0, reverse=True
            )[:40]
            for m in high_vol_resolved:
                cid = m.get("condition_id") or ""
                if cid:
                    scanned_market_ids.add(cid)

            all_market_trades: dict[str, list[dict]] = {}
            for cid in scanned_market_ids:
                tr = fetch_market_trades(cid, max_trades=1500)
                if tr:
                    all_market_trades[cid] = tr
                time.sleep(RATE_PAUSE)

            # Update Bayesian state (idempotent — safe to re-run)
            bayes_update = update_from_winners(winners, all_market_trades)
            print(f"    Bayesian: {bayes_update}")
            bayesian_summary = bayes_summary()
            top_bayesian = top_wallets_by_edge(limit=50, min_bets=2)

            # Run ML ranker on the winner feature set
            print("  Running ML wallet ranker...")
            ml_data = rank_wallets(winners)

            retro_data = {
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "lookback_days": retroactive_days,
                "markets_scanned": min(len(resolved), retroactive_max_markets),
                "total_winners": len(winners),
                "unique_winning_wallets": len(profiles),
                "profiles": profiles[:50],
                "top_bayesian_wallets": top_bayesian,
            }

            # Enrich live suspicious trades with Bayesian / ML wallet scores
            ml_combined_by_wallet = {r["wallet"].lower(): r for r in ml_data.get("combined", [])}
            for s in suspicious:
                addr = (s.get("wallet") or "").lower()
                if not addr:
                    continue
                bw = score_wallet(addr)
                if bw:
                    s["bayesian_edge"] = bw["posterior_mean"]
                    s["bayesian_p_above_baseline"] = bw["prob_above_baseline"]
                    s["bayesian_high_confidence"] = bw["high_confidence"]
                    s["bayesian_longshot_bets"] = bw["longshot_bets"]
                    s["bayesian_longshot_wins"] = bw["longshot_wins"]
                    if bw["high_confidence"]:
                        s["score"] = min(120, s["score"] + 20)
                        s["reasons"].append(
                            f"Bayesian high-confidence edge: {bw['posterior_mean']:.0%} win rate "
                            f"({bw['longshot_wins']}/{bw['longshot_bets']} long-shots)"
                        )
                ml_entry = ml_combined_by_wallet.get(addr)
                if ml_entry:
                    s["ml_combined_score"] = ml_entry["combined_score"]
                    s["ml_isolation_score"] = ml_entry["isolation_score"]
                    s["ml_xgboost_score"] = ml_entry["xgboost_score"]
                    s["ml_is_anomaly"] = ml_entry["is_anomaly"]
                    if ml_entry["combined_score"] >= 80:
                        s["score"] = min(120, s["score"] + 12)
                        s["reasons"].append(f"ML anomaly score {ml_entry['combined_score']:.0f}/100")
            # Re-sort after enrichment
            suspicious.sort(key=lambda x: (x["score"], x["potential_profit"]), reverse=True)
        except Exception as e:
            print(f"  Retroactive/Bayesian/ML enrichment failed: {e}")
            import traceback
            traceback.print_exc()

    # ─── Step 8: Trader Quality (copy-trade ranking) ───
    quality_data: dict = {}
    try:
        from trader_quality import run_quality_scan, score_wallet_quality

        # Wallets in sybil clusters are excluded from copy-trade rankings.
        excluded = wallets_in_clusters_set(cluster_data) if cluster_data else set()

        # Reuse the resolved + market-trades data the retroactive step fetched.
        if "resolved" in locals() and "all_market_trades" in locals() and resolved and all_market_trades:
            print("  Computing trader quality scores...")
            quality_data = run_quality_scan(
                resolved_markets=resolved,
                market_trades=all_market_trades,
                exclude_clusters=excluded,
            )
            print(
                f"    Quality: applied {quality_data.get('bets_applied_this_scan', 0)} new bets "
                f"across {quality_data.get('wallets_touched_this_scan', 0)} wallets; "
                f"{len(quality_data.get('top_traders', []))} top traders"
            )

            # Annotate live suspicious trades with quality flag (so a high-quality
            # wallet making a suspicious-looking trade gets context).
            for s in suspicious:
                addr = (s.get("wallet") or "").lower()
                if not addr:
                    continue
                q = score_wallet_quality(addr)
                if q and q.get("quality_score", 0) >= 60:
                    s["quality_score"] = q["quality_score"]
                    s["quality_roi"] = q["roi"]
                    s["quality_win_rate"] = q["win_rate"]
                    s["reasons"].append(
                        f"Wallet has trader-quality score {q['quality_score']}/100 "
                        f"(ROI {q['roi']:.0%}, win-rate {q['win_rate']:.0%})"
                    )
        else:
            print("  Trader quality skipped — no resolved markets data available")
    except Exception as e:
        print(f"  Trader quality scan failed: {e}")
        import traceback
        traceback.print_exc()

    # ─── Step 9: Smart Money Flow (open positions of top traders) ───
    smart_money_data: dict = {}
    try:
        from smart_money import aggregate_smart_money

        top_for_flow = quality_data.get("top_traders", [])[:25] if quality_data else []
        if top_for_flow:
            print(f"  Computing smart-money flow across {len(top_for_flow)} top wallets...")
            smart_money_data = aggregate_smart_money(top_for_flow, max_wallets=25)
            print(
                f"    Smart money: {smart_money_data.get('consensus_markets', 0)} consensus markets "
                f"from {smart_money_data.get('wallets_scanned', 0)} wallets"
            )
    except Exception as e:
        print(f"  Smart money aggregation failed: {e}")
        import traceback
        traceback.print_exc()

    # ─── Step 10: Wallet metadata (Polygonscan age + funding) — best effort ───
    metadata_data: dict = {}
    try:
        from wallet_metadata import is_available as meta_available, enrich_addresses
        if meta_available():
            top_addrs = [t["address"] for t in (quality_data.get("top_traders") or [])[:15]]
            top_addrs += [p["wallet"] for p in (retro_data.get("profiles") or [])[:10] if p.get("wallet")]
            unique_addrs = list({a.lower() for a in top_addrs if a})
            print(f"  Enriching {len(unique_addrs)} wallets with Polygonscan metadata...")
            metadata_data = enrich_addresses(unique_addrs, limit=25)
        else:
            print("  Wallet metadata skipped — POLYGONSCAN_API_KEY not set")
    except Exception as e:
        print(f"  Wallet metadata enrichment failed: {e}")

    # Prepare serializable output
    inv_serializable = {}
    for addr, inv in investigations.items():
        inv_copy = dict(inv)
        if inv_copy.get("first_seen"):
            inv_copy["first_seen"] = inv_copy["first_seen"].isoformat()
        if inv_copy.get("last_seen"):
            inv_copy["last_seen"] = inv_copy["last_seen"].isoformat()
        inv_serializable[addr] = inv_copy

    # Final re-sort after all enrichments (cluster bump may have changed scores)
    suspicious.sort(key=lambda x: (x["score"], x["potential_profit"]), reverse=True)

    result = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "scan_hours": SCAN_HOURS,
        "total_trades_scanned": len(trades),
        "suspicious_trades": suspicious[:100],  # top 100
        "wallet_investigations": inv_serializable,
        "aggregate_stats": agg,
        "retroactive": retro_data,
        "bayesian_summary": bayesian_summary,
        "ml": {
            "wallet_count": ml_data.get("wallet_count", 0),
            "available_models": ml_data.get("available_models", {}),
            "combined": ml_data.get("combined", [])[:30],
            "isolation_forest": ml_data.get("isolation_forest", [])[:20],
            "xgboost": ml_data.get("xgboost", [])[:20],
        },
        "clusters": cluster_data,
        "quality": quality_data,
        "smart_money": smart_money_data,
        "wallet_metadata": metadata_data,
    }

    # Cache (atomic write)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache_file), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, default=str)
        os.replace(tmp, cache_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"  Cached to {cache_file.name}")

    return result


if __name__ == "__main__":
    result = run_scanner()

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    agg = result["aggregate_stats"]
    if agg:
        print(f"  Trades scanned:       {result['total_trades_scanned']:,}")
        print(f"  Flagged:              {agg['total_flagged']}")
        print(f"  Total flagged vol:    ${agg['total_volume_flagged']:,.0f}")
        print(f"  Total potential profit: ${agg['total_potential_profit']:,.0f}")
        print(f"  Unique wallets:       {agg['unique_wallets']}")
        print(f"  Unique markets:       {agg['unique_markets']}")
        print(f"  Long-shot bets:       {agg['longshot_trades']}")
        print(f"  Long-shot profit pot: ${agg['longshot_total_profit']:,.0f}")
        if agg.get("avg_account_age") is not None:
            print(f"  Avg account age:      {agg['avg_account_age']:.0f} days")
            print(f"  New accounts (<30d):  {agg['new_accounts']}")

    print("\n  Top 10 Suspicious Trades:")
    for i, t in enumerate(result["suspicious_trades"][:10]):
        print(f"    {i+1}. [{t['score']}] ${t['usd_value']:,.0f} bet at {t['odds_str']} → ${t['potential_profit']:,.0f} potential profit")
        print(f"       \"{t['title'][:50]}\" → {t['outcome']}")
        print(f"       {', '.join(t['reasons'])}")
