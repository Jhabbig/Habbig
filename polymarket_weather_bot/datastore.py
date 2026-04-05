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
                    confidence TEXT
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
                    status TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date TEXT PRIMARY KEY,
                    realized_pnl REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    signals_count INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_signals_condition ON signals(condition_id);
            """)

    def log_signal(self, signal: Signal) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO signals
                   (timestamp, condition_id, question, city, station_icao, target_date,
                    forecast_mean, forecast_std, forecast_source,
                    model_prob, market_prob, edge, action, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, signal.market.condition_id, signal.market.question,
                 signal.market.city, signal.market.station_icao,
                 signal.market.target_date.isoformat() if signal.market.target_date else None,
                 signal.forecast.mean_temp_f, signal.forecast.std_temp_f,
                 signal.forecast.source, signal.model_prob, signal.market_prob,
                 signal.edge, signal.action, signal.confidence),
            )

    def log_trade(self, signal: Signal, position: PositionSize,
                  paper_mode: bool = True, order_id: str = "", status: str = "filled") -> None:
        now = datetime.now(timezone.utc).isoformat()
        side = "YES" if signal.action == "BUY_YES" else "NO"
        price = signal.market_prob if side == "YES" else (1.0 - signal.market_prob)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, condition_id, token_id, question, city, action, side,
                    amount, price, kelly_fraction, edge, paper_mode, order_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, signal.market.condition_id, signal.market.token_id,
                 signal.market.question, signal.market.city, signal.action, side,
                 position.amount, price, position.kelly_fraction, signal.edge,
                 1 if paper_mode else 0, order_id, status),
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
