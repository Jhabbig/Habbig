#!/usr/bin/env python3
"""Backtest v2: replay historical weather forecasts vs observed outcomes.

Reads weather_price_snapshots from data.db, fetches actual observed
daily highs/lows from Open-Meteo archive, resolves each market, and
computes PnL.

Methodology "v2-walkforward-executable" — fixes over v1:
- Decision snapshot = the FIRST snapshot at which |edge| crosses the
  threshold, priced at that snapshot. (v1 used the LAST snapshot per
  market — the most informed one — which is look-ahead bias.)
- Fills at the executable side. The snapshot table carries no bid/ask,
  so fills degrade to last price + a half-spread haircut per side plus a
  per-side fee — declared in output metadata.
- Sharpe computed on per-day aggregated PnL and annualized from daily
  (sqrt(365): these markets resolve every calendar day). v1 annualized
  per-bet PnLs with sqrt(252), inflating Sharpe by ~an order of
  magnitude. Raw per-bet stats are reported alongside, unannualized.
- Threshold selection is walk-forward (expanding window, held-out
  performance only). If snapshot history spans < 90 days, the threshold
  performance curve is suppressed: only per-threshold sample sizes print.

Usage:  python3 backtest.py
Output: prints a summary + writes backtest_results.json
"""

import json
import math
import os
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
OUT_PATH = os.path.join(BASE_DIR, "backtest_results.json")

HALF_SPREAD = 0.01
FEE_RATE = 0.01
MIN_SPAN_DAYS = 90
THRESHOLDS = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]
WF_MIN_TRAIN_DAYS = 60
WF_MIN_TRAIN_BETS = 30

# ─── Station map (coords for Open-Meteo archive lookups) ─────────────────────
STATION_MAP = {
    "new york": (40.7772, -73.8726),
    "chicago": (41.9742, -87.9073),
    "dallas": (32.8471, -96.8518),
    "miami": (25.7959, -80.2870),
    "los angeles": (33.9425, -118.4081),
    "atlanta": (33.6407, -84.4277),
    "austin": (30.1945, -97.6699),
    "houston": (29.6454, -95.2789),
    "denver": (39.7169, -104.7529),
    "san francisco": (37.6213, -122.3790),
    "seattle": (47.4502, -122.3088),
    "toronto": (43.6772, -79.6306),
    "london": (51.5053, -0.0553),
    "paris": (48.7233, 2.3794),
    "tokyo": (35.5533, 139.7811),
    "seoul": (37.5586, 126.7906),
    "sydney": (-33.9461, 151.1772),
    "hong kong": (22.3080, 113.9185),
    "tel aviv": (32.0114, 34.8867),
    "panama city": (9.0714, -79.3835),
    "buenos aires": (-34.5592, -58.4156),
    "ankara": (40.1281, 32.9951),
}


def fetch_observed_temps(lat, lon, start_date, end_date):
    """Batch-fetch daily max/min temps from Open-Meteo archive → {date: {"high", "low"}}."""
    try:
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
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
        lows = daily.get("temperature_2m_min", [])
        return {d: {"high": h, "low": lo} for d, h, lo in zip(dates, highs, lows) if h is not None or lo is not None}
    except Exception as e:
        print(f"  fetch error: {e}")
        return {}


def market_metric(question):
    return "low" if "lowest temp" in question.lower() else "high"


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
    m = re.search(r"between\s+(\d+)[–\-]\s*(\d+)", q)
    if m:
        return ("between", int(m.group(1)), int(m.group(2)))
    m = re.search(r"between\s+(\d+)\s+and\s+(\d+)", q)
    if m:
        return ("between", int(m.group(1)), int(m.group(2)))

    # "X°F or higher" / "above X" / "at least X"
    m = re.search(r"(\d+)\s*°?\s*f?\s+or\s+(higher|above|more)", q)
    if m:
        return ("above", int(m.group(1)), None)
    m = re.search(r"(above|over|at\s+least|exceed)\s+(\d+)", q)
    if m:
        return ("above", int(m.group(2)), None)

    # "X°F or below" / "under X" / "at most X"
    m = re.search(r"(\d+)\s*°?\s*f?\s+or\s+(below|lower|less)", q)
    if m:
        return ("below", int(m.group(1)), None)
    m = re.search(r"(below|under|at\s+most)\s+(\d+)", q)
    if m:
        return ("below", int(m.group(2)), None)

    return (None, None, None)


