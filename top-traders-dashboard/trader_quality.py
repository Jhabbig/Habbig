#!/usr/bin/env python3
"""
Trader Quality — sustainable-edge scoring for COPY-TRADING.

The existing scanner is tuned for *catching insiders/sybils*.
This module is the opposite: it ranks wallets that look like genuinely
skilled traders worth following.

Inputs come from the resolved-markets pipeline that the scanner already
runs (`resolved_markets.fetch_resolved_markets` + `fetch_market_trades`),
so we don't double-fetch anything.

For every BUY trade on a resolved market we know:
  - the winning outcome
  - the entry price (= market-implied probability at fill)
  - whether the wallet's outcome won
  - realized PnL: shares*(1-price) on a win, -usd on a loss

Per-wallet aggregates:
  - total_bets, wins, win_rate
  - realized_pnl, roi
  - avg_buy_price
  - per-bet return list (used for Sharpe-like ratio + calibration)
  - calibration_error: |actual - implied| in 5 entry-price bins

Quality score (0-100):
  - Base from win_rate vs implied (calibration)
  - ROI bonus
  - Sharpe-like consistency bonus
  - Sample-size bonus (log-scaled)
  - Hard floor: ≥ MIN_BETS resolved bets, otherwise we report None
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "trader_quality.sqlite3"

# Filters
MIN_TRADE_USD = 50.0
MIN_BETS_FOR_SCORE = 5            # need this many resolved bets before we score
BETS_HISTORY_CAP = 250            # cap stored per-wallet bet history
CALIBRATION_BINS = (0.0, 0.20, 0.40, 0.60, 0.80, 1.0)


# ─── DB helpers ──────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallet_quality (
                address          TEXT PRIMARY KEY,
                pseudonym        TEXT,
                name             TEXT,
                total_bets       INTEGER NOT NULL DEFAULT 0,
                wins             INTEGER NOT NULL DEFAULT 0,
                total_staked     REAL    NOT NULL DEFAULT 0,
                realized_pnl     REAL    NOT NULL DEFAULT 0,
                avg_buy_price    REAL    NOT NULL DEFAULT 0,
                unique_markets   INTEGER NOT NULL DEFAULT 0,
                bets_json        TEXT    NOT NULL DEFAULT '[]',
                markets_json     TEXT    NOT NULL DEFAULT '[]',
                first_seen_ts    INTEGER NOT NULL DEFAULT 0,
                last_updated_ts  INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS quality_processed (
                wallet     TEXT NOT NULL,
                market_id  TEXT NOT NULL,
                PRIMARY KEY (wallet, market_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_q_pnl ON wallet_quality(realized_pnl DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_q_bets ON wallet_quality(total_bets DESC)")


# ─── Per-trade derivation ───────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def derive_bets_from_market(
    trades: list[dict],
    winning_outcome: str,
) -> dict[str, list[dict]]:
    """
    For one resolved market, group BUY trades by wallet and compute:
      {wallet: [{price, size_usd, won, ts, pseudonym, name}, ...]}
    """
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        side = (t.get("side") or "").upper()
        if side and side != "BUY":
            continue
        size = _safe_float(t.get("size"))
        price = _safe_float(t.get("price"))
        if price <= 0 or price >= 1.0:
            continue
        usd = size * price
        if usd < MIN_TRADE_USD:
            continue
        wallet = (t.get("proxyWallet") or t.get("maker_address") or "").lower()
        if not wallet:
            continue
        outcome = t.get("outcome") or ""
        ts = _safe_int(t.get("timestamp"))
        won = 1 if outcome == winning_outcome else 0
        by_wallet[wallet].append({
            "price": price,
            "size_usd": usd,
            "won": won,
            "ts": ts,
            "pseudonym": t.get("pseudonym") or "",
            "name": t.get("name") or "",
        })
    return by_wallet


def _bet_realized_pnl(b: dict) -> float:
    """Realized PnL on a single bet."""
    price = b["price"]
    usd = b["size_usd"]
    if price <= 0:
        return 0.0
    if b["won"]:
        shares = usd / price
        return shares * (1.0 - price)  # collect 1 per share, paid `price` per share
    return -usd


def _bet_return_pct(b: dict) -> float:
    """Return as % of staked capital. +1.0 means doubled, -1.0 means total loss."""
    pnl = _bet_realized_pnl(b)
    return pnl / b["size_usd"] if b["size_usd"] else 0.0


# ─── DB upsert ──────────────────────────────────────────────────────

def upsert_wallet_bets(
    wallet: str,
    market_id: str,
    bets: list[dict],
) -> bool:
    """
    Idempotent merge: if (wallet, market_id) was already processed in a prior
    scan, no-op. Otherwise: append the new bets to bets_json (capped FIFO),
    update aggregates. Returns True if anything was applied.
    """
    if not bets or not wallet or not market_id:
        return False

    init_db()
    wallet = wallet.lower()
    now = int(time.time())

    with _conn() as c:
        # Skip already processed
        existing = c.execute(
            "SELECT 1 FROM quality_processed WHERE wallet = ? AND market_id = ?",
            (wallet, market_id),
        ).fetchone()
        if existing:
            return False

        row = c.execute(
            "SELECT * FROM wallet_quality WHERE address = ?",
            (wallet,),
        ).fetchone()

        n_new = len(bets)
        wins_new = sum(1 for b in bets if b["won"])
        staked_new = sum(b["size_usd"] for b in bets)
        pnl_new = sum(_bet_realized_pnl(b) for b in bets)
        avg_price_new_sum = sum(b["price"] * b["size_usd"] for b in bets)

        first_bet = bets[0]
        pseudonym = first_bet.get("pseudonym", "")
        name = first_bet.get("name", "")

        # Slim bet records for storage (don't store name/pseudonym repeatedly)
        slim_new = [
            {"p": round(b["price"], 4), "u": round(b["size_usd"], 2),
             "w": b["won"], "t": b["ts"], "m": market_id}
            for b in bets
        ]

        if row is None:
            unique_markets = 1
            bets_history = slim_new[-BETS_HISTORY_CAP:]
            markets_set = [market_id]
            avg_price = avg_price_new_sum / staked_new if staked_new else 0
            c.execute("""
                INSERT INTO wallet_quality (
                    address, pseudonym, name,
                    total_bets, wins, total_staked, realized_pnl,
                    avg_buy_price, unique_markets,
                    bets_json, markets_json, first_seen_ts, last_updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                wallet, pseudonym, name,
                n_new, wins_new, staked_new, pnl_new,
                avg_price, unique_markets,
                json.dumps(bets_history), json.dumps(markets_set),
                now, now,
            ))
        else:
            old_total_bets = row["total_bets"]
            old_wins = row["wins"]
            old_staked = row["total_staked"] or 0
            old_pnl = row["realized_pnl"] or 0
            old_avg_price = row["avg_buy_price"] or 0
            try:
                old_history = json.loads(row["bets_json"] or "[]")
            except (ValueError, TypeError):
                old_history = []
            try:
                old_markets = set(json.loads(row["markets_json"] or "[]"))
            except (ValueError, TypeError):
                old_markets = set()

            new_total_bets = old_total_bets + n_new
            new_wins = old_wins + wins_new
            new_staked = old_staked + staked_new
            new_pnl = old_pnl + pnl_new
            # Re-compute weighted avg
            if new_staked > 0:
                new_avg_price = (old_avg_price * old_staked + avg_price_new_sum) / new_staked
            else:
                new_avg_price = old_avg_price

            history = (old_history + slim_new)[-BETS_HISTORY_CAP:]
            old_markets.add(market_id)
            new_unique_markets = len(old_markets)

            c.execute("""
                UPDATE wallet_quality
                   SET total_bets       = ?,
                       wins             = ?,
                       total_staked     = ?,
                       realized_pnl     = ?,
                       avg_buy_price    = ?,
                       unique_markets   = ?,
                       bets_json        = ?,
                       markets_json     = ?,
                       pseudonym        = COALESCE(NULLIF(?, ''), pseudonym),
                       name             = COALESCE(NULLIF(?, ''), name),
                       last_updated_ts  = ?
                 WHERE address = ?
            """, (
                new_total_bets, new_wins, new_staked, new_pnl, new_avg_price,
                new_unique_markets, json.dumps(history), json.dumps(sorted(old_markets)),
                pseudonym, name, now, wallet,
            ))

        c.execute(
            "INSERT OR IGNORE INTO quality_processed (wallet, market_id) VALUES (?, ?)",
            (wallet, market_id),
        )
    return True


