#!/usr/bin/env python3
"""Backtest: replay historical weather forecasts vs observed outcomes.

Reads weather_price_snapshots from data.db, fetches actual observed highs
from Open-Meteo archive, resolves each market, and computes PnL at various
edge thresholds.

Usage:  python3 backtest.py
Output: prints a summary table + writes backtest_results.json
"""

import json
import math
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ─── Station map (coords for Open-Meteo archive lookups) ─────────────────────
STATION_MAP = {
    "new york":      (40.7772, -73.8726),
    "chicago":       (41.9742, -87.9073),
    "dallas":        (32.8471, -96.8518),
    "miami":         (25.7959, -80.2870),
    "los angeles":   (33.9425, -118.4081),
    "atlanta":       (33.6407, -84.4277),
    "austin":        (30.1945, -97.6699),
    "houston":       (29.6454, -95.2789),
    "denver":        (39.7169, -104.7529),
    "san francisco": (37.6213, -122.3790),
    "seattle":       (47.4502, -122.3088),
    "toronto":       (43.6772, -79.6306),
    "london":        (51.5053, -0.0553),
    "paris":         (48.7233, 2.3794),
    "tokyo":         (35.5533, 139.7811),
    "seoul":         (37.5586, 126.7906),
    "sydney":        (-33.9461, 151.1772),
    "hong kong":     (22.3080, 113.9185),
    "tel aviv":      (32.0114, 34.8867),
    "panama city":   (9.0714, -79.3835),
    "buenos aires":  (-34.5592, -58.4156),
    "ankara":        (40.1281, 32.9951),
}


def fetch_observed_highs(lat, lon, start_date, end_date):
    """Batch-fetch daily max temps from Open-Meteo archive."""
    try:
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "start_date": start_date,
                "end_date": end_date,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  Open-Meteo {resp.status_code} for ({lat},{lon}) {start_date}–{end_date}")
            return {}
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        return {d: h for d, h in zip(dates, highs) if h is not None}
    except Exception as e:
        print(f"  fetch error: {e}")
        return {}


def parse_threshold(question):
    """Parse temperature threshold from market question.

    Returns (kind, value1, value2) where kind is:
      'above'  -> temp >= value1
      'below'  -> temp <= value1
      'between' -> value1 <= temp <= value2
      'exact'  -> temp == value1 (rare)
    """
    q = question.lower()

    # "between 64-65°F" / "between 64 and 65"
    m = re.search(r'between\s+(\d+)[–\-]\s*(\d+)', q)
    if m:
        return ('between', int(m.group(1)), int(m.group(2)))
    m = re.search(r'between\s+(\d+)\s+and\s+(\d+)', q)
    if m:
        return ('between', int(m.group(1)), int(m.group(2)))

    # "X°F or higher" / "above X" / "at least X"
    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(higher|above|more)', q)
    if m:
        return ('above', int(m.group(1)), None)
    m = re.search(r'(above|over|at\s+least|exceed)\s+(\d+)', q)
    if m:
        return ('above', int(m.group(2)), None)

    # "X°F or below" / "under X" / "at most X"
    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(below|lower|less)', q)
    if m:
        return ('below', int(m.group(1)), None)
    m = re.search(r'(below|under|at\s+most)\s+(\d+)', q)
    if m:
        return ('below', int(m.group(2)), None)

    return (None, None, None)


def resolve_market(observed_high, threshold):
    """Given observed temp and parsed threshold, return True if YES wins."""
    kind, v1, v2 = threshold
    if kind == 'above':
        return observed_high >= v1
    elif kind == 'below':
        return observed_high <= v1
    elif kind == 'between':
        return v1 <= observed_high <= v2
    return None


