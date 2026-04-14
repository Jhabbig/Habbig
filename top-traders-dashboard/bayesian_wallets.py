#!/usr/bin/env python3
"""
Bayesian Wallet Skill Estimation.

For every wallet that bets on long-shots we keep a Beta(α, β) distribution
representing our belief about its true "edge" — i.e. the probability that a
given long-shot bet from this wallet ends up winning.

Why Beta?
  - Conjugate prior to Bernoulli, so updates are O(1) closed-form.
  - Supports tiny sample sizes gracefully (a wallet that's 1-for-1 doesn't get
    a frequentist 100% — the prior pulls it back to baseline).
  - Lets us compute things like P(edge > baseline) directly from the posterior.

Storage: a single SQLite file (`bayesian_wallets.db`) so the prior persists
between scans without needing to re-process every resolved market each time.

Pipeline integration:
  1. resolved_markets.run_retroactive_scan() returns long-shot wins per wallet.
  2. We pass those wins (and an estimated bet count) into update_from_winners().
  3. score_wallet() returns posterior mean + a high-confidence threshold flag.
  4. The suspicious_trades scanner adds Bayesian score on top of its rules.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "bayesian_wallets.db"

# Prior parameters: Beta(α₀, β₀).
# A long-shot bet (price ≤ 25%) wins ~12% of the time on average across
# Polymarket — that's our baseline. Pseudo-counts of (3, 22) give us a
# reasonably concentrated prior centered near 12% that updates quickly.
PRIOR_ALPHA = 3.0
PRIOR_BETA = 22.0
BASELINE_LONGSHOT_WIN_RATE = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)  # ~0.12

# Threshold for "highly likely to have edge": posterior P(edge > baseline)
HIGH_CONFIDENCE_THRESHOLD = 0.95


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create schema if it doesn't exist."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_stats (
                address              TEXT PRIMARY KEY,
                pseudonym            TEXT,
                name                 TEXT,
                total_longshot_bets  INTEGER NOT NULL DEFAULT 0,
                longshot_wins        INTEGER NOT NULL DEFAULT 0,
                bayesian_alpha       REAL    NOT NULL DEFAULT 3.0,
                bayesian_beta        REAL    NOT NULL DEFAULT 22.0,
                total_realized_usd   REAL    NOT NULL DEFAULT 0,
                total_staked_usd     REAL    NOT NULL DEFAULT 0,
                first_seen_ts        INTEGER NOT NULL DEFAULT 0,
                last_updated_ts      INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_markets (
                wallet      TEXT NOT NULL,
                market_id   TEXT NOT NULL,
                won         INTEGER NOT NULL,
                processed_ts INTEGER NOT NULL,
                PRIMARY KEY (wallet, market_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_alpha ON wallet_stats(bayesian_alpha DESC)")
        conn.commit()
    finally:
        conn.close()


# ─── Beta distribution math ──────────────────────────────────────────

def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """
    I_x(a,b) — regularized incomplete beta function.

    Uses a continued-fraction expansion (Numerical Recipes §6.4). Stable for
    the small (a,b) values we deal with here. Lets us compute the Beta CDF
    without scipy.
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    # Symmetry: avoids slow convergence for x close to 1
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_incomplete_beta(1 - x, b, a)

    log_prefix = a * math.log(x) + b * math.log(1 - x) - _log_beta(a, b) - math.log(a)

    # Continued fraction (Lentz's method)
    eps = 1e-12
    fpmin = 1e-300
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        # Even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        # Odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return math.exp(log_prefix) * h


def beta_cdf(x: float, alpha: float, beta: float) -> float:
    """CDF of Beta(α, β) at x."""
    return _regularized_incomplete_beta(x, alpha, beta)


def prob_edge_above_baseline(alpha: float, beta: float, baseline: float = BASELINE_LONGSHOT_WIN_RATE) -> float:
    """P(p > baseline) where p ~ Beta(alpha, beta)."""
    return 1.0 - beta_cdf(baseline, alpha, beta)


def posterior_mean(alpha: float, beta: float) -> float:
    return alpha / (alpha + beta)


def posterior_variance(alpha: float, beta: float) -> float:
    s = alpha + beta
    return (alpha * beta) / (s * s * (s + 1))


# ─── Public API ──────────────────────────────────────────────────────

