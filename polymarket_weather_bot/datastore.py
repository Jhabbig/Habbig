"""SQLite datastore — log all signals and trades."""

from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

from config import Config
from edge_calculator import Signal
from risk_manager import PositionSize

logger = logging.getLogger(__name__)


class DataStore:
    """SQLite-backed storage for signals, trades, and PnL tracking."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or Config.DB_PATH
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    question TEXT,
                    city TEXT,
                    station_icao TEXT,
                    target_date TEXT,
                    forecast_mean REAL,
                    forecast_std REAL,
                    forecast_source TEXT,
                    model_prob REAL,
                    market_prob REAL,
                    edge REAL,
                    action TEXT,
                    confidence TEXT,
                    platform TEXT DEFAULT 'polymarket'
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT,
                    question TEXT,
                    city TEXT,
                    action TEXT,
                    side TEXT,
                    amount REAL,
                    price REAL,
                    kelly_fraction REAL,
                    edge REAL,
                    paper_mode INTEGER,
                    order_id TEXT,
                    status TEXT,
                    platform TEXT DEFAULT 'polymarket'
                );

                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date TEXT PRIMARY KEY,
                    realized_pnl REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    signals_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS calibration (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id    TEXT NOT NULL,
                    platform        TEXT DEFAULT 'polymarket',
                    city            TEXT,
                    target_date     TEXT,
                    model_prob      REAL,
                    market_prob     REAL,
                    outcome         INTEGER,
                    resolved_at     TEXT,
                    prob_method     TEXT DEFAULT 'gaussian'
                );

                CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_signals_condition ON signals(condition_id);
                CREATE INDEX IF NOT EXISTS idx_calibration_cond ON calibration(condition_id);
            """)
            # Migrate existing databases: add platform column if missing
            try:
                conn.execute("SELECT platform FROM signals LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE signals ADD COLUMN platform TEXT DEFAULT 'polymarket'")
            try:
                conn.execute("SELECT platform FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE trades ADD COLUMN platform TEXT DEFAULT 'polymarket'")

    def log_signal(self, signal: Signal) -> None:
        now = datetime.now(timezone.utc).isoformat()
        platform = getattr(signal.market, "platform", "polymarket")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO signals
                   (timestamp, condition_id, question, city, station_icao, target_date,
                    forecast_mean, forecast_std, forecast_source,
                    model_prob, market_prob, edge, action, confidence, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, signal.market.condition_id, signal.market.question,
                 signal.market.city, signal.market.station_icao,
                 signal.market.target_date.isoformat() if signal.market.target_date else None,
                 signal.forecast.mean_temp_f, signal.forecast.std_temp_f,
                 signal.forecast.source, signal.model_prob, signal.market_prob,
                 signal.edge, signal.action, signal.confidence, platform),
            )

    def log_trade(self, signal: Signal, position: PositionSize,
                  paper_mode: bool = True, order_id: str = "", status: str = "filled") -> None:
        now = datetime.now(timezone.utc).isoformat()
        side = "YES" if signal.action == "BUY_YES" else "NO"
        price = signal.market_prob if side == "YES" else (1.0 - signal.market_prob)
        platform = getattr(signal.market, "platform", "polymarket")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, condition_id, token_id, question, city, action, side,
                    amount, price, kelly_fraction, edge, paper_mode, order_id, status,
                    platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, signal.market.condition_id, signal.market.token_id,
                 signal.market.question, signal.market.city, signal.action, side,
                 position.amount, price, position.kelly_fraction, signal.edge,
                 1 if paper_mode else 0, order_id, status, platform),
            )

    def get_today_stats(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute(
                "SELECT COUNT(*) as cnt FROM signals WHERE timestamp LIKE ?",
                (f"{today}%",)).fetchone()
            trades = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total_amount FROM trades WHERE timestamp LIKE ?",
                (f"{today}%",)).fetchone()
            actionable = conn.execute(
                "SELECT COUNT(*) as cnt FROM signals WHERE timestamp LIKE ? AND action != 'NO_TRADE'",
                (f"{today}%",)).fetchone()
        return {
            "date": today,
            "signals_total": signals["cnt"],
            "signals_actionable": actionable["cnt"],
            "trades_count": trades["cnt"],
            "total_amount": trades["total_amount"],
        }

    def get_recent_trades(self, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 50) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_pnl_summary(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM trades").fetchone()
            by_action = conn.execute(
                "SELECT action, COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM trades GROUP BY action").fetchall()
            by_city = conn.execute(
                "SELECT city, COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM trades GROUP BY city").fetchall()
        return {
            "total_trades": total["cnt"],
            "total_wagered": total["total"],
            "by_action": {r["action"]: {"count": r["cnt"], "total": r["total"]} for r in by_action},
            "by_city": {r["city"]: {"count": r["cnt"], "total": r["total"]} for r in by_city},
        }

    # ── Calibration / Brier score ────────────────────────────────────────────

    def log_calibration(self, signal: Signal, outcome: int) -> None:
        """Record a resolved signal for calibration tracking.

        Args:
            signal: The original trading signal.
            outcome: 1 if YES resolved true, 0 if NO.
        """
        now = datetime.now(timezone.utc).isoformat()
        platform = getattr(signal.market, "platform", "polymarket")
        prob_method = getattr(signal, "prob_method", "gaussian")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO calibration
                   (condition_id, platform, city, target_date,
                    model_prob, market_prob, outcome, resolved_at, prob_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal.market.condition_id, platform, signal.market.city,
                 signal.market.target_date.isoformat() if signal.market.target_date else None,
                 signal.model_prob, signal.market_prob,
                 outcome, now, prob_method),
            )

    def get_brier_score(self) -> dict:
        """Compute Brier score and calibration stats from resolved signals.

        Brier score = mean((model_prob - outcome)^2).
        Lower is better; 0.25 = coin flip, 0.0 = perfect.

        Also computes the market's Brier score for comparison — if ours is
        lower, the model adds value over market prices.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT model_prob, market_prob, outcome, prob_method, platform "
                "FROM calibration WHERE outcome IS NOT NULL"
            ).fetchall()

        if not rows:
            return {"n": 0, "brier_model": None, "brier_market": None, "edge_vs_market": None}

        n = len(rows)
        brier_model = sum((r["model_prob"] - r["outcome"]) ** 2 for r in rows) / n
        brier_market = sum((r["market_prob"] - r["outcome"]) ** 2 for r in rows) / n

        # Breakdown by method
        by_method = {}
        for r in rows:
            method = r["prob_method"] or "gaussian"
            if method not in by_method:
                by_method[method] = {"n": 0, "sum_sq": 0.0}
            by_method[method]["n"] += 1
            by_method[method]["sum_sq"] += (r["model_prob"] - r["outcome"]) ** 2

        method_scores = {}
        for method, data in by_method.items():
            method_scores[method] = {
                "n": data["n"],
                "brier": data["sum_sq"] / data["n"],
            }

        # Breakdown by platform
        by_platform = {}
        for r in rows:
            plat = r["platform"] or "polymarket"
            if plat not in by_platform:
                by_platform[plat] = {"n": 0, "sum_sq": 0.0}
            by_platform[plat]["n"] += 1
            by_platform[plat]["sum_sq"] += (r["model_prob"] - r["outcome"]) ** 2

        platform_scores = {}
        for plat, data in by_platform.items():
            platform_scores[plat] = {
                "n": data["n"],
                "brier": data["sum_sq"] / data["n"],
            }

        # Calibration buckets: group predictions into 10% bins
        buckets = {}
        for r in rows:
            bucket = min(int(r["model_prob"] * 10), 9)  # 0-9
            label = f"{bucket*10}-{bucket*10+10}%"
            if label not in buckets:
                buckets[label] = {"n": 0, "sum_prob": 0.0, "sum_outcome": 0.0}
            buckets[label]["n"] += 1
            buckets[label]["sum_prob"] += r["model_prob"]
            buckets[label]["sum_outcome"] += r["outcome"]

        calibration_buckets = {}
        for label, data in sorted(buckets.items()):
            calibration_buckets[label] = {
                "n": data["n"],
                "avg_predicted": data["sum_prob"] / data["n"],
                "avg_actual": data["sum_outcome"] / data["n"],
            }

        return {
            "n": n,
            "brier_model": round(brier_model, 4),
            "brier_market": round(brier_market, 4),
            "edge_vs_market": round(brier_market - brier_model, 4),
            "by_method": method_scores,
            "by_platform": platform_scores,
            "calibration_buckets": calibration_buckets,
        }