# ─── Scoring ────────────────────────────────────────────────────────

def _calibration_error(bets: list[dict]) -> tuple[float, list[dict]]:
    """
    Compare implied probability (entry price) to realized win rate, in 5 bins.
    Returns (mean_abs_error, per_bin_breakdown).
    """
    bins: list[dict] = []
    total_abs_err = 0.0
    bins_with_data = 0

    for i in range(len(CALIBRATION_BINS) - 1):
        lo, hi = CALIBRATION_BINS[i], CALIBRATION_BINS[i + 1]
        in_bin = [b for b in bets if lo <= b["p"] < hi]
        if not in_bin:
            bins.append({"lo": lo, "hi": hi, "count": 0, "implied": (lo + hi) / 2,
                         "actual": 0.0, "error": 0.0})
            continue
        implied = sum(b["p"] for b in in_bin) / len(in_bin)
        actual = sum(b["w"] for b in in_bin) / len(in_bin)
        err = abs(actual - implied)
        bins.append({
            "lo": lo, "hi": hi, "count": len(in_bin),
            "implied": round(implied, 4),
            "actual": round(actual, 4),
            "error": round(err, 4),
        })
        total_abs_err += err
        bins_with_data += 1

    mean_err = total_abs_err / bins_with_data if bins_with_data else 1.0
    return mean_err, bins


