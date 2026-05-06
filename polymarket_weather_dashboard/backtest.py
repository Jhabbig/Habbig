#!/usr/bin/env python3
"""Walk-forward backtest for the weather dashboard.

Major changes over the previous version:
  * Pulls historical prices straight from Polymarket's CLOB
    `prices-history` endpoint, so the backtest no longer depends on having
    a `weather_price_snapshots` row at the right moment in time.
  * Replays each market at multiple lead times (T-1, T-3, T-7, T-14 days)
    instead of only the closest-to-truth snapshot, removing the optimistic
    bias of "betting at the final price."
  * Calibration metrics next to PnL: Brier score, log loss, per-bucket
    reliability — the actual model question.
  * Bootstrap 90% confidence interval on Sharpe at every edge threshold so
    we know which tier numbers are real and which are tiny-sample noise.
  * Per-(lead-time × station) breakdowns so the report tells you *where*
    the edge is (or isn't), not just the overall average.

Usage
    python3 backtest.py                      # last 90d, all leads, all cities
    python3 backtest.py --days 180           # extend window
    python3 backtest.py --leads 1,3,7        # subset of leads
    python3 backtest.py --no-network         # use only data.db, skip Polymarket fetch
    python3 backtest.py --output out.json    # custom output path

Output
    Prints a multi-section table to stdout and writes the full structured
    result to `backtest_results.json`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import weather_calibration as wcal
import weather_pure as wpure

try:
    import polymarket_history as poly_hist
except ImportError:
    poly_hist = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_LEADS_DAYS = (1, 3, 7, 14)
DEFAULT_THRESHOLDS = (0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30)
POLYMARKET_FEE = 0.02

# Coordinates for each city we resolve. Same shape as server.py STATION_MAP
# but trimmed to just lat/lon for backtest purposes.
STATION_COORDS = {
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


# ─── Observed temps from Open-Meteo archive ───────────────────────────────────

def fetch_observed_highs(lat: float, lon: float, start_date: str, end_date: str) -> dict:
    """Batch-fetch daily max temps from Open-Meteo archive in °F."""
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
            logger.warning("Open-Meteo %d for (%s,%s) %s..%s",
                           resp.status_code, lat, lon, start_date, end_date)
            return {}
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        return {d: h for d, h in zip(dates, highs) if h is not None}
    except requests.RequestException as e:
        logger.warning("Open-Meteo fetch failed: %s", e)
        return {}


# ─── Snapshot loader (data.db) ────────────────────────────────────────────────

def load_resolved_snapshots(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Pull every (market, last-snapshot) pair from the local DB.

    The walk-forward path uses these as the *anchor* set: question, target
    date, model_prob, and the latest-available yes_price (only used as a
    fallback when Polymarket prices-history is unreachable).
    """
    rows = conn.execute(
        """SELECT market_id, question, city, target_date,
                  yes_price, model_prob, edge, timestamp
           FROM weather_price_snapshots
           WHERE target_date < date('now')
             AND target_date >= date('now', ?)
             AND city IS NOT NULL
             AND model_prob IS NOT NULL
             AND yes_price IS NOT NULL
           ORDER BY market_id, timestamp DESC""",
        (f"-{days} days",),
    ).fetchall()
    last_snap: dict = {}
    for r in rows:
        mid = r["market_id"]
        if mid not in last_snap:
            last_snap[mid] = dict(r)
    return list(last_snap.values())


def load_token_id_map(conn: sqlite3.Connection) -> dict:
    """Best-effort: pull any cached token_id from data.db, if the schema
    has been extended to store it. Older DBs won't have the column —
    that's fine, the backtest still works with the live discovery path."""
    try:
        rows = conn.execute(
            "SELECT market_id, token_id FROM weather_market_tokens"
        ).fetchall()
        return {r["market_id"]: r["token_id"] for r in rows if r["token_id"]}
    except sqlite3.OperationalError:
        return {}


# ─── Walk-forward price replay ────────────────────────────────────────────────