def main():
    conn = sqlite3.connect("data.db")
    conn.row_factory = sqlite3.Row

    # 1. Get all resolved markets (target_date in the past)
    print("Loading resolved market snapshots...")
    rows = conn.execute("""
        SELECT market_id, question, city, target_date, yes_price, model_prob, edge, timestamp
        FROM weather_price_snapshots
        WHERE target_date < date('now')
          AND city IS NOT NULL
          AND model_prob IS NOT NULL
          AND yes_price IS NOT NULL
        ORDER BY market_id, timestamp DESC
    """).fetchall()
    print(f"  {len(rows)} total snapshots")

    # 2. Take the LAST snapshot per market (closest to resolution)
    last_snap = {}
    for r in rows:
        mid = r["market_id"]
        if mid not in last_snap:  # Already sorted DESC
            last_snap[mid] = dict(r)
    print(f"  {len(last_snap)} unique markets with final snapshots")

    # 3. Group by city and fetch observed temps
    print("\nFetching observed temperatures from Open-Meteo archive...")
    city_dates = defaultdict(set)
    for m in last_snap.values():
        city_dates[m["city"]].add(m["target_date"])

    observed = {}  # (city, date) -> temp_f
    for city, dates in sorted(city_dates.items()):
        coords = STATION_MAP.get(city)
        if not coords:
            print(f"  {city}: no coords, skipping")
            continue
        sorted_dates = sorted(dates)
        lat, lon = coords
        batch = fetch_observed_highs(lat, lon, sorted_dates[0], sorted_dates[-1])
        for d in sorted_dates:
            if d in batch:
                observed[(city, d)] = batch[d]
        print(f"  {city}: {len([d for d in sorted_dates if (city, d) in observed])}/{len(sorted_dates)} dates resolved")
        time.sleep(0.5)  # Be polite

    # 4. Resolve each market
    print("\nResolving markets...")
    results = []
    unresolvable = {"no_threshold": 0, "no_observed": 0, "ambiguous": 0}
    for mid, m in last_snap.items():
        threshold = parse_threshold(m["question"])
        if threshold[0] is None:
            unresolvable["no_threshold"] += 1
            continue
        key = (m["city"], m["target_date"])
        if key not in observed:
            unresolvable["no_observed"] += 1
            continue
        obs = observed[key]
        yes_wins = resolve_market(obs, threshold)
        if yes_wins is None:
            unresolvable["ambiguous"] += 1
            continue

        yes_price = m["yes_price"]
        model_prob = m["model_prob"]
        edge = m["edge"] or 0.0

        # PnL if we bet $1 based on model signal
        # Bet YES when edge > 0 (model says more likely than market)
        # Bet NO when edge < 0 (model says less likely than market)
        pnl = None
        bet_side = None
        if edge > 0:
            # Buy YES at yes_price → payout $1 if YES wins, $0 if NO
            bet_side = "YES"
            pnl = (1.0 - yes_price) if yes_wins else -yes_price
        elif edge < 0:
            # Buy NO at (1 - yes_price) → payout $1 if NO wins, $0 if YES
            bet_side = "NO"
            no_price = 1.0 - yes_price
            pnl = (1.0 - no_price) if not yes_wins else -no_price

        results.append({
            "market_id": mid,
            "question": m["question"],
            "city": m["city"],
            "target_date": m["target_date"],
            "observed_high": obs,
            "threshold": threshold,
            "yes_wins": yes_wins,
            "yes_price": yes_price,
            "model_prob": model_prob,
            "edge": edge,
            "edge_pct": round(edge * 100, 1),
            "bet_side": bet_side,
            "pnl": round(pnl, 4) if pnl is not None else None,
        })

    print(f"  Resolved: {len(results)}")
    print(f"  Skipped: {unresolvable}")

    # 5. Compute stats at various edge thresholds
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS")
    print("=" * 80)
    if not results:
        print("No resolved markets — nothing to report.")
        return
    print(f"Period: {min(r['target_date'] for r in results)} to {max(r['target_date'] for r in results)}")
    print(f"Markets resolved: {len(results)}")
    print(f"Cities: {len(set(r['city'] for r in results))}")
    print(f"Observed temps available: {len(observed)}")

    # Overall model calibration
    print("\n--- Model Calibration ---")
    bins = [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    for lo, hi in bins:
        in_bin = [r for r in results if lo <= r["model_prob"] < hi]
        if not in_bin:
            continue
        actual_rate = sum(1 for r in in_bin if r["yes_wins"]) / len(in_bin)
        mid = (lo + hi) / 2
        print(f"  Model says {lo:.0%}–{hi:.0%}: actual YES rate = {actual_rate:.1%}  (n={len(in_bin)})  {'✓ calibrated' if abs(actual_rate - mid) < 0.15 else '✗ off'}")

    # PnL by edge threshold
    print("\n--- PnL by Edge Threshold (bet $1 on every signal above threshold) ---")
    print(f"{'Threshold':>10} {'Bets':>6} {'Wins':>6} {'Win%':>7} {'Total PnL':>10} {'Avg PnL':>9} {'Sharpe':>7} {'Max DD':>8}")
    print("-" * 75)

    thresholds = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]
    summary = {}
    for thr in thresholds:
        bets = [r for r in results if r["pnl"] is not None and abs(r["edge"]) >= thr]
        if not bets:
            print(f"  {thr:>8.0%}   {'---':>6}")
            continue
        wins = sum(1 for b in bets if b["pnl"] > 0)
        pnls = [b["pnl"] for b in bets]
        total = sum(pnls)
        avg = total / len(pnls)
        std = statistics.stdev(pnls) if len(pnls) > 1 else 1.0
        sharpe = (avg / std) * math.sqrt(252) if std > 0 else 0.0

        # Max drawdown
        cumulative = []
        running = 0
        for p in pnls:
            running += p
            cumulative.append(running)
        peak = 0
        max_dd = 0
        for c in cumulative:
            peak = max(peak, c)
            max_dd = min(max_dd, c - peak)

        win_pct = wins / len(bets) * 100
        print(f"  {thr:>8.0%} {len(bets):>6} {wins:>6} {win_pct:>6.1f}% {total:>+10.2f} {avg:>+8.4f} {sharpe:>+7.2f} {max_dd:>+8.2f}")
        summary[f"{thr:.0%}"] = {
            "threshold": thr, "bets": len(bets), "wins": wins,
            "win_pct": round(win_pct, 1), "total_pnl": round(total, 2),
            "avg_pnl": round(avg, 4), "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd, 2),
        }

    # Fees impact
    print("\n--- After Polymarket Fees (~2% per trade) ---")
    FEE = 0.02
    print(f"{'Threshold':>10} {'Bets':>6} {'Gross PnL':>10} {'Fees':>8} {'Net PnL':>10} {'Net Avg':>9}")
    print("-" * 65)
    for thr in thresholds:
        bets = [r for r in results if r["pnl"] is not None and abs(r["edge"]) >= thr]
        if not bets:
            continue
        gross = sum(b["pnl"] for b in bets)
        fees = len(bets) * FEE
        net = gross - fees
        net_avg = net / len(bets)
        print(f"  {thr:>8.0%} {len(bets):>6} {gross:>+10.2f} {fees:>8.2f} {net:>+10.2f} {net_avg:>+8.4f}")

    # Best and worst bets
    print("\n--- Top 5 Best Bets ---")
    by_pnl = sorted([r for r in results if r["pnl"] is not None], key=lambda r: r["pnl"], reverse=True)
    for r in by_pnl[:5]:
        print(f"  PnL {r['pnl']:+.3f} | edge {r['edge_pct']:+.1f}% | {r['bet_side']} | obs={r['observed_high']:.1f}°F | {r['question'][:80]}")

    print("\n--- Top 5 Worst Bets ---")
    for r in by_pnl[-5:]:
        print(f"  PnL {r['pnl']:+.3f} | edge {r['edge_pct']:+.1f}% | {r['bet_side']} | obs={r['observed_high']:.1f}°F | {r['question'][:80]}")

    # Per-city breakdown
    print("\n--- Per-City Performance (all edges) ---")
    city_stats = defaultdict(lambda: {"bets": 0, "pnl": 0, "wins": 0})
    for r in results:
        if r["pnl"] is None:
            continue
        cs = city_stats[r["city"]]
        cs["bets"] += 1
        cs["pnl"] += r["pnl"]
        cs["wins"] += 1 if r["pnl"] > 0 else 0
    print(f"{'City':>15} {'Bets':>6} {'Win%':>7} {'PnL':>10}")
    for city in sorted(city_stats, key=lambda c: city_stats[c]["pnl"], reverse=True):
        cs = city_stats[city]
        wp = cs["wins"] / cs["bets"] * 100 if cs["bets"] else 0
        print(f"  {city:>13} {cs['bets']:>6} {wp:>6.1f}% {cs['pnl']:>+10.2f}")

    # Save results
    output = {
        "period": {
            "start": min(r["target_date"] for r in results),
            "end": max(r["target_date"] for r in results),
        },
        "markets_resolved": len(results),
        "cities": len(set(r["city"] for r in results)),
        "summary_by_threshold": summary,
        "results": results,
    }
    with open("backtest_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nFull results saved to backtest_results.json")


if __name__ == "__main__":
    main()