def _sharpe_like(bets: list[dict]) -> float:
    """
    Per-bet return ratio: mean / std. Not annualized — purely for ranking
    consistency vs lucky one-shots.
    """
    if len(bets) < 2:
        return 0.0
    returns: list[float] = []
    for b in bets:
        price = b["p"]
        if price <= 0:
            continue
        if b["w"]:
            returns.append((1.0 - price) / price)  # win → (1/price - 1)
        else:
            returns.append(-1.0)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std


def _quality_score(
    total_bets: int,
    win_rate: float,
    avg_buy_price: float,
    roi: float,
    sharpe: float,
    calibration_err: float,
) -> tuple[int, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if total_bets < MIN_BETS_FOR_SCORE:
        return 0, [f"Not enough data ({total_bets} bets)"]

    # Win-rate vs implied probability — the core skill signal
    edge = win_rate - avg_buy_price
    if edge >= 0.10:
        score += 30
        reasons.append(f"Wins {win_rate:.0%} vs implied {avg_buy_price:.0%} (+{edge:.0%} edge)")
    elif edge >= 0.05:
        score += 18
        reasons.append(f"Wins {win_rate:.0%} vs implied {avg_buy_price:.0%} (+{edge:.0%} edge)")
    elif edge >= 0.02:
        score += 8
        reasons.append(f"Slight edge over market ({edge:+.1%})")

    # ROI
    if roi >= 0.50:
        score += 25
        reasons.append(f"ROI {roi:.0%}")
    elif roi >= 0.20:
        score += 15
        reasons.append(f"ROI {roi:.0%}")
    elif roi >= 0.05:
        score += 6
        reasons.append(f"Positive ROI {roi:.0%}")
    elif roi <= -0.30:
        score -= 15  # punish persistent losers

    # Sharpe-like consistency
    if sharpe >= 0.6:
        score += 20
        reasons.append(f"Consistent (Sharpe-like {sharpe:.2f})")
    elif sharpe >= 0.3:
        score += 10
        reasons.append(f"Steady returns (Sharpe-like {sharpe:.2f})")

    # Calibration — well-calibrated traders are reliable
    if calibration_err <= 0.05:
        score += 15
        reasons.append(f"Well calibrated (err {calibration_err:.0%})")
    elif calibration_err <= 0.10:
        score += 6
        reasons.append(f"Reasonably calibrated (err {calibration_err:.0%})")

    # Sample-size bonus (log-scaled)
    sample_bonus = min(15, int(round(math.log10(max(total_bets, 1)) * 8)))
    score += sample_bonus
    if sample_bonus >= 8:
        reasons.append(f"Large track record ({total_bets} bets)")

    score = max(0, min(100, int(round(score))))
    return score, reasons


def score_wallet_quality(address: str) -> dict | None:
    """Compute the quality profile for a single wallet."""
    init_db()
    address = address.lower()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM wallet_quality WHERE address = ?",
            (address,),
        ).fetchone()
    if not row:
        return None
    return _row_to_profile(row)


