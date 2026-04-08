#!/usr/bin/env python3
"""
Polymarket Suspicious Trades Scanner
Scans recent Polymarket trades for unusually large or anomalous bets,
calculates potential profit, investigates wallets, and outputs data for the dashboard.

Key insight: A $1,000 bet at 5% odds (20:1) = $20,000 potential profit.
That's way more suspicious than a $10,000 bet at 50% odds (2:1) = $10,000 potential profit.
We rank by POTENTIAL PROFIT, not just trade size.
"""

import requests
import time
import json
import math
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
            ts = t.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except (ValueError, TypeError, AttributeError):
                    ts = 0
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


def compute_market_stats(trades):
    """Compute per-market trade statistics."""
    market_trades = defaultdict(list)
    for t in trades:
        key = t.get("conditionId") or t.get("slug", "unknown")
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        usd_value = size * price if price > 0 else size
        potential_profit = calc_potential_profit(usd_value, price)
        market_trades[key].append({
            "size": size,
            "usd_value": usd_value,
            "price": price,
            "potential_profit": potential_profit,
            "trade": t,
        })

    stats = {}
    for market_id, mtrades in market_trades.items():
        sizes = [t["usd_value"] for t in mtrades]
        profits = [t["potential_profit"] for t in mtrades]
        if len(sizes) < MIN_TRADES_FOR_STATS:
            continue
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
        }
    return stats


def find_suspicious_trades(trades, market_stats):
    """Flag trades by POTENTIAL PROFIT, statistical outlier status, and odds context."""
    suspicious = []

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
        ts = t.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError, AttributeError):
                ts = 0

        # Compute z-score if we have market stats
        zscore = 0
        market_mean = 0
        market_std = 0
        market_volume = 0
        if market_id in market_stats:
            ms = market_stats[market_id]
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
            reasons.append(f"Big bet on a long-shot — classic insider pattern")
        elif price <= 0.25 and usd_value >= 10000:
            score += 10
            reasons.append(f"Large bet on underdog")
        elif price <= 0.50 and usd_value >= 25000:
            score += 7
            reasons.append(f"Heavy bet at medium odds — notable")
        elif usd_value >= 50000:
            score += 5
            reasons.append(f"Very large position at any odds")

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
        "markets_traded": set(),
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
            result["markets_traded"].add(t.get("title", t.get("slug", "?")))

            ts = t.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except (ValueError, TypeError, AttributeError):
                    ts = 0
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

    result["markets_traded"] = len(result["markets_traded"])
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

    # Account ages
    ages = [inv["account_age_days"] for inv in wallet_investigations.values() if inv["account_age_days"] is not None]
    new_accounts = sum(1 for a in ages if a < 30)

    # Long-shot stats
    longshot_trades = [t for t in suspicious_trades if t["price"] <= 0.15]
    longshot_profit = sum(t["potential_profit"] for t in longshot_trades)

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
    }


# ═══════════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════

def run_scanner():
    """Full scan pipeline. Returns data for dashboard integration."""
    print("=" * 60)
    print("  Polymarket Suspicious Trades Scanner")
    print("=" * 60)

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"suspicious_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}.json"

    # Check cache (valid for 1 hour)
    if cache_file.exists():
        print("  Loading cached scan results...")
        with open(cache_file) as f:
            return json.load(f)

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

    # Step 4: Find suspicious trades
    print("  Scanning for suspicious trades...")
    suspicious = find_suspicious_trades(trades, market_stats)
    print(f"    Found {len(suspicious)} suspicious trades.")

    if suspicious:
        top = suspicious[0]
        print(f"    Top score: {top['score']} — {top['title']}")
        print(f"    Bet: ${top['usd_value']:,.0f} at {top['odds_str']} odds → potential ${top['potential_profit']:,.0f} profit")

    # Step 5: Investigate wallets
    investigations = investigate_top_wallets(suspicious)

    # Step 6: Aggregate stats
    agg = compute_aggregate_stats(suspicious, investigations)

    # Prepare serializable output
    inv_serializable = {}
    for addr, inv in investigations.items():
        inv_copy = dict(inv)
        if inv_copy.get("first_seen"):
            inv_copy["first_seen"] = inv_copy["first_seen"].isoformat()
        if inv_copy.get("last_seen"):
            inv_copy["last_seen"] = inv_copy["last_seen"].isoformat()
        inv_serializable[addr] = inv_copy

    result = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "scan_hours": SCAN_HOURS,
        "total_trades_scanned": len(trades),
        "suspicious_trades": suspicious[:100],  # top 100
        "wallet_investigations": inv_serializable,
        "aggregate_stats": agg,
    }

    # Cache
    with open(cache_file, "w") as f:
        json.dump(result, f, default=str)
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
