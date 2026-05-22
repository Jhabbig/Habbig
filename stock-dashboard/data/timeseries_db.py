#!/usr/bin/env python3
"""
Time-Series Database for Stock Trading Data

Extends the gateway DB with:
- OHLCV bars (intraday + daily)
- Stock trades (entry/exit with reasons)
- Performance metrics (Sharpe, drawdown, etc)
- Quote cache (bid/ask)
- Technical indicator cache
"""

import sqlite3
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Any

log = logging.getLogger("stock_trading_db")

DB_PATH = Path(__file__).parent.parent / "trading_data.db"
_lock = threading.RLock()

STOCK_TRADING_SCHEMA = """
-- OHLCV bars: intraday (1m, 5m, 1h) and daily
CREATE TABLE IF NOT EXISTS ohlcv_bars (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    interval          TEXT NOT NULL,
    timestamp         INTEGER NOT NULL,
    open              REAL NOT NULL,
    high              REAL NOT NULL,
    low               REAL NOT NULL,
    close             REAL NOT NULL,
    volume            INTEGER NOT NULL,
    vwap              REAL,
    created_at        INTEGER NOT NULL,
    UNIQUE(ticker, interval, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_ts ON ohlcv_bars(ticker, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ohlcv_interval ON ohlcv_bars(ticker, interval, timestamp DESC);

-- Stock trades with entry/exit signals and reasoning
CREATE TABLE IF NOT EXISTS stock_trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER,
    ticker                  TEXT NOT NULL,
    side                    TEXT NOT NULL,  -- BUY or SHORT
    entry_date              INTEGER NOT NULL,
    entry_price             REAL NOT NULL,
    entry_reason            TEXT NOT NULL,  -- ML signal, technical, sentiment, etc
    signals_present         TEXT,            -- JSON array of active signals
    confidence_score        REAL,            -- 0-1
    position_size_shares    INTEGER,
    position_size_pct       REAL,            -- % of portfolio
    risk_reward_ratio       REAL,            -- expected RR
    target_price            REAL,
    stop_price              REAL,
    exit_date               INTEGER,
    exit_price              REAL,
    exit_reason             TEXT,
    hold_duration_minutes   INTEGER,
    realized_pnl            REAL,
    realized_pnl_pct        REAL,
    slippage_bps            INTEGER,        -- basis points
    commissions             REAL,
    status                  TEXT NOT NULL DEFAULT 'open',  -- open, closed, cancelled
    notes                   TEXT,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_user_date ON stock_trades(user_id, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON stock_trades(ticker, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON stock_trades(status);

-- Daily/weekly/monthly performance metrics
CREATE TABLE IF NOT EXISTS performance_metrics (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER,
    date                    INTEGER NOT NULL,
    period                  TEXT NOT NULL,  -- daily, weekly, monthly
    trades_count            INTEGER,
    winning_trades          INTEGER,
    losing_trades           INTEGER,
    win_rate                REAL,
    avg_winner              REAL,
    avg_loser               REAL,
    profit_factor           REAL,
    gross_profit            REAL,
    gross_loss              REAL,
    net_profit              REAL,
    daily_return_pct        REAL,
    cumulative_return_pct   REAL,
    sharpe_ratio            REAL,
    sortino_ratio           REAL,
    calmar_ratio            REAL,
    max_drawdown_pct        REAL,
    max_drawdown_date       INTEGER,
    recovery_date           INTEGER,
    volatility_pct          REAL,
    recovery_factor         REAL,
    created_at              INTEGER NOT NULL,
    UNIQUE(user_id, date, period)
);
CREATE INDEX IF NOT EXISTS idx_metrics_user_date ON performance_metrics(user_id, date DESC);

-- Quote cache: bid/ask/last for microstructure analysis
CREATE TABLE IF NOT EXISTS quote_cache (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    timestamp         INTEGER NOT NULL,
    bid               REAL,
    ask               REAL,
    bid_size          INTEGER,
    ask_size          INTEGER,
    last_price        REAL,
    last_size         INTEGER,
    spread_bps        INTEGER,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_ticker_ts ON quote_cache(ticker, timestamp DESC);

-- Technical indicator cache: avoid recomputation
CREATE TABLE IF NOT EXISTS indicator_cache (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    date              INTEGER NOT NULL,
    rsi_14            REAL,
    rsi_7             REAL,
    rsi_21            REAL,
    macd_line         REAL,
    macd_signal       REAL,
    macd_histogram    REAL,
    bb_upper_20       REAL,
    bb_middle_20      REAL,
    bb_lower_20       REAL,
    bb_position       REAL,
    atr_14            REAL,
    obv               REAL,
    created_at        INTEGER NOT NULL,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_indicators_ticker_date ON indicator_cache(ticker, date DESC);

-- Portfolio exposure snapshots
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    timestamp         INTEGER NOT NULL,
    total_cash        REAL,
    total_equity      REAL,
    total_positions   REAL,
    sector_exposure   TEXT,        -- JSON dict: sector -> pct
    correlation_exposure TEXT,     -- JSON dict
    portfolio_delta   REAL,
    portfolio_gamma   REAL,
    portfolio_vega    REAL,
    portfolio_theta   REAL,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_ts ON portfolio_snapshots(user_id, timestamp DESC);

-- Alert history
CREATE TABLE IF NOT EXISTS alerts_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    ticker            TEXT,
    alert_type        TEXT NOT NULL,  -- technical, volatility, flow, macro, etc
    alert_message     TEXT NOT NULL,
    trigger_condition TEXT,
    trigger_value     REAL,
    acknowledged      INTEGER DEFAULT 0,
    acted_upon        INTEGER DEFAULT 0,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_user_ts ON alerts_log(user_id, created_at DESC);
"""