def _row_to_profile(row: sqlite3.Row) -> dict:
    try:
        bets = json.loads(row["bets_json"] or "[]")
    except (ValueError, TypeError):
        bets = []

    total_bets = row["total_bets"]
    wins = row["wins"]
    win_rate = wins / total_bets if total_bets else 0.0
    staked = row["total_staked"] or 0
    pnl = row["realized_pnl"] or 0
    roi = pnl / staked if staked else 0.0
    avg_price = row["avg_buy_price"] or 0

    cal_err, cal_bins = _calibration_error(bets) if bets else (1.0, [])
    sharpe = _sharpe_like(bets)
    score, reasons = _quality_score(total_bets, win_rate, avg_price, roi, sharpe, cal_err)

    return {
        "address": row["address"],
        "pseudonym": row["pseudonym"] or "",
        "name": row["name"] or "",
        "total_bets": total_bets,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "total_staked": round(staked, 2),
        "realized_pnl": round(pnl, 2),
        "roi": round(roi, 4),
        "avg_buy_price": round(avg_price, 4),
        "unique_markets": row["unique_markets"],
        "sharpe_like": round(sharpe, 4),
        "calibration_error": round(cal_err, 4),
        "calibration_bins": cal_bins,
        "quality_score": score,
        "reasons": reasons,
        "first_seen_ts": row["first_seen_ts"],
        "last_updated_ts": row["last_updated_ts"],
    }