def get_wallet_stats(address: str) -> dict | None:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM wallet_stats WHERE address = ?",
            (address.lower(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_wallet(
    address: str,
    won_bet: bool,
    realized_usd: float = 0.0,
    staked_usd: float = 0.0,
    market_id: str | None = None,
    pseudonym: str | None = None,
    name: str | None = None,
) -> None:
    """
    Idempotent update: if (wallet, market_id) was already processed, this is a
    no-op. Otherwise we increment the relevant Beta parameter and bookkeeping.
    """
    init_db()
    address = address.lower()
    now = int(time.time())

    conn = _connect()
    try:
        # Skip if we've already counted this wallet/market combo
        if market_id:
            existing = conn.execute(
                "SELECT 1 FROM processed_markets WHERE wallet = ? AND market_id = ?",
                (address, market_id),
            ).fetchone()
            if existing:
                return

        row = conn.execute(
            "SELECT * FROM wallet_stats WHERE address = ?", (address,),
        ).fetchone()

        if row is None:
            alpha = PRIOR_ALPHA + (1.0 if won_bet else 0.0)
            beta = PRIOR_BETA + (0.0 if won_bet else 1.0)
            conn.execute("""
                INSERT INTO wallet_stats (
                    address, pseudonym, name,
                    total_longshot_bets, longshot_wins,
                    bayesian_alpha, bayesian_beta,
                    total_realized_usd, total_staked_usd,
                    first_seen_ts, last_updated_ts
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """, (
                address, pseudonym or "", name or "",
                1 if won_bet else 0,
                alpha, beta,
                realized_usd, staked_usd,
                now, now,
            ))
        else:
            new_alpha = row["bayesian_alpha"] + (1.0 if won_bet else 0.0)
            new_beta = row["bayesian_beta"] + (0.0 if won_bet else 1.0)
            new_pseudonym = pseudonym or row["pseudonym"] or ""
            new_name = name or row["name"] or ""
            conn.execute("""
                UPDATE wallet_stats
                SET total_longshot_bets = total_longshot_bets + 1,
                    longshot_wins       = longshot_wins + ?,
                    bayesian_alpha      = ?,
                    bayesian_beta       = ?,
                    total_realized_usd  = total_realized_usd + ?,
                    total_staked_usd    = total_staked_usd + ?,
                    pseudonym           = ?,
                    name                = ?,
                    last_updated_ts     = ?
                WHERE address = ?
            """, (
                1 if won_bet else 0,
                new_alpha, new_beta,
                realized_usd, staked_usd,
                new_pseudonym, new_name,
                now, address,
            ))

        if market_id:
            conn.execute("""
                INSERT OR IGNORE INTO processed_markets (wallet, market_id, won, processed_ts)
                VALUES (?, ?, ?, ?)
            """, (address, market_id, 1 if won_bet else 0, now))

        conn.commit()
    finally:
        conn.close()


def update_from_winners(winners: list[dict], all_market_trades: dict[str, list[dict]] | None = None) -> dict:
    """
    Apply retroactive scan results to the Bayesian model.

    `winners` is the list returned by resolved_markets.find_longshot_winners().
    Each item is a single long-shot WIN by a wallet on a market.

    If `all_market_trades` is provided as a {market_id: [trades]} map, we also
    register losing long-shot bets so the Beta beta-parameter increments
    correctly. Without it, we only update on wins (which biases scores upward
    but is still informative for ranking).
    """
    init_db()

    # 1. Apply wins (one Beta α-bump per (wallet, market))
    win_keys = set()
    for w in winners:
        addr = (w.get("wallet") or "").lower()
        mid = w.get("market_id") or ""
        if not addr or not mid:
            continue
        upsert_wallet(
            address=addr,
            won_bet=True,
            realized_usd=float(w.get("realized_profit", 0) or 0),
            staked_usd=float(w.get("size_usd", 0) or 0),
            market_id=mid,
            pseudonym=w.get("pseudonym") or "",
            name=w.get("name") or "",
        )
        win_keys.add((addr, mid))

    losses_processed = 0
    if all_market_trades:
        # 2. Apply losses for wallets that took the OTHER side at long odds
        from resolved_markets import LONGSHOT_PRICE_MAX, LONGSHOT_PRICE_MIN, LONGSHOT_MIN_USD

        # Build a map of winning_outcome per market from the winners list
        winning_outcome_by_market: dict[str, str] = {}
        for w in winners:
            mid = w.get("market_id") or ""
            if mid and mid not in winning_outcome_by_market:
                winning_outcome_by_market[mid] = w.get("outcome", "")

        for market_id, trades in all_market_trades.items():
            winning_outcome = winning_outcome_by_market.get(market_id)
            if not winning_outcome:
                continue
            seen_loss_keys: set[tuple[str, str]] = set()
            for t in trades:
                outcome = t.get("outcome", "")
                if outcome == winning_outcome:
                    continue  # this would be a win, not a loss
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
                addr = (t.get("proxyWallet") or t.get("maker_address") or "").lower()
                if not addr:
                    continue
                key = (addr, market_id)
                if key in win_keys or key in seen_loss_keys:
                    continue
                seen_loss_keys.add(key)
                upsert_wallet(
                    address=addr,
                    won_bet=False,
                    realized_usd=-usd,  # losing the stake
                    staked_usd=usd,
                    market_id=market_id,
                    pseudonym=t.get("pseudonym") or "",
                    name=t.get("name") or "",
                )
                losses_processed += 1

    return {
        "wins_processed": len(win_keys),
        "losses_processed": losses_processed,
    }


def score_wallet(address: str) -> dict | None:
    """
    Compute Bayesian metrics for a single wallet.

    Returns:
      {
        'address': str,
        'longshot_bets': int,
        'longshot_wins': int,
        'posterior_mean': float,           # E[edge]
        'posterior_std': float,            # √Var[edge]
        'prob_above_baseline': float,      # P(edge > 12%)
        'high_confidence': bool,           # P(edge > baseline) >= 0.95
        'edge_lift': float,                # posterior_mean / baseline
      }
    """
    row = get_wallet_stats(address)
    if not row:
        return None

    alpha = float(row["bayesian_alpha"])
    beta_param = float(row["bayesian_beta"])
    mean = posterior_mean(alpha, beta_param)
    var = posterior_variance(alpha, beta_param)
    p_above = prob_edge_above_baseline(alpha, beta_param)

    return {
        "address": row["address"],
        "name": row["name"],
        "pseudonym": row["pseudonym"],
        "longshot_bets": row["total_longshot_bets"],
        "longshot_wins": row["longshot_wins"],
        "bayesian_alpha": round(alpha, 3),
        "bayesian_beta": round(beta_param, 3),
        "posterior_mean": round(mean, 4),
        "posterior_std": round(math.sqrt(var), 4),
        "prob_above_baseline": round(p_above, 4),
        "high_confidence": p_above >= HIGH_CONFIDENCE_THRESHOLD,
        "edge_lift": round(mean / BASELINE_LONGSHOT_WIN_RATE, 2),
        "total_realized_usd": round(row["total_realized_usd"], 2),
        "total_staked_usd": round(row["total_staked_usd"], 2),
        "last_updated_ts": row["last_updated_ts"],
    }


def top_wallets_by_edge(limit: int = 50, min_bets: int = 3) -> list[dict]:
    """
    Return wallets ranked by P(edge > baseline), filtered to those with at
    least `min_bets` resolved long-shots so we don't surface 1-bet flukes.
    """
    init_db()
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT * FROM wallet_stats
            WHERE total_longshot_bets >= ?
            ORDER BY bayesian_alpha DESC
            LIMIT ?
        """, (min_bets, limit * 4)).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        alpha = float(row["bayesian_alpha"])
        beta_param = float(row["bayesian_beta"])
        mean = posterior_mean(alpha, beta_param)
        p_above = prob_edge_above_baseline(alpha, beta_param)
        scored.append({
            "address": row["address"],
            "name": row["name"],
            "pseudonym": row["pseudonym"],
            "longshot_bets": row["total_longshot_bets"],
            "longshot_wins": row["longshot_wins"],
            "posterior_mean": round(mean, 4),
            "prob_above_baseline": round(p_above, 4),
            "high_confidence": p_above >= HIGH_CONFIDENCE_THRESHOLD,
            "edge_lift": round(mean / BASELINE_LONGSHOT_WIN_RATE, 2),
            "total_realized_usd": round(row["total_realized_usd"], 2),
        })

    scored.sort(key=lambda x: (x["prob_above_baseline"], x["posterior_mean"]), reverse=True)
    return scored[:limit]


def stats_summary() -> dict:
    init_db()
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_wallets,
                SUM(total_longshot_bets) AS total_bets,
                SUM(longshot_wins) AS total_wins,
                SUM(total_realized_usd) AS total_realized,
                MAX(last_updated_ts) AS last_updated
            FROM wallet_stats
        """).fetchone()

        high_conf = conn.execute("""
            SELECT COUNT(*) AS n FROM wallet_stats
            WHERE total_longshot_bets >= 3
        """).fetchone()
    finally:
        conn.close()

    if not row or not row["total_wallets"]:
        return {
            "total_wallets": 0,
            "total_bets": 0,
            "total_wins": 0,
            "total_realized": 0,
            "wallets_with_3plus_bets": 0,
            "baseline_win_rate": BASELINE_LONGSHOT_WIN_RATE,
            "last_updated": 0,
        }

    return {
        "total_wallets": row["total_wallets"] or 0,
        "total_bets": row["total_bets"] or 0,
        "total_wins": row["total_wins"] or 0,
        "total_realized": round(row["total_realized"] or 0, 2),
        "wallets_with_3plus_bets": high_conf["n"] if high_conf else 0,
        "baseline_win_rate": BASELINE_LONGSHOT_WIN_RATE,
        "last_updated": row["last_updated"] or 0,
    }


if __name__ == "__main__":
    init_db()
    print(f"Bayesian wallet DB at: {DB_PATH}")
    print(f"Prior: Beta({PRIOR_ALPHA}, {PRIOR_BETA}) — baseline {BASELINE_LONGSHOT_WIN_RATE:.1%}")
    print(f"\nCurrent state: {stats_summary()}")

    # Sanity check the math
    print("\nQuick sanity check:")
    print(f"  P(edge > 12% | 0 wins / 0 bets) = {prob_edge_above_baseline(PRIOR_ALPHA, PRIOR_BETA):.3f} (should be ~0.5)")
    print(f"  P(edge > 12% | 5 wins / 5 bets) = {prob_edge_above_baseline(PRIOR_ALPHA + 5, PRIOR_BETA):.3f}")
    print(f"  P(edge > 12% | 10 wins / 10 bets) = {prob_edge_above_baseline(PRIOR_ALPHA + 10, PRIOR_BETA):.3f}")
    print(f"  P(edge > 12% | 0 wins / 10 bets) = {prob_edge_above_baseline(PRIOR_ALPHA, PRIOR_BETA + 10):.3f}")