def init_db():
    """Initialize or migrate the trading database schema."""
    with _get_conn() as conn:
        conn.executescript(STOCK_TRADING_SCHEMA)
        conn.commit()
    log.info(f"Initialized trading DB at {DB_PATH}")


@contextmanager
def _get_conn():
    """Get a thread-safe DB connection."""
    with _lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


# ─── OHLCV Operations ────────────────────────────────────────────────

def insert_ohlcv_bar(ticker: str, interval: str, timestamp: int, o: float, h: float,
                     l: float, c: float, v: int, vwap: Optional[float] = None) -> None:
    """Insert or replace an OHLCV bar."""
    now = int(datetime.now(timezone.utc).timestamp())
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO ohlcv_bars
            (ticker, interval, timestamp, open, high, low, close, volume, vwap, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, interval, timestamp, o, h, l, c, v, vwap, now))
        conn.commit()


def get_ohlcv_bars(ticker: str, interval: str, limit: int = 1000) -> List[Dict[str, Any]]:
    """Get recent OHLCV bars for a ticker and interval."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM ohlcv_bars
            WHERE ticker = ? AND interval = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (ticker, interval, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]  # Return in ascending order


def get_ohlcv_range(ticker: str, interval: str, start_ts: int, end_ts: int) -> List[Dict]:
    """Get OHLCV bars within a timestamp range."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM ohlcv_bars
            WHERE ticker = ? AND interval = ? AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (ticker, interval, start_ts, end_ts)).fetchall()
    return [dict(row) for row in rows]


# ─── Trade Operations ────────────────────────────────────────────────