def resolve_market(observed_high, threshold):
    """Given observed temp and parsed threshold, return True if YES wins."""
    kind, v1, v2 = threshold
    if kind == "above":
        return observed_high >= v1
    elif kind == "below":
        return observed_high <= v1
    elif kind == "between":
        return v1 <= observed_high <= v2
    return None


def snapshot_edge(snap):
    if snap["edge"] is not None:
        return snap["edge"]
    return snap["model_prob"] - snap["yes_price"]


def decide(snaps, thr):
    """First snapshot (chronological) where |edge| crosses thr → (snap, side)."""
    for s in snaps:
        e = snapshot_edge(s)
        if abs(e) >= thr and e != 0:
            return s, ("YES" if e > 0 else "NO")
    return None, None


def make_bet(market, thr):
    snap, side = decide(market["snaps"], thr)
    if snap is None:
        return None
    yes_price = snap["yes_price"]
    raw_cost = yes_price if side == "YES" else 1.0 - yes_price
    fill = min(max(raw_cost + HALF_SPREAD, 0.005), 0.995)
    fee = round(FEE_RATE * fill, 4)
    payout = 1.0 if (market["yes_wins"] if side == "YES" else not market["yes_wins"]) else 0.0
    return {
        "market_id": market["market_id"],
        "city": market["city"],
        "target_date": market["target_date"],
        "bet_side": side,
        "decision_timestamp": snap["timestamp"],
        "yes_price": yes_price,
        "model_prob": snap["model_prob"],
        "edge": snapshot_edge(snap),
        "fill_price": round(fill, 4),
        "fee": fee,
        "pnl_gross": round(payout - raw_cost, 4),
        "pnl_net": round(payout - fill - fee, 4),
    }


def per_bet_stats(bets):
    pnls = [b["pnl_net"] for b in bets]
    n = len(pnls)
    if n == 0:
        return None
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    avg = total / n
    std = statistics.stdev(pnls) if n > 1 else None
    return {
        "bets": n,
        "wins": wins,
        "win_pct": round(wins / n * 100, 1),
        "total_pnl_gross": round(sum(b["pnl_gross"] for b in bets), 2),
        "total_fees_and_spread": round(sum(b["pnl_gross"] - b["pnl_net"] for b in bets), 2),
        "total_pnl_net": round(total, 2),
        "avg_pnl_net": round(avg, 4),
        "std_pnl_net": round(std, 4) if std is not None else None,
        "per_bet_ratio_unannualized": round(avg / std, 3) if std else None,
    }


def daily_stats(bets):
    by_day = defaultdict(float)
    for b in bets:
        by_day[b["target_date"]] += b["pnl_net"]
    days = sorted(by_day)
    series = [by_day[d] for d in days]
    n = len(series)
    if n == 0:
        return None
    mean = sum(series) / n
    std = statistics.stdev(series) if n > 1 else None
    sharpe = round(mean / std * math.sqrt(365), 2) if std else None

    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in series:
        running += p
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    return {
        "days": n,
        "avg_daily_pnl": round(mean, 4),
        "std_daily_pnl": round(std, 4) if std is not None else None,
        "sharpe_annualized": sharpe,
        "max_drawdown": round(max_dd, 2),
        "daily_pnl": {d: round(by_day[d], 4) for d in days},
    }