def fetch_walkforward_prices(market: dict, leads_days, token_cache: dict,
                             use_network: bool = True) -> dict:
    """For one market, fetch the YES price at each requested lead time.

    Returns ``{lead_days: yes_price}``. When network access is disabled or
    the Polymarket lookup fails we fall back to the single yes_price stored
    in `weather_price_snapshots`, marking it as lead=0 so the caller can
    still produce a (degraded) report.
    """
    out: dict = {}
    target_date = market.get("target_date")
    yes_price = market.get("yes_price")

    if not use_network or poly_hist is None:
        if yes_price is not None:
            out[0] = float(yes_price)
        return out

    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        if yes_price is not None:
            out[0] = float(yes_price)
        return out

    target_unix = int(target_dt.timestamp())
    market_id = market.get("market_id")
    token_id = token_cache.get(market_id) or poly_hist.find_yes_token_id(market_id)
    if token_id:
        token_cache[market_id] = token_id
        try:
            prices = poly_hist.stitch_walkforward_prices(token_id, target_unix, leads_days)
            for d, p in prices.items():
                out[d] = float(p)
        except Exception as e:
            logger.debug("walkforward fetch failed for %s: %s", market_id, e)

    if not out and yes_price is not None:
        out[0] = float(yes_price)
    return out


# ─── PnL accounting for one (market, lead) pair ───────────────────────────────

def evaluate_bet(yes_price: float, model_prob: float, yes_wins: bool) -> dict:
    """Compute edge, side, raw PnL for a single bet.

    Bet YES when model_prob > yes_price (model says YES is more likely
    than market priced it). Otherwise bet NO. PnL excludes fees — the
    caller layers those on at the threshold-aggregation step.
    """
    edge = model_prob - yes_price
    if abs(edge) < 1e-9:
        return {"edge": 0.0, "side": None, "pnl": None}
    if edge > 0:
        side = "YES"
        pnl = (1.0 - yes_price) if yes_wins else -yes_price
    else:
        side = "NO"
        no_price = 1.0 - yes_price
        pnl = (1.0 - no_price) if not yes_wins else -no_price
    return {"edge": round(edge, 4), "side": side, "pnl": round(pnl, 4)}


# ─── Aggregation + summary ────────────────────────────────────────────────────

def summarize_threshold(bets: list[dict], threshold: float, fee: float) -> dict:
    """One row of the threshold table — counts, win%, gross/net PnL,
    bootstrap Sharpe CI, max drawdown."""
    filtered = [b for b in bets if abs(b["edge"]) >= threshold and b["pnl"] is not None]
    if not filtered:
        return {
            "threshold": threshold, "bets": 0,
        }
    pnls = [b["pnl"] for b in filtered]
    wins = sum(1 for p in pnls if p > 0)
    gross = sum(pnls)
    fees = len(pnls) * fee
    net = gross - fees

    cumulative, running, peak, max_dd = [], 0.0, 0.0, 0.0
    for p in pnls:
        running += p
        cumulative.append(running)
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    sharpe = wcal.bootstrap_sharpe(pnls)
    return {
        "threshold": threshold,
        "bets": len(pnls),
        "wins": wins,
        "win_pct": round(wins / len(pnls) * 100, 1),
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net, 2),
        "avg_pnl": round(gross / len(pnls), 4),
        "net_avg_pnl": round(net / len(pnls), 4),
        "sharpe_per_trade": sharpe,
        "max_drawdown": round(max_dd, 2),
    }


def calibration_block(bets: list[dict]) -> dict:
    """Brier, log-loss, and reliability diagram for the model's predictions.

    The ``outcome`` is YES=1, NO=0 — independent of which side we bet.
    This measures the model's calibration, which is the actual question
    you'd answer to know whether a probabilistic forecast is honest.
    """
    preds = [b["model_prob"] for b in bets if b.get("model_prob") is not None]
    outcomes = [1 if b.get("yes_wins") else 0 for b in bets if b.get("model_prob") is not None]
    return {
        "n": len(preds),
        "brier": round(wcal.brier_score(preds, outcomes) or 0.0, 4) if preds else None,
        "log_loss": round(wcal.log_loss(preds, outcomes) or 0.0, 4) if preds else None,
        "reliability": wcal.reliability_diagram(preds, outcomes, n_bins=10),
    }


def per_lead_breakdown(bets: list[dict], thresholds, fee: float) -> dict:
    by_lead: dict = defaultdict(list)
    for b in bets:
        by_lead[b["lead_days"]].append(b)
    out: dict = {}
    for d, blist in sorted(by_lead.items()):
        out[d] = {
            "n": len(blist),
            "calibration": calibration_block(blist),
            "by_threshold": {f"{t:.0%}": summarize_threshold(blist, t, fee) for t in thresholds},
        }
    return out