def top_quality_traders(
    limit: int = 50,
    min_bets: int = MIN_BETS_FOR_SCORE,
    exclude_addresses: set[str] | None = None,
    min_roi: float | None = None,
) -> list[dict]:
    init_db()
    excl = {a.lower() for a in (exclude_addresses or set())}
    with _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM wallet_quality
             WHERE total_bets >= ?
             ORDER BY realized_pnl DESC
             LIMIT ?
            """,
            (min_bets, limit * 6),
        ).fetchall()

    profiles = []
    for r in rows:
        if r["address"] in excl:
            continue
        prof = _row_to_profile(r)
        if min_roi is not None and prof["roi"] < min_roi:
            continue
        if prof["quality_score"] <= 0:
            continue
        profiles.append(prof)

    profiles.sort(key=lambda p: (p["quality_score"], p["realized_pnl"]), reverse=True)
    return profiles[:limit]


def quality_summary() -> dict:
    init_db()
    with _conn() as c:
        row = c.execute("""
            SELECT COUNT(*) AS total_wallets,
                   SUM(total_bets) AS total_bets,
                   SUM(wins) AS total_wins,
                   SUM(realized_pnl) AS total_pnl,
                   MAX(last_updated_ts) AS last_updated
              FROM wallet_quality
        """).fetchone()
        eligible = c.execute(
            "SELECT COUNT(*) AS n FROM wallet_quality WHERE total_bets >= ?",
            (MIN_BETS_FOR_SCORE,),
        ).fetchone()
    return {
        "total_wallets": row["total_wallets"] or 0,
        "total_bets": row["total_bets"] or 0,
        "total_wins": row["total_wins"] or 0,
        "total_pnl": round(row["total_pnl"] or 0, 2),
        "wallets_with_min_bets": eligible["n"] if eligible else 0,
        "min_bets": MIN_BETS_FOR_SCORE,
        "last_updated": row["last_updated"] or 0,
    }


# ─── Pipeline entry point ───────────────────────────────────────────

def run_quality_scan(
    resolved_markets: list[dict],
    market_trades: dict[str, list[dict]],
    exclude_clusters: set[str] | None = None,
) -> dict:
    """
    Process a batch of resolved markets + their trades and persist to the
    quality DB. Returns a summary + the top quality traders.

    Inputs come from suspicious_trades.run_scanner so we don't refetch.
      - resolved_markets: list with .condition_id, .winning_outcome
      - market_trades: {market_id: [trades]} for any subset of those markets
      - exclude_clusters: set of wallets that are in sybil clusters
                          (excluded from copy-trade rankings even if PnL is high)
    """
    init_db()

    market_winner_map = {
        m.get("condition_id") or "": m.get("winning_outcome") or ""
        for m in resolved_markets
        if m.get("condition_id") and m.get("winning_outcome")
    }

    bets_applied = 0
    wallets_touched: set[str] = set()
    markets_processed = 0

    for market_id, trades in market_trades.items():
        winner = market_winner_map.get(market_id)
        if not winner or not trades:
            continue
        markets_processed += 1
        by_wallet = derive_bets_from_market(trades, winner)
        for wallet, bets in by_wallet.items():
            if upsert_wallet_bets(wallet, market_id, bets):
                bets_applied += len(bets)
                wallets_touched.add(wallet)

    top = top_quality_traders(limit=50, exclude_addresses=exclude_clusters)
    summary = quality_summary()

    return {
        "markets_processed_this_scan": markets_processed,
        "bets_applied_this_scan": bets_applied,
        "wallets_touched_this_scan": len(wallets_touched),
        "summary": summary,
        "top_traders": top,
    }


if __name__ == "__main__":
    # Smoke test with synthetic resolved-market trades
    fake_markets = [
        {"condition_id": "m1", "winning_outcome": "Yes"},
        {"condition_id": "m2", "winning_outcome": "No"},
    ]
    fake_trades = {
        "m1": [
            # Skilled wallet: bought Yes at 30%, won
            {"proxyWallet": "0xskill", "side": "BUY", "outcome": "Yes",
             "size": 1000, "price": 0.30, "timestamp": 1700000000, "pseudonym": "Skilled"},
            # Unskilled wallet: bought No at 70%, lost
            {"proxyWallet": "0xnoob", "side": "BUY", "outcome": "No",
             "size": 1000, "price": 0.70, "timestamp": 1700000000, "pseudonym": "Noob"},
        ],
        "m2": [
            {"proxyWallet": "0xskill", "side": "BUY", "outcome": "No",
             "size": 1500, "price": 0.40, "timestamp": 1700100000, "pseudonym": "Skilled"},
            {"proxyWallet": "0xnoob", "side": "BUY", "outcome": "Yes",
             "size": 800, "price": 0.60, "timestamp": 1700100000, "pseudonym": "Noob"},
        ],
    }
    result = run_quality_scan(fake_markets, fake_trades)
    print("Summary:", result["summary"])
    print("Markets processed:", result["markets_processed_this_scan"])
    print("Top traders:")
    for t in result["top_traders"]:
        print(f"  [{t['quality_score']}] {t['address'][:10]} {t['pseudonym']:>10} "
              f"bets={t['total_bets']} wr={t['win_rate']:.0%} "
              f"pnl=${t['realized_pnl']:.0f} roi={t['roi']:.0%}")