def run_walk_forward(bets_by_thr):
    all_days = sorted({b["target_date"] for bets in bets_by_thr.values() for b in bets})
    if not all_days:
        return None, "no bets"
    first_day = date.fromisoformat(all_days[0])
    heldout = []
    schedule = []
    for day in all_days:
        if (date.fromisoformat(day) - first_day).days < WF_MIN_TRAIN_DAYS:
            continue
        best_thr, best_score = None, None
        for thr in THRESHOLDS:
            train = [b for b in bets_by_thr[thr] if b["target_date"] < day]
            if len(train) < WF_MIN_TRAIN_BETS:
                continue
            score = sum(b["pnl_net"] for b in train) / len(train)
            if best_score is None or score > best_score:
                best_score, best_thr = score, thr
        if best_thr is None:
            continue
        day_bets = [b for b in bets_by_thr[best_thr] if b["target_date"] == day]
        heldout.extend(day_bets)
        schedule.append(
            {
                "day": day,
                "chosen_threshold": best_thr,
                "train_avg_pnl_net": round(best_score, 4),
                "heldout_bets": len(day_bets),
            }
        )
    if not heldout:
        return None, f"no test day had ≥{WF_MIN_TRAIN_DAYS} training days with ≥{WF_MIN_TRAIN_BETS} training bets"
    return {
        "run": True,
        "min_train_days": WF_MIN_TRAIN_DAYS,
        "min_train_bets": WF_MIN_TRAIN_BETS,
        "schedule": schedule,
        "heldout_per_bet": per_bet_stats(heldout),
        "heldout_daily": daily_stats(heldout),
    }, None


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Load all pre-resolution snapshots for resolved markets, oldest first.
    #    Snapshots timestamped after target_date are post-resolution → excluded.
    print("Loading resolved market snapshots...")
    rows = conn.execute("""
        SELECT market_id, question, city, target_date, yes_price, model_prob, edge, timestamp
        FROM weather_price_snapshots
        WHERE target_date < date('now')
          AND city IS NOT NULL
          AND model_prob IS NOT NULL
          AND yes_price IS NOT NULL
          AND substr(timestamp, 1, 10) <= target_date
        ORDER BY market_id, timestamp ASC
    """).fetchall()
    print(f"  {len(rows)} usable snapshots")

    snaps_by_market = defaultdict(list)
    snap_days = set()
    for r in rows:
        snaps_by_market[r["market_id"]].append(dict(r))
        snap_days.add(r["timestamp"][:10])
    print(f"  {len(snaps_by_market)} unique markets")

    if not snaps_by_market:
        print("No snapshots — nothing to do.")
        return

    span_days = (date.fromisoformat(max(snap_days)) - date.fromisoformat(min(snap_days))).days + 1
    suppressed = span_days < MIN_SPAN_DAYS
    print(f"  Snapshot history: {min(snap_days)} → {max(snap_days)} ({span_days} calendar days, {len(snap_days)} distinct snapshot days)")

    # 2. Group by city and fetch observed temps
    print("\nFetching observed temperatures from Open-Meteo archive...")
    city_dates = defaultdict(set)
    for snaps in snaps_by_market.values():
        city_dates[snaps[0]["city"]].add(snaps[0]["target_date"])

    observed = {}  # (city, date) -> {"high": temp_f, "low": temp_f}
    for city, dates in sorted(city_dates.items()):
        coords = STATION_MAP.get(city)
        if not coords:
            print(f"  {city}: no coords, skipping")
            continue
        sorted_dates = sorted(dates)
        lat, lon = coords
        batch = fetch_observed_temps(lat, lon, sorted_dates[0], sorted_dates[-1])
        for d in sorted_dates:
            if d in batch:
                observed[(city, d)] = batch[d]
        print(f"  {city}: {len([d for d in sorted_dates if (city, d) in observed])}/{len(sorted_dates)} dates resolved")
        time.sleep(0.5)  # Be polite

    # 3. Resolve each market
    print("\nResolving markets...")
    markets = []
    unresolvable = {"no_threshold": 0, "no_observed": 0, "ambiguous": 0}
    for mid, snaps in snaps_by_market.items():
        first = snaps[0]
        threshold = parse_threshold(first["question"])
        if threshold[0] is None:
            unresolvable["no_threshold"] += 1
            continue
        key = (first["city"], first["target_date"])
        metric = market_metric(first["question"])
        obs = (observed.get(key) or {}).get(metric)
        if obs is None:
            unresolvable["no_observed"] += 1
            continue
        yes_wins = resolve_market(obs, threshold)
        if yes_wins is None:
            unresolvable["ambiguous"] += 1
            continue
        markets.append(
            {
                "market_id": mid,
                "question": first["question"],
                "city": first["city"],
                "target_date": first["target_date"],
                "metric": metric,
                "observed_high": obs,
                "threshold": threshold,
                "yes_wins": yes_wins,
                "snaps": snaps,
            }
        )

    print(f"  Resolved: {len(markets)}")
    print(f"  Skipped: {unresolvable}")

    print("\n" + "=" * 80)
    print("BACKTEST RESULTS  (methodology: v2-walkforward-executable)")
    print("=" * 80)
    if not markets:
        print("No resolved markets — nothing to report.")
        return
    print(f"Period: {min(m['target_date'] for m in markets)} to {max(m['target_date'] for m in markets)}")
    print(f"Markets resolved: {len(markets)}")
    print(f"Cities: {len(set(m['city'] for m in markets))}")
    print(f"Fill model: NO bid/ask in snapshots → last price + {HALF_SPREAD:.2f} half-spread haircut per side + {FEE_RATE:.0%} per-side fee")

    # 4. Bets per threshold — decision = first |edge| crossing, no look-ahead
    bets_by_thr = {thr: [b for b in (make_bet(m, thr) for m in markets) if b] for thr in THRESHOLDS}
    base_bets = bets_by_thr[0.0]
    base_by_mid = {b["market_id"]: b for b in base_bets}

    # 5. Per-market result rows (base threshold decision), old JSON keys preserved
    results = []
    for m in markets:
        b = base_by_mid.get(m["market_id"])
        results.append(
            {
                "market_id": m["market_id"],
                "question": m["question"],
                "city": m["city"],
                "target_date": m["target_date"],
                "metric": m["metric"],
                "observed_high": m["observed_high"],
                "threshold": m["threshold"],
                "yes_wins": m["yes_wins"],
                "yes_price": b["yes_price"] if b else m["snaps"][0]["yes_price"],
                "model_prob": b["model_prob"] if b else m["snaps"][0]["model_prob"],
                "edge": b["edge"] if b else snapshot_edge(m["snaps"][0]),
                "edge_pct": round((b["edge"] if b else snapshot_edge(m["snaps"][0])) * 100, 1),
                "bet_side": b["bet_side"] if b else None,
                "decision_timestamp": b["decision_timestamp"] if b else None,
                "fill_price": b["fill_price"] if b else None,
                "fee": b["fee"] if b else None,
                "pnl": b["pnl_gross"] if b else None,
                "pnl_net": b["pnl_net"] if b else None,
            }
        )

    # 6. Model calibration (probabilities as of the decision snapshot)
    print("\n--- Model Calibration (at decision snapshot) ---")
    bins = [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    for lo, hi in bins:
        in_bin = [r for r in results if lo <= r["model_prob"] < hi]
        if not in_bin:
            continue
        actual_rate = sum(1 for r in in_bin if r["yes_wins"]) / len(in_bin)
        mid = (lo + hi) / 2
        print(f"  Model says {lo:.0%}–{hi:.0%}: actual YES rate = {actual_rate:.1%}  (n={len(in_bin)})  {'✓ calibrated' if abs(actual_rate - mid) < 0.15 else '✗ off'}")

    # 7. Overall stats: honest daily Sharpe + raw per-bet stats side by side
    overall_pb = per_bet_stats(base_bets)
    overall_daily = daily_stats(base_bets)
    print("\n--- Overall (all signals, executable fills, net of fees+spread) ---")
    if overall_pb:
        print(
            f"  Per-bet:  n={overall_pb['bets']}  win%={overall_pb['win_pct']}  "
            f"gross={overall_pb['total_pnl_gross']:+.2f}  net={overall_pb['total_pnl_net']:+.2f}  "
            f"avg={overall_pb['avg_pnl_net']:+.4f}  "
            f"ratio(unannualized)={overall_pb['per_bet_ratio_unannualized']}"
        )
        print(
            f"  Daily:    days={overall_daily['days']}  avg={overall_daily['avg_daily_pnl']:+.4f}  "
            f"Sharpe(ann., sqrt365)={overall_daily['sharpe_annualized']}  "
            f"maxDD={overall_daily['max_drawdown']:+.2f}"
        )
        if overall_daily["days"] < 30:
            print(f"  ⚠ Only {overall_daily['days']} trading days — the daily Sharpe is statistically meaningless at this sample size.")
    else:
        print("  No bets generated.")

    # 8. Threshold curve — refused below MIN_SPAN_DAYS to prevent overfitting a
    #    threshold on a tiny window; sample sizes only.
    summary = {}
    if suppressed:
        print("\n" + "!" * 80)
        print(f"WARNING: snapshot history spans only {span_days} days (< {MIN_SPAN_DAYS}).")
        print("Refusing to print a PnL-by-threshold curve — any threshold chosen from this")
        print("window would be overfit. Per-threshold SAMPLE SIZES only:")
        print("!" * 80)
        print(f"{'Threshold':>10} {'Bets':>6}")
        for thr in THRESHOLDS:
            n = len(bets_by_thr[thr])
            print(f"  {thr:>8.0%} {n:>6}")
            summary[f"{thr:.0%}"] = {
                "threshold": thr,
                "bets": n,
                "wins": None,
                "win_pct": None,
                "total_pnl": None,
                "avg_pnl": None,
                "sharpe": None,
                "max_drawdown": None,
                "suppressed": True,
            }
    else:
        print("\n--- PnL by Edge Threshold (in-sample reference — headline numbers are the")
        print("    walk-forward held-out results below) ---")
        print(f"{'Threshold':>10} {'Bets':>6} {'Wins':>6} {'Win%':>7} {'Net PnL':>10} {'Avg Net':>9} {'Sharpe(d)':>9} {'Max DD':>8}")
        print("-" * 80)
        for thr in THRESHOLDS:
            bets = bets_by_thr[thr]
            if not bets:
                print(f"  {thr:>8.0%}   {'---':>6}")
                continue
            pb = per_bet_stats(bets)
            ds = daily_stats(bets)
            print(
                f"  {thr:>8.0%} {pb['bets']:>6} {pb['wins']:>6} {pb['win_pct']:>6.1f}% "
                f"{pb['total_pnl_net']:>+10.2f} {pb['avg_pnl_net']:>+8.4f} "
                f"{(ds['sharpe_annualized'] if ds['sharpe_annualized'] is not None else float('nan')):>+9.2f} "
                f"{ds['max_drawdown']:>+8.2f}"
            )
            summary[f"{thr:.0%}"] = {
                "threshold": thr,
                "bets": pb["bets"],
                "wins": pb["wins"],
                "win_pct": pb["win_pct"],
                "total_pnl": pb["total_pnl_net"],
                "avg_pnl": pb["avg_pnl_net"],
                "sharpe": ds["sharpe_annualized"],
                "max_drawdown": ds["max_drawdown"],
                "suppressed": False,
            }

    # 9. Walk-forward held-out performance
    if suppressed:
        wf = {"run": False, "reason": f"history spans {span_days} days < {MIN_SPAN_DAYS} required"}
        print(f"\nWalk-forward: NOT RUN — {wf['reason']}.")
    else:
        wf_result, wf_err = run_walk_forward(bets_by_thr)
        if wf_result is None:
            wf = {"run": False, "reason": wf_err}
            print(f"\nWalk-forward: NOT RUN — {wf_err}.")
        else:
            wf = wf_result
            hp, hd = wf["heldout_per_bet"], wf["heldout_daily"]
            print("\n--- Walk-Forward Held-Out Performance (threshold chosen on training window only) ---")
            print(f"  Per-bet:  n={hp['bets']}  win%={hp['win_pct']}  net={hp['total_pnl_net']:+.2f}  avg={hp['avg_pnl_net']:+.4f}")
            print(f"  Daily:    days={hd['days']}  Sharpe(ann.)={hd['sharpe_annualized']}  maxDD={hd['max_drawdown']:+.2f}")
            thr_counts = defaultdict(int)
            for s in wf["schedule"]:
                thr_counts[s["chosen_threshold"]] += 1
            print(f"  Chosen thresholds (days): {dict(sorted(thr_counts.items()))}")

    # 10. Best and worst bets (net)
    settled = [r for r in results if r["pnl_net"] is not None]
    by_pnl = sorted(settled, key=lambda r: r["pnl_net"], reverse=True)
    print("\n--- Top 5 Best Bets (net) ---")
    for r in by_pnl[:5]:
        print(f"  PnL {r['pnl_net']:+.3f} | edge {r['edge_pct']:+.1f}% | {r['bet_side']} | obs={r['observed_high']:.1f}°F | {r['question'][:80]}")
    print("\n--- Top 5 Worst Bets (net) ---")
    for r in by_pnl[-5:]:
        print(f"  PnL {r['pnl_net']:+.3f} | edge {r['edge_pct']:+.1f}% | {r['bet_side']} | obs={r['observed_high']:.1f}°F | {r['question'][:80]}")

    # 11. Per-city breakdown (net)
    print("\n--- Per-City Performance (all edges, net) ---")
    city_stats = defaultdict(lambda: {"bets": 0, "pnl": 0, "wins": 0})
    for r in settled:
        cs = city_stats[r["city"]]
        cs["bets"] += 1
        cs["pnl"] += r["pnl_net"]
        cs["wins"] += 1 if r["pnl_net"] > 0 else 0
    print(f"{'City':>15} {'Bets':>6} {'Win%':>7} {'Net PnL':>10}")
    for city in sorted(city_stats, key=lambda c: city_stats[c]["pnl"], reverse=True):
        cs = city_stats[city]
        wp = cs["wins"] / cs["bets"] * 100 if cs["bets"] else 0
        print(f"  {city:>13} {cs['bets']:>6} {wp:>6.1f}% {cs['pnl']:>+10.2f}")

    # 12. Save results
    caveats = [
        f"Snapshots carry last trade price only (no bid/ask): fills modeled as last price + {HALF_SPREAD:.2f} half-spread haircut per side, {FEE_RATE:.0%} per-side fee on notional. Real fills may be worse.",
        "Decision = first snapshot where |edge| crosses the threshold; decision granularity is limited by the ~30-min snapshot cadence.",
        "Snapshots timestamped after target_date (UTC) are excluded as post-resolution; UTC-vs-local-day mismatch may leak a few hours for far-eastern cities.",
        "Sharpe is computed on per-target-date aggregated net PnL, annualized with sqrt(365).",
        "'lowest temperature' markets resolve against the Open-Meteo daily minimum (v1 wrongly used the daily max for all markets); the 'observed_high' key holds the observed value of each market's own metric — see 'metric'.",
    ]
    if suppressed:
        caveats.append(
            f"Snapshot history spans only {span_days} days (< {MIN_SPAN_DAYS}): threshold performance curve suppressed (sample sizes only) and walk-forward not run. Do NOT pick an edge threshold from this data."
        )
    if overall_daily and overall_daily["days"] < 30:
        caveats.append(f"Only {overall_daily['days']} distinct trading days — all Sharpe/drawdown figures are statistically unreliable at this sample size.")

    output = {
        "metadata": {
            "methodology": "v2-walkforward-executable",
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caveats": caveats,
            "fill_model": {
                "type": "last_price_plus_haircut",
                "has_bid_ask": False,
                "half_spread_haircut": HALF_SPREAD,
                "fee_rate_per_side": FEE_RATE,
            },
            "sharpe_model": {
                "aggregation": "per_target_date_daily_pnl",
                "annualization": "sqrt(365)",
            },
            "history_span_days": span_days,
            "distinct_snapshot_days": len(snap_days),
            "min_span_days_for_threshold_curve": MIN_SPAN_DAYS,
            "threshold_curve_suppressed": suppressed,
        },
        "period": {
            "start": min(m["target_date"] for m in markets),
            "end": max(m["target_date"] for m in markets),
        },
        "markets_resolved": len(markets),
        "cities": len(set(m["city"] for m in markets)),
        "overall": {"per_bet": overall_pb, "daily": overall_daily},
        "summary_by_threshold": summary,
        "walk_forward": wf,
        "results": results,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nFull results saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