def per_station_breakdown(bets: list[dict], threshold: float, fee: float) -> list:
    by_city: dict = defaultdict(list)
    for b in bets:
        by_city[b["city"]].append(b)
    out = []
    for city, blist in sorted(by_city.items()):
        s = summarize_threshold(blist, threshold, fee)
        s["city"] = city
        out.append(s)
    out.sort(key=lambda r: -(r.get("net_pnl") or 0.0))
    return out


# ─── Pretty printing ──────────────────────────────────────────────────────────

def _fmt_sharpe(s: dict) -> str:
    if not s or s.get("point") is None:
        return "—"
    return f"{s['point']:+.2f} [{s['lo']:+.2f}, {s['hi']:+.2f}]"


def print_report(out: dict) -> None:
    print("=" * 88)
    print("WEATHER DASHBOARD — WALK-FORWARD BACKTEST")
    print("=" * 88)
    print(f"Period:          {out['period']['start']} to {out['period']['end']}")
    print(f"Markets:         {out['markets_resolved']}")
    print(f"Cities:          {out['cities']}")
    print(f"Bets (all leads): {out['n_bets']}")
    print(f"Leads tested:    {out['leads_days']}")

    cal = out["overall_calibration"]
    print()
    print(f"--- Overall calibration (n={cal['n']}) ---")
    print(f"Brier score: {cal['brier']}   (lower is better; 0.25 is the constant-0.5 baseline)")
    print(f"Log loss:    {cal['log_loss']}")
    if cal["reliability"]:
        print("Reliability bins (model says X% → actual rate):")
        for bin_ in cal["reliability"]:
            mark = "✓" if abs(bin_["miscalibration"]) < 0.10 else "✗"
            print(f"  {bin_['bin_lo']:.0%}–{bin_['bin_hi']:.0%}: predicted={bin_['avg_predicted']:.1%}  "
                  f"actual={bin_['actual_rate']:.1%}  miscal={bin_['miscalibration']:+.1%}  "
                  f"n={bin_['n']}  {mark}")

    print()
    print("--- Threshold table (gross / net of {:.0%} fees) ---".format(POLYMARKET_FEE))
    print(f"{'Edge ≥':>8} {'Bets':>6} {'Win%':>6} {'Gross':>9} {'Net':>9} {'Net Avg':>9} {'Sharpe (90% CI)':>26} {'Max DD':>8}")
    for k, row in out["by_threshold"].items():
        if not row.get("bets"):
            continue
        print(f"  {k:>6} {row['bets']:>6} {row['win_pct']:>5.1f}% "
              f"{row['gross_pnl']:>+9.2f} {row['net_pnl']:>+9.2f} {row['net_avg_pnl']:>+9.4f} "
              f"{_fmt_sharpe(row['sharpe_per_trade']):>26} {row['max_drawdown']:>+8.2f}")

    print()
    print("--- Per lead-time at edge ≥ 5% ---")
    for d, block in out["by_lead"].items():
        thr = block["by_threshold"].get("5%", {})
        if not thr.get("bets"):
            continue
        print(f"  T-{d}d: bets={thr['bets']}  win%={thr['win_pct']:.1f}  net={thr['net_pnl']:+.2f}  "
              f"sharpe={_fmt_sharpe(thr['sharpe_per_trade'])}")

    print()
    print("--- Per city at edge ≥ 5% ---")
    for r in out["by_station"]:
        if not r.get("bets"):
            continue
        print(f"  {r['city']:>15}: bets={r['bets']:>4}  win%={r['win_pct']:>5.1f}  "
              f"net={r['net_pnl']:>+8.2f}  sharpe={_fmt_sharpe(r['sharpe_per_trade'])}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_backtest(days: int, leads_days, db_path: str,
                 use_network: bool, output_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    logger.info("Loading resolved-market snapshots from %s ...", db_path)
    snaps = load_resolved_snapshots(conn, days)
    logger.info("  %d unique markets in window", len(snaps))

    if not snaps:
        logger.warning("No data — backtest cannot run. Has the snapshot loop been running?")
        return {"period": {}, "markets_resolved": 0, "n_bets": 0,
                "by_threshold": {}, "overall_calibration": {"n": 0}}

    # Group target dates per city for one batched Open-Meteo archive call
    city_dates: dict = defaultdict(set)
    for m in snaps:
        city_dates[m["city"]].add(m["target_date"])

    logger.info("Fetching observed temps from Open-Meteo archive for %d cities ...",
                len(city_dates))
    observed: dict = {}
    for city, dates in sorted(city_dates.items()):
        coords = STATION_COORDS.get(city)
        if not coords:
            logger.info("  %s: no coords mapped — skipping", city)
            continue
        sorted_dates = sorted(dates)
        batch = fetch_observed_highs(coords[0], coords[1], sorted_dates[0], sorted_dates[-1])
        for d in sorted_dates:
            if d in batch:
                observed[(city, d)] = batch[d]
        logger.info("  %s: %d/%d dates resolved", city,
                    sum(1 for d in sorted_dates if (city, d) in observed),
                    len(sorted_dates))
        time.sleep(0.5)

    token_cache = load_token_id_map(conn)
    logger.info("Token id cache primed with %d entries", len(token_cache))

    bets: list[dict] = []
    skipped = {"no_threshold": 0, "no_observed": 0, "no_prices": 0,
               "ambiguous": 0}

    logger.info("Replaying markets (walk-forward) ...")
    for m in snaps:
        threshold = wpure.parse_threshold_for_resolution(m["question"])
        if threshold[0] is None:
            skipped["no_threshold"] += 1
            continue
        key = (m["city"], m["target_date"])
        if key not in observed:
            skipped["no_observed"] += 1
            continue
        observed_high = observed[key]
        yes_wins = wpure.resolve_market(observed_high, threshold)
        if yes_wins is None:
            skipped["ambiguous"] += 1
            continue

        prices = fetch_walkforward_prices(m, leads_days, token_cache, use_network)
        if not prices:
            skipped["no_prices"] += 1
            continue

        for lead, yes_price in prices.items():
            ev = evaluate_bet(yes_price, m["model_prob"], yes_wins)
            bets.append({
                "market_id": m["market_id"],
                "question": m["question"],
                "city": m["city"],
                "target_date": m["target_date"],
                "lead_days": int(lead),
                "yes_price": float(yes_price),
                "model_prob": float(m["model_prob"]),
                "yes_wins": bool(yes_wins),
                "observed_high": observed_high,
                "edge": ev["edge"],
                "side": ev["side"],
                "pnl": ev["pnl"],
            })

    logger.info("  %d (market, lead) pairs evaluated", len(bets))
    logger.info("  skipped: %s", skipped)

    if not bets:
        return {"period": {"start": None, "end": None},
                "markets_resolved": 0, "n_bets": 0,
                "skipped": skipped, "overall_calibration": {"n": 0},
                "by_threshold": {}, "by_lead": {}, "by_station": []}

    out = {
        "period": {
            "start": min(b["target_date"] for b in bets),
            "end":   max(b["target_date"] for b in bets),
        },
        "markets_resolved": len({b["market_id"] for b in bets}),
        "cities": len({b["city"] for b in bets}),
        "n_bets": len(bets),
        "leads_days": list(leads_days),
        "skipped": skipped,
        "overall_calibration": calibration_block(bets),
        "by_threshold": {f"{t:.0%}": summarize_threshold(bets, t, POLYMARKET_FEE)
                          for t in DEFAULT_THRESHOLDS},
        "by_lead": per_lead_breakdown(bets, DEFAULT_THRESHOLDS, POLYMARKET_FEE),
        "by_station": per_station_breakdown(bets, threshold=0.05, fee=POLYMARKET_FEE),
        "bets": bets,
    }

    Path(output_path).write_text(json.dumps(out, indent=2, default=str))
    logger.info("Full results saved to %s", output_path)
    print_report(out)
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=90,
                   help="Lookback window in days (default 90).")
    p.add_argument("--leads", type=str, default=",".join(str(d) for d in DEFAULT_LEADS_DAYS),
                   help="Comma-separated lead times in days (default 1,3,7,14).")
    p.add_argument("--db", type=str, default=str(Path(__file__).parent / "data.db"),
                   help="Path to data.db.")
    p.add_argument("--no-network", action="store_true",
                   help="Skip the Polymarket prices-history fetch (use stored snapshot prices).")
    p.add_argument("--output", type=str,
                   default=str(Path(__file__).parent / "backtest_results.json"),
                   help="Where to write the JSON output.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    leads = tuple(int(s) for s in args.leads.split(",") if s.strip())
    if not leads:
        logger.error("--leads must contain at least one integer")
        return 2
    try:
        run_backtest(days=args.days, leads_days=leads, db_path=args.db,
                     use_network=not args.no_network, output_path=args.output)
        return 0
    except Exception as e:
        logger.exception("Backtest failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