def insert_trade(
    ticker: str,
    side: str,  # BUY or SHORT
    entry_date: int,
    entry_price: float,
    entry_reason: str,
    signals_present: Optional[str] = None,
    confidence: float = 0.5,
    position_size_shares: Optional[int] = None,
    position_size_pct: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    user_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> int:
    """Insert a new open trade. Returns trade ID."""
    now = int(datetime.now(timezone.utc).timestamp())
    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO stock_trades (
                user_id, ticker, side, entry_date, entry_price, entry_reason,
                signals_present, confidence_score, position_size_shares, position_size_pct,
                target_price, stop_price, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """, (
            user_id, ticker, side, entry_date, entry_price, entry_reason,
            signals_present, confidence, position_size_shares, position_size_pct,
            target_price, stop_price, now, now
        ))
        conn.commit()
        return cursor.lastrowid


def close_trade(
    trade_id: int,
    exit_date: int,
    exit_price: float,
    exit_reason: str,
    slippage_bps: int = 0,
    commissions: float = 0.0,
) -> None:
    """Close an open trade with exit details."""
    now = int(datetime.now(timezone.utc).timestamp())
    with _get_conn() as conn:
        # Get the original trade to compute PnL
        trade = conn.execute(
            "SELECT side, position_size_shares, entry_price FROM stock_trades WHERE id = ?",
            (trade_id,)
        ).fetchone()

        if trade:
            side, shares, entry = trade[0], trade[1], trade[2]
            if shares and entry:
                if side == "BUY":
                    realized_pnl = (exit_price - entry) * shares - commissions
                    realized_pnl_pct = ((exit_price - entry) / entry) * 100
                else:  # SHORT
                    realized_pnl = (entry - exit_price) * shares - commissions
                    realized_pnl_pct = ((entry - exit_price) / entry) * 100
            else:
                realized_pnl = None
                realized_pnl_pct = None
        else:
            realized_pnl = None
            realized_pnl_pct = None

        conn.execute("""
            UPDATE stock_trades
            SET exit_date = ?, exit_price = ?, exit_reason = ?,
                realized_pnl = ?, realized_pnl_pct = ?, slippage_bps = ?,
                commissions = ?, status = 'closed', updated_at = ?
            WHERE id = ?
        """, (exit_date, exit_price, exit_reason, realized_pnl, realized_pnl_pct,
              slippage_bps, commissions, now, trade_id))
        conn.commit()


def get_open_trades(user_id: Optional[int] = None) -> List[Dict]:
    """Get all open trades for a user (or all users if user_id is None)."""
    with _get_conn() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM stock_trades WHERE status = 'open' AND user_id = ? ORDER BY entry_date DESC",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_trades WHERE status = 'open' ORDER BY entry_date DESC"
            ).fetchall()
    return [dict(row) for row in rows]


def get_closed_trades(user_id: Optional[int] = None, limit: int = 100) -> List[Dict]:
    """Get closed trades for performance analysis."""
    with _get_conn() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM stock_trades WHERE status = 'closed' AND user_id = ? ORDER BY exit_date DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_trades WHERE status = 'closed' ORDER BY exit_date DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


# ─── Performance Metrics ─────────────────────────────────────────────

def insert_performance_metrics(
    user_id: Optional[int],
    date: int,
    period: str,
    trades_count: int = 0,
    winning_trades: int = 0,
    losing_trades: int = 0,
    win_rate: float = 0.0,
    avg_winner: float = 0.0,
    avg_loser: float = 0.0,
    profit_factor: float = 0.0,
    gross_profit: float = 0.0,
    gross_loss: float = 0.0,
    net_profit: float = 0.0,
    daily_return_pct: float = 0.0,
    cumulative_return_pct: float = 0.0,
    sharpe_ratio: float = 0.0,
    sortino_ratio: float = 0.0,
    calmar_ratio: float = 0.0,
    max_drawdown_pct: float = 0.0,
    volatility_pct: float = 0.0,
) -> None:
    """Insert or update performance metrics for a period."""
    now = int(datetime.now(timezone.utc).timestamp())
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO performance_metrics (
                user_id, date, period, trades_count, winning_trades, losing_trades,
                win_rate, avg_winner, avg_loser, profit_factor, gross_profit, gross_loss,
                net_profit, daily_return_pct, cumulative_return_pct, sharpe_ratio,
                sortino_ratio, calmar_ratio, max_drawdown_pct, volatility_pct, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, date, period, trades_count, winning_trades, losing_trades,
            win_rate, avg_winner, avg_loser, profit_factor, gross_profit, gross_loss,
            net_profit, daily_return_pct, cumulative_return_pct, sharpe_ratio,
            sortino_ratio, calmar_ratio, max_drawdown_pct, volatility_pct, now
        ))
        conn.commit()


def get_performance_history(user_id: Optional[int] = None, period: str = "daily") -> List[Dict]:
    """Get performance metrics history."""
    with _get_conn() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM performance_metrics WHERE user_id = ? AND period = ? ORDER BY date DESC",
                (user_id, period)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM performance_metrics WHERE period = ? ORDER BY date DESC",
                (period,)
            ).fetchall()
    return [dict(row) for row in rows]


# ─── Quote Cache ─────────────────────────────────────────────────────

def insert_quote(
    ticker: str,
    timestamp: int,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    bid_size: Optional[int] = None,
    ask_size: Optional[int] = None,
    last_price: Optional[float] = None,
) -> None:
    """Insert or replace a quote snapshot."""
    now = int(datetime.now(timezone.utc).timestamp())
    spread_bps = None
    if bid and ask and bid > 0:
        spread_bps = int((ask - bid) / bid * 10000)

    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO quote_cache
            (ticker, timestamp, bid, ask, bid_size, ask_size, last_price, spread_bps, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, timestamp, bid, ask, bid_size, ask_size, last_price, spread_bps, now))
        conn.commit()


def get_recent_quotes(ticker: str, limit: int = 1000) -> List[Dict]:
    """Get recent quotes for a ticker."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM quote_cache WHERE ticker = ? ORDER BY timestamp DESC LIMIT ?
        """, (ticker, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"Trading database initialized at {DB_PATH}")
