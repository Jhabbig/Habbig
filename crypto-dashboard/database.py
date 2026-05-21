#!/usr/bin/env python3
"""
Database layer for CryptoEdge — SQLite-backed.

Uses SQLite with WAL mode for dashboard-specific data (predictions, watchlists,
alerts, accuracy, Kalshi markets). Auth is handled by the gateway; this module
only manages dashboard-specific data.

DB file: data.db (stored alongside this file)
"""

from __future__ import annotations

import atexit
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("crypto.db")

DB_PATH = Path(__file__).parent / "data.db"

# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    window_start      TEXT NOT NULL,
    pred_direction    TEXT NOT NULL,
    pred_delta        REAL,
    pred_prob         REAL,
    confidence        REAL,
    ensemble_agreement TEXT DEFAULT '',
    model_details     TEXT DEFAULT '',
    actual_direction  TEXT,
    actual_delta      REAL,
    was_correct       INTEGER,
    resolved_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, window_start)
);

CREATE TABLE IF NOT EXISTS crypto_watchlists (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    tickers   TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crypto_alert_preferences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    min_confidence REAL NOT NULL DEFAULT 0.6,
    alert_email    INTEGER NOT NULL DEFAULT 1,
    alert_browser  INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker)
);

CREATE TABLE IF NOT EXISTS crypto_alert_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    ticker     TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crypto_kalshi_markets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT UNIQUE NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    status       TEXT,
    yes_price    REAL,
    no_price     REAL,
    volume       INTEGER DEFAULT 0,
    data         TEXT DEFAULT '{}',
    last_updated TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    id           TEXT PRIMARY KEY,
    email        TEXT,
    username     TEXT
);

CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON crypto_predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_predictions_created ON crypto_predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON crypto_watchlists(user_id);
CREATE INDEX IF NOT EXISTS idx_alert_prefs_user ON crypto_alert_preferences(user_id);
CREATE INDEX IF NOT EXISTS idx_alert_prefs_ticker ON crypto_alert_preferences(ticker);
CREATE INDEX IF NOT EXISTS idx_alert_history_ticker ON crypto_alert_history(ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_ticker ON crypto_kalshi_markets(ticker);

CREATE TABLE IF NOT EXISTS news_trade_alerts (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    link         TEXT,
    source       TEXT,
    published    TEXT,
    description  TEXT,
    score        INTEGER DEFAULT 0,
    keywords     TEXT DEFAULT '[]',
    event_keywords TEXT DEFAULT '[]',
    reasons      TEXT DEFAULT '[]',
    amounts      TEXT DEFAULT '[]',
    related_markets TEXT DEFAULT '[]',
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    notified     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS news_trade_watchlist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    alert_id     TEXT NOT NULL,
    notes        TEXT DEFAULT '',
    notify_email INTEGER DEFAULT 1,
    notify_push  INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, alert_id)
);

CREATE INDEX IF NOT EXISTS idx_news_alerts_score ON news_trade_alerts(score DESC);
CREATE INDEX IF NOT EXISTS idx_news_alerts_scanned ON news_trade_alerts(scanned_at);
CREATE INDEX IF NOT EXISTS idx_news_watchlist_user ON news_trade_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_news_watchlist_alert ON news_trade_watchlist(alert_id);

CREATE TABLE IF NOT EXISTS clob_credentials (
    user_id      TEXT PRIMARY KEY,
    encrypted    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clob_trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    order_id        TEXT,
    condition_id    TEXT,
    token_id        TEXT,
    market_question TEXT,
    outcome         TEXT,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'market',
    price           REAL,
    size            REAL,
    amount          REAL,
    status          TEXT NOT NULL DEFAULT 'submitted',
    response_data   TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clob_trades_user ON clob_trade_log(user_id);
CREATE INDEX IF NOT EXISTS idx_clob_trades_status ON clob_trade_log(status);
CREATE INDEX IF NOT EXISTS idx_clob_trades_created ON clob_trade_log(created_at);

CREATE TABLE IF NOT EXISTS clob_favorites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    question     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, condition_id)
);

CREATE INDEX IF NOT EXISTS idx_clob_favorites_user ON clob_favorites(user_id);

CREATE TABLE IF NOT EXISTS kalshi_credentials (
    user_id      TEXT PRIMARY KEY,
    encrypted    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Long-term holding tables ────────────────────────────────────────────────
-- Daily OHLCV bars used by the long_term analytics module. Cheap to keep in
-- the same DB as the rest of the dashboard data; ~5 KB per asset per year.
CREATE TABLE IF NOT EXISTS crypto_daily_bars (
    ticker     TEXT NOT NULL,
    date       TEXT NOT NULL,           -- ISO YYYY-MM-DD
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, date)
);

-- Long, narrow on-chain metrics table — one row per (ticker, metric, date).
-- Lets us add new metrics without schema migrations.
CREATE TABLE IF NOT EXISTS crypto_onchain_metrics (
    ticker  TEXT NOT NULL,
    metric  TEXT NOT NULL,
    date    TEXT NOT NULL,
    value   REAL NOT NULL,
    PRIMARY KEY (ticker, metric, date)
);
CREATE INDEX IF NOT EXISTS idx_onchain_ticker_metric ON crypto_onchain_metrics(ticker, metric);

-- User holdings, lot-aware. One row per acquisition lot so we can track
-- long-term vs short-term capital gains correctly.
CREATE TABLE IF NOT EXISTS crypto_holdings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    qty           REAL NOT NULL,
    cost_basis    REAL NOT NULL,        -- USD per unit at acquisition
    acquired_at   TEXT NOT NULL,        -- ISO date
    note          TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_holdings_user ON crypto_holdings(user_id);
CREATE INDEX IF NOT EXISTS idx_holdings_user_ticker ON crypto_holdings(user_id, ticker);

-- Per-user target portfolio weights. Sum should be ~1.0; we don't enforce it.
CREATE TABLE IF NOT EXISTS crypto_target_weights (
    user_id     TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    weight      REAL NOT NULL,
    drift_band  REAL NOT NULL DEFAULT 0.05,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, ticker)
);

-- DCA schedule: one row per (user, ticker). The bot/cron consumes this.
CREATE TABLE IF NOT EXISTS crypto_dca_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    frequency       TEXT NOT NULL DEFAULT 'weekly',  -- daily | weekly | monthly
    base_amount_usd REAL NOT NULL,
    use_multiplier  INTEGER NOT NULL DEFAULT 1,      -- apply cycle-aware multiplier?
    active          INTEGER NOT NULL DEFAULT 1,
    next_run_at     TEXT,
    last_run_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker)
);

-- Long-term alert preferences (drawdown depth, MVRV cross, vol regime change).
CREATE TABLE IF NOT EXISTS crypto_long_term_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    alert_type   TEXT NOT NULL,         -- drawdown | mvrv_high | mvrv_low | vol_regime | risk_off
    threshold    REAL,
    last_fired_at TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker, alert_type)
);
CREATE INDEX IF NOT EXISTS idx_lt_alerts_user ON crypto_long_term_alerts(user_id);

-- ── Derivatives series (funding, OI, basis) ─────────────────────────────────
-- Long-narrow table so we can add new series without schema migrations.
CREATE TABLE IF NOT EXISTS crypto_derivatives_series (
    ticker  TEXT NOT NULL,
    ts      TEXT NOT NULL,           -- ISO datetime
    value   REAL NOT NULL,
    metric  TEXT NOT NULL,           -- funding_rate | open_interest_usd | perp_basis
    PRIMARY KEY (ticker, ts, metric)
);
CREATE INDEX IF NOT EXISTS idx_deriv_ticker_metric ON crypto_derivatives_series(ticker, metric);
CREATE INDEX IF NOT EXISTS idx_deriv_ts ON crypto_derivatives_series(ts);

-- ── Macro series (FRED / Stooq) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_macro_series (
    series_id TEXT NOT NULL,
    date      TEXT NOT NULL,         -- ISO date (YYYY-MM-DD)
    value     REAL NOT NULL,
    PRIMARY KEY (series_id, date)
);
CREATE INDEX IF NOT EXISTS idx_macro_series ON crypto_macro_series(series_id);

-- ── Indicator backtest results ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_indicator_backtests (
    indicator         TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    horizon_days      INTEGER NOT NULL,
    fired_n           INTEGER NOT NULL,
    median_fwd_return REAL,
    mean_fwd_return   REAL,
    win_rate          REAL,
    median_baseline   REAL NOT NULL,
    median_excess     REAL,
    hit_ratio         REAL,
    sample_window     INTEGER NOT NULL,
    computed_at       TEXT NOT NULL,
    PRIMARY KEY (indicator, ticker, horizon_days, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_backtest_lookup ON crypto_indicator_backtests(indicator, ticker, horizon_days);

-- ── Auto-execution: exchange credentials ────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_exchange_credentials (
    user_id     TEXT NOT NULL,
    exchange    TEXT NOT NULL,         -- coinbase | kraken
    encrypted   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, exchange)
);

-- ── Auto-execution: per-user safety limits ──────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_safety_limits (
    user_id              TEXT PRIMARY KEY,
    dry_run              INTEGER NOT NULL DEFAULT 1,
    max_order_usd        REAL NOT NULL DEFAULT 500.0,
    max_daily_usd        REAL NOT NULL DEFAULT 1000.0,
    circuit_breaker_pct  REAL NOT NULL DEFAULT 0.10,
    limit_offset_bps     INTEGER NOT NULL DEFAULT 50,
    limit_ttl_seconds    INTEGER NOT NULL DEFAULT 3600,
    fallback_to_market   INTEGER NOT NULL DEFAULT 0,
    preferred_exchange   TEXT NOT NULL DEFAULT 'coinbase',
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Auto-execution: append-only execution log ───────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_executions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    side             TEXT NOT NULL,         -- buy | sell
    action           TEXT NOT NULL,         -- placed | filled | dry_run | skipped | blocked | cancelled_ttl
    reason           TEXT,
    usd_amount       REAL,
    limit_price      REAL,
    fill_price       REAL,
    fill_qty         REAL,
    order_id         TEXT,
    client_order_id  TEXT,
    status           TEXT NOT NULL DEFAULT 'open',   -- open | filled | cancelled
    raw              TEXT DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_exec_user ON crypto_executions(user_id);
CREATE INDEX IF NOT EXISTS idx_exec_user_created ON crypto_executions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_open ON crypto_executions(user_id, status, action);

-- ── Tax: dispositions (immutable sale ledger) ───────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_dispositions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    qty             REAL NOT NULL,
    sell_price      REAL NOT NULL,
    sell_date       TEXT NOT NULL,         -- ISO date YYYY-MM-DD
    method          TEXT NOT NULL,         -- FIFO | LIFO | HIFO | LOFO | TAX_OPTIMAL
    exchange        TEXT NOT NULL DEFAULT 'manual',
    execution_id    INTEGER,               -- FK → crypto_executions.id (nullable)
    realized_gain   REAL NOT NULL DEFAULT 0,
    lt_gain         REAL NOT NULL DEFAULT 0,
    st_gain         REAL NOT NULL DEFAULT 0,
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_disp_user ON crypto_dispositions(user_id);
CREATE INDEX IF NOT EXISTS idx_disp_user_date ON crypto_dispositions(user_id, sell_date);
CREATE INDEX IF NOT EXISTS idx_disp_execution ON crypto_dispositions(execution_id);

-- ── Tax: lot consumption (which acquisition lot funded which sale) ─────────
CREATE TABLE IF NOT EXISTS crypto_tax_lot_consumption (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    disposition_id  INTEGER NOT NULL,
    holding_id      INTEGER NOT NULL,
    consumed_qty    REAL NOT NULL,
    cost_basis      REAL NOT NULL,         -- per-unit cost basis at acquisition
    realized_gain   REAL NOT NULL,
    classification  TEXT NOT NULL,         -- LT | ST
    days_held       INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (disposition_id) REFERENCES crypto_dispositions(id),
    FOREIGN KEY (holding_id) REFERENCES crypto_holdings(id)
);
CREATE INDEX IF NOT EXISTS idx_consump_disposition ON crypto_tax_lot_consumption(disposition_id);
CREATE INDEX IF NOT EXISTS idx_consump_holding ON crypto_tax_lot_consumption(holding_id);

-- ── Tax: per-user settings ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_tax_settings (
    user_id              TEXT PRIMARY KEY,
    jurisdiction         TEXT NOT NULL DEFAULT 'US',
    default_lot_method   TEXT NOT NULL DEFAULT 'HIFO',
    harvest_min_loss_usd REAL NOT NULL DEFAULT 100.0,
    harvest_min_age_days INTEGER NOT NULL DEFAULT 30,
    st_rate              REAL NOT NULL DEFAULT 0.30,
    lt_rate              REAL NOT NULL DEFAULT 0.15,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Push: subscriptions (one row per device) ────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    p256dh      TEXT NOT NULL,           -- client public key (base64url)
    auth        TEXT NOT NULL,           -- client auth secret (base64url)
    user_agent  TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, endpoint)
);
CREATE INDEX IF NOT EXISTS idx_push_user ON crypto_push_subscriptions(user_id);

-- ── Push: pending notifications (service worker fetches these) ──────────────
CREATE TABLE IF NOT EXISTS crypto_pending_notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    url          TEXT DEFAULT '/long-term',
    tag          TEXT DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON crypto_pending_notifications(user_id, delivered_at);

-- ── Strategies (Phase 4) ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_strategies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id         TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT DEFAULT '',
    rules_json            TEXT NOT NULL,
    base_ticker           TEXT NOT NULL,
    starting_capital_usd  REAL NOT NULL DEFAULT 10000.0,
    visibility            TEXT NOT NULL DEFAULT 'private',  -- private | public
    forked_from_id        INTEGER,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_strategies_owner ON crypto_strategies(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_strategies_public ON crypto_strategies(visibility, created_at DESC);

CREATE TABLE IF NOT EXISTS crypto_strategy_backtests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id         INTEGER NOT NULL,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    final_value_usd     REAL NOT NULL,
    total_return_pct    REAL NOT NULL,
    sharpe              REAL,
    sortino             REAL,
    max_drawdown_pct    REAL NOT NULL,
    win_rate            REAL,
    trade_count         INTEGER NOT NULL DEFAULT 0,
    equity_curve_json   TEXT NOT NULL DEFAULT '[]',
    computed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (strategy_id) REFERENCES crypto_strategies(id)
);
CREATE INDEX IF NOT EXISTS idx_strat_bt_strategy ON crypto_strategy_backtests(strategy_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS crypto_strategy_follows (
    user_id      TEXT NOT NULL,
    strategy_id  INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, strategy_id)
);

-- ── Onboarding state ────────────────────────────────────────────────────────
-- One row per user that's started onboarding. Step is the *next* one they
-- need to complete; completed_at is set when they hit the last step.
CREATE TABLE IF NOT EXISTS crypto_user_onboarding (
    user_id        TEXT PRIMARY KEY,
    step           TEXT NOT NULL DEFAULT 'welcome',
    settings_json  TEXT DEFAULT '{}',
    completed_at   TEXT,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Live strategy subscriptions ─────────────────────────────────────────────
-- Subscribing chains a public strategy into the user's account. The
-- subscription evaluator runs the strategy's rules each tick and routes
-- the resulting buys/sells through _evaluate_leg (same safety gauntlet).
-- Subscriptions are *isolated* from the user's manual DCA schedule so
-- they don't overwrite each other.
CREATE TABLE IF NOT EXISTS crypto_strategy_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    strategy_id     INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    paused          INTEGER NOT NULL DEFAULT 0,
    last_run_at     TEXT,
    next_run_at     TEXT,
    last_action     TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_strat_subs_user ON crypto_strategy_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_strat_subs_active ON crypto_strategy_subscriptions(active, next_run_at);

-- ── Billing (Stripe subscriptions, tier mapping) ────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_billing (
    user_id                TEXT PRIMARY KEY,
    tier                   TEXT NOT NULL DEFAULT 'free',  -- free | pro | wealth
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    status                 TEXT DEFAULT 'active',         -- active | past_due | cancelled
    current_period_end     TEXT,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_billing_customer ON crypto_billing(stripe_customer_id);

-- ── Referrals ───────────────────────────────────────────────────────────────
-- One stable code per user (the code is the PK so lookups are O(1)).
CREATE TABLE IF NOT EXISTS crypto_referral_codes (
    code        TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Anonymous visits — recorded when someone hits `?ref=CODE` before signup.
-- The `anon_id` is a session cookie value we set client-side.
CREATE TABLE IF NOT EXISTS crypto_referral_visits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_user_id    TEXT NOT NULL,
    referral_code       TEXT NOT NULL,
    anon_id             TEXT NOT NULL,
    source              TEXT DEFAULT 'link',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ref_visits_anon ON crypto_referral_visits(anon_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ref_visits_referrer ON crypto_referral_visits(referrer_user_id);

-- Bound attributions — one row per referred user after signup. Immutable
-- linkage; only the conversion fields get updated later.
CREATE TABLE IF NOT EXISTS crypto_referral_attributions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    referred_user_id       TEXT NOT NULL UNIQUE,
    referrer_user_id       TEXT NOT NULL,
    referral_code          TEXT NOT NULL,
    source                 TEXT DEFAULT 'link',
    recorded_at            TEXT NOT NULL DEFAULT (datetime('now')),
    converted_at           TEXT,
    conversion_tier        TEXT,
    conversion_value_cents INTEGER DEFAULT 0,
    payout_owed_cents      INTEGER DEFAULT 0,
    payout_status          TEXT DEFAULT 'pending'  -- pending | paid | clawed_back
);
CREATE INDEX IF NOT EXISTS idx_ref_attr_referrer ON crypto_referral_attributions(referrer_user_id);
-- ── News (Bloomberg-style entity-tagged feed + alert rules) ────────────────
CREATE TABLE IF NOT EXISTS crypto_news_items (
    id            TEXT PRIMARY KEY,         -- 16-char URL hash
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT,
    body_snippet  TEXT DEFAULT '',
    sentiment     REAL DEFAULT 0,
    topics        TEXT DEFAULT '',          -- CSV
    tickers       TEXT DEFAULT '',
    regulators    TEXT DEFAULT '',
    entities      TEXT DEFAULT '',
    tags          TEXT DEFAULT '',
    scraped_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_news_scraped ON crypto_news_items(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON crypto_news_items(source, scraped_at DESC);

CREATE TABLE IF NOT EXISTS crypto_news_alert_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    query_json    TEXT NOT NULL DEFAULT '{}',
    notify_push   INTEGER NOT NULL DEFAULT 1,
    notify_email  INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_news_rules_user ON crypto_news_alert_rules(user_id);

CREATE TABLE IF NOT EXISTS crypto_news_alert_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    news_id     TEXT NOT NULL,
    fired_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(rule_id, news_id)
);
CREATE INDEX IF NOT EXISTS idx_news_history_user ON crypto_news_alert_history(user_id, fired_at DESC);

-- ── User preferences (digest, email) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto_user_preferences (
    user_id              TEXT PRIMARY KEY,
    email                TEXT,
    digest_enabled       INTEGER NOT NULL DEFAULT 1,
    digest_day_of_week   INTEGER NOT NULL DEFAULT 0,  -- 0=Monday
    last_digest_sent_at  TEXT,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ── Connection management ────────────────────────────────────────────────────

def _configure_connection(c: sqlite3.Connection) -> None:
    """Apply performance pragmas to a fresh connection."""
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    c.execute("PRAGMA cache_size = -8000")   # 8 MB page cache
    c.execute("PRAGMA busy_timeout = 5000")  # wait up to 5 s on lock


_local = threading.local()
_all_connections: list[sqlite3.Connection] = []
_conn_list_lock = threading.Lock()


def _close_all_connections():
    """Close all tracked thread-local SQLite connections at exit."""
    with _conn_list_lock:
        for c in _all_connections:
            try:
                c.close()
            except Exception:
                pass
        _all_connections.clear()


atexit.register(_close_all_connections)


def _get_conn() -> sqlite3.Connection:
    """Return the thread-local SQLite connection, creating it if needed."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        _configure_connection(c)
        _local.conn = c
        with _conn_list_lock:
            _all_connections.append(c)
    return c


@contextmanager
def _conn():
    c = _get_conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def init_db() -> None:
    """Create tables if they don't exist. Called on startup."""
    with _conn() as c:
        c.executescript(SCHEMA)
    log.info("SQLite database initialized at %s", DB_PATH)


# ── Helper: Row wrapper ─────────────────────────────────────────────────────

class Row(dict):
    """Dict subclass that supports both dict['key'] and dict.key access,
    mimicking sqlite3.Row interface for backward compatibility."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def keys(self):
        return super().keys()


def _row(data) -> Optional[Row]:
    if data is None:
        return None
    if isinstance(data, sqlite3.Row):
        return Row({k: data[k] for k in data.keys()})
    return Row(data)


def _rows(data: list) -> list[Row]:
    return [_row(d) for d in data]


# ─── Predictions & Accuracy ─────────────────────────────────────────

def log_prediction(ticker: str, window_start: str, pred_direction: str,
                   pred_delta: float, pred_prob: float, confidence: float,
                   ensemble_agreement: str = "", model_details: str = ""):
    """Insert a prediction, ignoring duplicates on (ticker, window_start)."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO crypto_predictions
                   (ticker, window_start, pred_direction, pred_delta, pred_prob,
                    confidence, ensemble_agreement, model_details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, window_start, pred_direction, pred_delta, pred_prob,
                 confidence, ensemble_agreement, model_details),
            )
    except Exception as e:
        log.warning("log_prediction error: %s", e)


def resolve_prediction(ticker: str, window_start: str, actual_direction: str, actual_delta: float):
    """Resolve an open prediction with the actual outcome."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, pred_direction FROM crypto_predictions "
            "WHERE ticker = ? AND window_start = ? AND was_correct IS NULL LIMIT 1",
            (ticker, window_start),
        ).fetchone()
        if not row:
            return
        was_correct = 1 if row["pred_direction"] == actual_direction else 0
        c.execute(
            """UPDATE crypto_predictions
               SET actual_direction = ?, actual_delta = ?, was_correct = ?,
                   resolved_at = ?
               WHERE id = ?""",
            (actual_direction, actual_delta, was_correct,
             datetime.now(timezone.utc).isoformat(), row["id"]),
        )


def get_accuracy_stats(ticker: str = None, days: int = 30) -> dict:
    """Compute accuracy statistics from resolved predictions."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM crypto_predictions "
                "WHERE was_correct IS NOT NULL AND created_at > ? AND ticker = ? "
                "ORDER BY created_at DESC",
                (since, ticker),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_predictions "
                "WHERE was_correct IS NOT NULL AND created_at > ? "
                "ORDER BY created_at DESC",
                (since,),
            ).fetchall()

    if not rows:
        return {"total": 0, "correct": 0, "accuracy": 0,
                "high_conf_total": 0, "high_conf_correct": 0, "high_conf_accuracy": 0}

    total = len(rows)
    correct = sum(1 for r in rows if r["was_correct"])
    hc = [r for r in rows if (r["confidence"] or 0) >= 0.6]
    hc_correct = sum(1 for r in hc if r["was_correct"])

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "high_conf_total": len(hc),
        "high_conf_correct": hc_correct,
        "high_conf_accuracy": hc_correct / len(hc) if hc else 0,
        "avg_mae": sum(abs((r["pred_delta"] or 0) - (r["actual_delta"] or 0)) for r in rows) / total,
    }


def get_recent_predictions(ticker: str = None, limit: int = 50) -> list:
    """Fetch the most recent predictions."""
    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM crypto_predictions WHERE ticker = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_predictions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _rows(rows)


# ─── Watchlists ──────────────────────────────────────────────────────

def get_watchlists(user_id: str) -> list:
    """Get all watchlists for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_watchlists WHERE user_id = ?", (user_id,)
        ).fetchall()
    return _rows(rows)


def create_watchlist(user_id: str, name: str, tickers: list) -> int:
    """Create a new watchlist. Returns the new row ID."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO crypto_watchlists (user_id, name, tickers) VALUES (?, ?, ?)",
            (user_id, name, json.dumps(tickers)),
        )
        return cur.lastrowid or 0


def update_watchlist(watchlist_id: int, user_id: str, tickers: list):
    """Update the tickers in a watchlist (owner-scoped)."""
    with _conn() as c:
        c.execute(
            "UPDATE crypto_watchlists SET tickers = ? WHERE id = ? AND user_id = ?",
            (json.dumps(tickers), watchlist_id, user_id),
        )


def delete_watchlist(watchlist_id: int, user_id: str):
    """Delete a watchlist (owner-scoped)."""
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_watchlists WHERE id = ? AND user_id = ?",
            (watchlist_id, user_id),
        )


# ─── Alert Preferences ──────────────────────────────────────────────

def get_alert_prefs(user_id: str) -> list:
    """Get all alert preferences for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_alert_preferences WHERE user_id = ?", (user_id,)
        ).fetchall()
    return _rows(rows)


def set_alert_pref(user_id: str, ticker: str, min_confidence: float = 0.6,
                   alert_email: bool = True, alert_browser: bool = True):
    """Upsert an alert preference for a user+ticker pair."""
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_alert_preferences
               (user_id, ticker, min_confidence, alert_email, alert_browser)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                   min_confidence = excluded.min_confidence,
                   alert_email    = excluded.alert_email,
                   alert_browser  = excluded.alert_browser""",
            (user_id, ticker, min_confidence,
             1 if alert_email else 0, 1 if alert_browser else 0),
        )


def get_alert_prefs_for_ticker(ticker: str) -> list:
    """Get all alert preferences for a specific ticker (across all users),
    joining with profiles to get the email."""
    with _conn() as c:
        rows = c.execute(
            """SELECT a.*, COALESCE(p.email, '') AS email
               FROM crypto_alert_preferences a
               LEFT JOIN profiles p ON p.id = a.user_id
               WHERE a.ticker = ? AND a.alert_email = 1""",
            (ticker,),
        ).fetchall()
    return _rows(rows)


def log_alert(user_id: str | None, ticker: str, alert_type: str, message: str, confidence: float = 0):
    """Log an alert that was sent."""
    with _conn() as c:
        c.execute(
            "INSERT INTO crypto_alert_history (user_id, ticker, alert_type, message, confidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, ticker, alert_type, message, confidence),
        )


# ─── Kalshi ──────────────────────────────────────────────────────────

def upsert_kalshi_market(ticker: str, title: str, category: str, status: str,
                         yes_price: float, no_price: float, volume: int, data: dict):
    """Insert or update a Kalshi market entry."""
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_kalshi_markets
               (ticker, title, category, status, yes_price, no_price, volume, data, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                   title        = excluded.title,
                   category     = excluded.category,
                   status       = excluded.status,
                   yes_price    = excluded.yes_price,
                   no_price     = excluded.no_price,
                   volume       = excluded.volume,
                   data         = excluded.data,
                   last_updated = excluded.last_updated""",
            (ticker, title, category, status, yes_price, no_price, volume,
             json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )


def get_kalshi_markets(category: str = None, limit: int = 100) -> list:
    """Fetch Kalshi markets, optionally filtered by category."""
    with _conn() as c:
        if category:
            rows = c.execute(
                "SELECT * FROM crypto_kalshi_markets WHERE category = ? "
                "ORDER BY volume DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_kalshi_markets ORDER BY volume DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _rows(rows)


# ─── User lookup (reads from local profiles table) ────────────────

def get_user(user_id: str) -> dict | None:
    """Look up a user profile by UUID. Used for email alert lookups."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, username FROM profiles WHERE id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    if row:
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["username"] or "",
            "tier": "admin",  # tier is managed by gateway subscriptions now
        }
    return None


# ─── News-Trade Alerts ──────────────────────────────────────────────

def upsert_news_alert(alert: dict):
    """Insert or update a news-trade alert."""
    with _conn() as c:
        c.execute(
            """INSERT INTO news_trade_alerts
               (id, title, link, source, published, description, score,
                keywords, event_keywords, reasons, amounts, related_markets, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   score           = MAX(excluded.score, news_trade_alerts.score),
                   related_markets = excluded.related_markets,
                   scanned_at      = excluded.scanned_at""",
            (alert["id"], alert["title"], alert.get("link", ""),
             alert.get("source", ""), alert.get("published", ""),
             alert.get("description", ""), alert.get("score", 0),
             json.dumps(alert.get("insider_keywords", [])),
             json.dumps(alert.get("event_keywords", [])),
             json.dumps(alert.get("reasons", [])),
             json.dumps(alert.get("amounts", [])),
             json.dumps(alert.get("related_markets", [])),
             alert.get("scanned_at", datetime.now(timezone.utc).isoformat())),
        )


def get_news_alerts(min_score: int = 0, limit: int = 50, hours: int = 72) -> list:
    """Fetch recent news-trade alerts sorted by score."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM news_trade_alerts WHERE score >= ? AND scanned_at > ? "
            "ORDER BY score DESC, scanned_at DESC LIMIT ?",
            (min_score, since, limit),
        ).fetchall()
    result = []
    for r in _rows(rows):
        # Parse JSON fields
        for field in ("keywords", "event_keywords", "reasons", "amounts", "related_markets"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def get_unnotified_alerts(min_score: int = 30) -> list:
    """Fetch high-score alerts that haven't been pushed yet."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM news_trade_alerts WHERE score >= ? AND notified = 0 "
            "ORDER BY score DESC LIMIT 20",
            (min_score,),
        ).fetchall()
    result = []
    for r in _rows(rows):
        for field in ("keywords", "event_keywords", "reasons", "amounts", "related_markets"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def mark_alert_notified(alert_id: str):
    """Mark a news-trade alert as notified."""
    with _conn() as c:
        c.execute("UPDATE news_trade_alerts SET notified = 1 WHERE id = ?", (alert_id,))


# ─── News-Trade Watchlist ──────────────────────────────────────────

def get_news_watchlist(user_id: str) -> list:
    """Get a user's news-trade watchlist with alert details."""
    with _conn() as c:
        rows = c.execute(
            """SELECT w.*, a.title, a.link, a.source, a.score, a.published,
                      a.description, a.related_markets, a.reasons, a.keywords
               FROM news_trade_watchlist w
               JOIN news_trade_alerts a ON a.id = w.alert_id
               WHERE w.user_id = ?
               ORDER BY a.score DESC""",
            (user_id,),
        ).fetchall()
    result = []
    for r in _rows(rows):
        for field in ("related_markets", "reasons", "keywords"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def add_to_news_watchlist(user_id: str, alert_id: str, notes: str = "",
                          notify_email: bool = True, notify_push: bool = True) -> bool:
    """Add an alert to a user's watchlist. Returns True if added, False if duplicate."""
    try:
        with _conn() as c:
            cursor = c.execute(
                """INSERT OR IGNORE INTO news_trade_watchlist
                   (user_id, alert_id, notes, notify_email, notify_push)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, alert_id, notes,
                 1 if notify_email else 0, 1 if notify_push else 0),
            )
        return cursor.rowcount > 0
    except Exception:
        return False


def remove_from_news_watchlist(user_id: str, alert_id: str):
    """Remove an alert from a user's watchlist."""
    with _conn() as c:
        c.execute(
            "DELETE FROM news_trade_watchlist WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        )


def get_watchlist_users_for_alert(alert_id: str) -> list:
    """Get all users watching a specific alert (for push notifications)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT w.user_id, w.notify_email, w.notify_push,
                      COALESCE(p.email, '') AS email
               FROM news_trade_watchlist w
               LEFT JOIN profiles p ON p.id = w.user_id
               WHERE w.alert_id = ?""",
            (alert_id,),
        ).fetchall()
    return _rows(rows)


# ─── CLOB Credentials ──────────────────────────────────────────────

def save_clob_credentials(user_id: str, encrypted: str):
    """Save encrypted CLOB API credentials for a user."""
    with _conn() as c:
        c.execute(
            """INSERT INTO clob_credentials (user_id, encrypted, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   encrypted = excluded.encrypted,
                   updated_at = datetime('now')""",
            (user_id, encrypted),
        )


def get_clob_credentials(user_id: str) -> Optional[str]:
    """Get encrypted CLOB credentials for a user. Returns the encrypted blob."""
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM clob_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["encrypted"] if row else None


def delete_clob_credentials(user_id: str):
    """Delete CLOB credentials for a user."""
    with _conn() as c:
        c.execute("DELETE FROM clob_credentials WHERE user_id = ?", (user_id,))


def has_clob_credentials(user_id: str) -> bool:
    """Check if a user has CLOB credentials stored."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM clob_credentials WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


# ─── Kalshi Credentials ──────────────────────────────────────────────

def save_kalshi_credentials(user_id: str, encrypted: str):
    """Save encrypted Kalshi API credentials for a user."""
    with _conn() as c:
        c.execute(
            """INSERT INTO kalshi_credentials (user_id, encrypted, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   encrypted = excluded.encrypted,
                   updated_at = datetime('now')""",
            (user_id, encrypted),
        )


def get_kalshi_credentials(user_id: str) -> Optional[str]:
    """Get encrypted Kalshi credentials for a user. Returns the encrypted blob."""
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM kalshi_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["encrypted"] if row else None


def delete_kalshi_credentials(user_id: str):
    """Delete Kalshi credentials for a user."""
    with _conn() as c:
        c.execute("DELETE FROM kalshi_credentials WHERE user_id = ?", (user_id,))


def has_kalshi_credentials(user_id: str) -> bool:
    """Check if a user has Kalshi credentials stored."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM kalshi_credentials WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


# ─── CLOB Trade Log ───────────────────────────────────────────────

def log_clob_trade(user_id: str, order_id: str, condition_id: str,
                   token_id: str, market_question: str, outcome: str,
                   side: str, order_type: str, price: float,
                   size: float, amount: float, status: str,
                   response_data: dict) -> int:
    """Log a CLOB trade. Returns the log row ID."""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO clob_trade_log
               (user_id, order_id, condition_id, token_id, market_question,
                outcome, side, order_type, price, size, amount, status, response_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, order_id, condition_id, token_id, market_question,
             outcome, side, order_type, price, size, amount, status,
             json.dumps(response_data)),
        )
        return cur.lastrowid or 0


def get_clob_trades(user_id: str, limit: int = 50) -> list:
    """Get recent CLOB trades for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM clob_trade_log WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    result = []
    for r in _rows(rows):
        try:
            r["response_data"] = json.loads(r.get("response_data", "{}"))
        except (json.JSONDecodeError, TypeError):
            r["response_data"] = {}
        result.append(r)
    return result


def update_clob_trade_status(trade_id: int, status: str, response_data: dict = None):
    """Update the status of a logged trade."""
    with _conn() as c:
        if response_data:
            c.execute(
                "UPDATE clob_trade_log SET status = ?, response_data = ? WHERE id = ?",
                (status, json.dumps(response_data), trade_id),
            )
        else:
            c.execute(
                "UPDATE clob_trade_log SET status = ? WHERE id = ?",
                (status, trade_id),
            )


# ─── CLOB Favorites ──────────────────────────────────────────────

def get_clob_favorites(user_id: str) -> list:
    """Get a user's favorite markets."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM clob_favorites WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def add_clob_favorite(user_id: str, condition_id: str, question: str) -> bool:
    """Add a market to favorites. Returns True if added."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO clob_favorites (user_id, condition_id, question) "
                "VALUES (?, ?, ?)",
                (user_id, condition_id, question),
            )
        return True
    except Exception:
        return False


def remove_clob_favorite(user_id: str, condition_id: str):
    """Remove a market from favorites."""
    with _conn() as c:
        c.execute(
            "DELETE FROM clob_favorites WHERE user_id = ? AND condition_id = ?",
            (user_id, condition_id),
        )


# ── Long-term: daily bars ───────────────────────────────────────────────────

def upsert_daily_bars(rows: list[tuple]) -> None:
    """Bulk upsert. rows: list of (ticker, date, open, high, low, close, volume)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_daily_bars (ticker, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume""",
            rows,
        )


def get_daily_bars(ticker: str, days: int = 365 * 4) -> list[Row]:
    """Oldest→newest. `days` is just a window cap; we still return everything stored
    if you have less than that."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT date, open, high, low, close, volume FROM crypto_daily_bars "
            "WHERE ticker = ? AND date >= ? ORDER BY date ASC",
            (ticker, cutoff),
        ).fetchall()
    return _rows(rows)


def get_latest_daily_bar_date(ticker: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM crypto_daily_bars WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


# ── Long-term: on-chain metrics ─────────────────────────────────────────────

def upsert_onchain_metrics(rows: list[tuple]) -> None:
    """Bulk upsert. rows: list of (ticker, metric, date, value)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_onchain_metrics (ticker, metric, date, value)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker, metric, date) DO UPDATE SET value=excluded.value""",
            rows,
        )


def get_onchain_metric(ticker: str, metric: str, days: int = 365) -> list[Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT date, value FROM crypto_onchain_metrics "
            "WHERE ticker = ? AND metric = ? AND date >= ? ORDER BY date ASC",
            (ticker, metric, cutoff),
        ).fetchall()
    return _rows(rows)


def get_latest_onchain_date(ticker: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM crypto_onchain_metrics WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


# ── Long-term: holdings (lot-aware) ─────────────────────────────────────────

def add_holding(user_id: str, ticker: str, qty: float, cost_basis: float,
                acquired_at: str, note: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_holdings (user_id, ticker, qty, cost_basis, acquired_at, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, ticker, qty, cost_basis, acquired_at, note),
        )
        return int(cur.lastrowid)


def remove_holding(user_id: str, holding_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        )


def get_holdings(user_id: str) -> list[Row]:
    """All lots, oldest first — caller can roll up by ticker."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ticker, qty, cost_basis, acquired_at, note "
            "FROM crypto_holdings WHERE user_id = ? ORDER BY acquired_at ASC, id ASC",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def get_holdings_rollup(user_id: str) -> list[dict]:
    """Sum qty and weighted-avg cost basis per ticker."""
    lots = get_holdings(user_id)
    rollup: dict[str, dict] = {}
    for lot in lots:
        t = lot["ticker"]
        agg = rollup.setdefault(t, {"ticker": t, "qty": 0.0, "cost_total": 0.0, "lots": 0})
        agg["qty"] += float(lot["qty"])
        agg["cost_total"] += float(lot["qty"]) * float(lot["cost_basis"])
        agg["lots"] += 1
    out = []
    for t, agg in rollup.items():
        avg_cost = agg["cost_total"] / agg["qty"] if agg["qty"] else 0.0
        out.append({
            "ticker": t, "qty": agg["qty"],
            "avg_cost_basis": avg_cost, "cost_total": agg["cost_total"],
            "lots": agg["lots"],
        })
    return out


# ── Long-term: target weights ───────────────────────────────────────────────

def set_target_weight(user_id: str, ticker: str, weight: float, drift_band: float = 0.05) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_target_weights (user_id, ticker, weight, drift_band, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                 weight=excluded.weight, drift_band=excluded.drift_band,
                 updated_at=datetime('now')""",
            (user_id, ticker, weight, drift_band),
        )


def remove_target_weight(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_target_weights WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )


def get_target_weights(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ticker, weight, drift_band, updated_at "
            "FROM crypto_target_weights WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return _rows(rows)


# ── Long-term: DCA schedule ─────────────────────────────────────────────────

def upsert_dca_schedule(user_id: str, ticker: str, frequency: str, base_amount_usd: float,
                        use_multiplier: bool = True, active: bool = True) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_dca_schedule
                 (user_id, ticker, frequency, base_amount_usd, use_multiplier, active)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                 frequency=excluded.frequency,
                 base_amount_usd=excluded.base_amount_usd,
                 use_multiplier=excluded.use_multiplier,
                 active=excluded.active""",
            (user_id, ticker, frequency, base_amount_usd,
             1 if use_multiplier else 0, 1 if active else 0),
        )


def remove_dca_schedule(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_dca_schedule WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )


def get_dca_schedules(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ticker, frequency, base_amount_usd, use_multiplier, active, "
            "       next_run_at, last_run_at "
            "FROM crypto_dca_schedule WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def mark_dca_run(user_id: str, ticker: str, next_run_at: str) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_dca_schedule
               SET last_run_at = datetime('now'), next_run_at = ?
               WHERE user_id = ? AND ticker = ?""",
            (next_run_at, user_id, ticker),
        )


# ── Long-term: alerts ───────────────────────────────────────────────────────

def upsert_long_term_alert(user_id: str, ticker: str, alert_type: str,
                           threshold: Optional[float], active: bool = True) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_long_term_alerts (user_id, ticker, alert_type, threshold, active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker, alert_type) DO UPDATE SET
                 threshold=excluded.threshold, active=excluded.active""",
            (user_id, ticker, alert_type, threshold, 1 if active else 0),
        )


def get_long_term_alerts(user_id: str | None = None) -> list[Row]:
    with _conn() as c:
        if user_id:
            rows = c.execute(
                "SELECT id, user_id, ticker, alert_type, threshold, last_fired_at, active "
                "FROM crypto_long_term_alerts WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, user_id, ticker, alert_type, threshold, last_fired_at, active "
                "FROM crypto_long_term_alerts WHERE active = 1",
            ).fetchall()
    return _rows(rows)


def mark_long_term_alert_fired(alert_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE crypto_long_term_alerts SET last_fired_at = datetime('now') WHERE id = ?",
            (alert_id,),
        )


def remove_long_term_alert(user_id: str, ticker: str, alert_type: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_long_term_alerts "
            "WHERE user_id = ? AND ticker = ? AND alert_type = ?",
            (user_id, ticker, alert_type),
        )


# ── Derivatives series ──────────────────────────────────────────────────────

def upsert_derivatives_series(rows: list[tuple]) -> None:
    """Bulk upsert. rows: (ticker, ts, value, metric)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_derivatives_series (ticker, ts, value, metric)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker, ts, metric) DO UPDATE SET value=excluded.value""",
            rows,
        )


def get_derivatives_series(ticker: str, metric: str, days: int = 365) -> list[Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, value FROM crypto_derivatives_series "
            "WHERE ticker = ? AND metric = ? AND ts >= ? ORDER BY ts ASC",
            (ticker, metric, cutoff),
        ).fetchall()
    return _rows(rows)


# ── Macro series ────────────────────────────────────────────────────────────

def upsert_macro_series(rows: list[tuple]) -> None:
    """Bulk upsert. rows: (series_id, date, value)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_macro_series (series_id, date, value)
               VALUES (?, ?, ?)
               ON CONFLICT(series_id, date) DO UPDATE SET value=excluded.value""",
            rows,
        )


def get_macro_series(series_id: str, days: int = 365) -> list[Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT date, value FROM crypto_macro_series "
            "WHERE series_id = ? AND date >= ? ORDER BY date ASC",
            (series_id, cutoff),
        ).fetchall()
    return _rows(rows)


def get_latest_macro_date(series_id: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM crypto_macro_series WHERE series_id = ?",
            (series_id,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


# ── Backtest results ────────────────────────────────────────────────────────

def upsert_backtest_results(rows: list[tuple]) -> None:
    """Bulk upsert. rows tuple matches columns in crypto_indicator_backtests."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_indicator_backtests
                 (indicator, ticker, horizon_days, fired_n, median_fwd_return,
                  mean_fwd_return, win_rate, median_baseline, median_excess,
                  hit_ratio, sample_window, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(indicator, ticker, horizon_days, computed_at)
               DO NOTHING""",
            rows,
        )


def get_latest_backtest_results() -> list[Row]:
    """Most recent row for each (indicator, ticker, horizon_days)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT b.*
               FROM crypto_indicator_backtests b
               JOIN (
                 SELECT indicator, ticker, horizon_days, MAX(computed_at) AS m
                 FROM crypto_indicator_backtests
                 GROUP BY indicator, ticker, horizon_days
               ) x ON b.indicator = x.indicator AND b.ticker = x.ticker
                  AND b.horizon_days = x.horizon_days AND b.computed_at = x.m
               ORDER BY b.indicator, b.ticker, b.horizon_days"""
        ).fetchall()
    return _rows(rows)


# ── Exchange credentials ────────────────────────────────────────────────────

def upsert_exchange_credentials(user_id: str, exchange: str, encrypted: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_exchange_credentials (user_id, exchange, encrypted, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, exchange) DO UPDATE SET
                 encrypted = excluded.encrypted,
                 updated_at = datetime('now')""",
            (user_id, exchange, encrypted),
        )


def get_exchange_credentials(user_id: str, exchange: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM crypto_exchange_credentials "
            "WHERE user_id = ? AND exchange = ?",
            (user_id, exchange),
        ).fetchone()
    return row["encrypted"] if row else None


def delete_exchange_credentials(user_id: str, exchange: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_exchange_credentials WHERE user_id = ? AND exchange = ?",
            (user_id, exchange),
        )


def list_exchange_credentials(user_id: str) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT exchange FROM crypto_exchange_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [r["exchange"] for r in rows]


# ── Safety limits ───────────────────────────────────────────────────────────

def get_safety_limits(user_id: str) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            """SELECT dry_run, max_order_usd, max_daily_usd, circuit_breaker_pct,
                      limit_offset_bps, limit_ttl_seconds, fallback_to_market,
                      preferred_exchange, updated_at
               FROM crypto_safety_limits WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    out = _row(row)
    # SQLite stores ints; Python-side it's nicer to expose booleans.
    if out:
        out["dry_run"] = bool(out["dry_run"])
        out["fallback_to_market"] = bool(out["fallback_to_market"])
    return out


def upsert_safety_limits(user_id: str, safety: dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_safety_limits
                 (user_id, dry_run, max_order_usd, max_daily_usd, circuit_breaker_pct,
                  limit_offset_bps, limit_ttl_seconds, fallback_to_market,
                  preferred_exchange, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 dry_run=excluded.dry_run,
                 max_order_usd=excluded.max_order_usd,
                 max_daily_usd=excluded.max_daily_usd,
                 circuit_breaker_pct=excluded.circuit_breaker_pct,
                 limit_offset_bps=excluded.limit_offset_bps,
                 limit_ttl_seconds=excluded.limit_ttl_seconds,
                 fallback_to_market=excluded.fallback_to_market,
                 preferred_exchange=excluded.preferred_exchange,
                 updated_at=datetime('now')""",
            (user_id,
             1 if safety["dry_run"] else 0,
             float(safety["max_order_usd"]),
             float(safety["max_daily_usd"]),
             float(safety["circuit_breaker_pct"]),
             int(safety["limit_offset_bps"]),
             int(safety["limit_ttl_seconds"]),
             1 if safety["fallback_to_market"] else 0,
             str(safety["preferred_exchange"])),
        )


# ── Executions ──────────────────────────────────────────────────────────────

def log_execution(user_id: str, decision: dict, raw: dict | None = None) -> int:
    """Persist an execution decision. Returns the inserted row id."""
    raw_json = json.dumps(raw or {})
    status = "open" if decision.get("action") == "placed" else "logged"
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_executions
                 (user_id, ticker, exchange, side, action, reason,
                  usd_amount, limit_price, order_id, client_order_id, status, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id,
             str(decision.get("ticker", "")),
             str(decision.get("exchange", "")),
             str(decision.get("side", "")),
             str(decision.get("action", "")),
             str(decision.get("reason", "")),
             decision.get("usd_amount"),
             decision.get("limit_price"),
             decision.get("order_id"),
             decision.get("client_order_id"),
             status,
             raw_json),
        )
        return int(cur.lastrowid)


def update_execution_status(exec_id: int, status: str,
                            fill_price: float | None = None,
                            fill_qty: float | None = None) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_executions
               SET status = ?,
                   fill_price = COALESCE(?, fill_price),
                   fill_qty = COALESCE(?, fill_qty)
               WHERE id = ?""",
            (status, fill_price, fill_qty, exec_id),
        )


def get_executions(user_id: str, limit: int = 200) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM crypto_executions WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return _rows(rows)


def get_executions_since(user_id: str, hours: int = 24) -> list[Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_executions WHERE user_id = ? AND created_at >= ?",
            (user_id, cutoff),
        ).fetchall()
    return _rows(rows)


def get_open_executions_before(user_id: str, cutoff_iso: str) -> list[Row]:
    """Open (status='open', action='placed') orders older than cutoff_iso."""
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM crypto_executions
               WHERE user_id = ? AND status = 'open' AND action = 'placed'
                 AND created_at < ? AND order_id IS NOT NULL""",
            (user_id, cutoff_iso),
        ).fetchall()
    return _rows(rows)


def get_all_open_executions(user_id: str) -> list[Row]:
    """All currently-open placed orders for this user. Used by the fill poller."""
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM crypto_executions
               WHERE user_id = ? AND status = 'open' AND action = 'placed'
                 AND order_id IS NOT NULL
               ORDER BY created_at ASC""",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def get_due_dca_schedules() -> list[Row]:
    """Schedules where next_run_at <= now (or is unset) AND active = 1.
    Returns user_id alongside ticker for the cross-user cron loop."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT user_id, ticker, frequency, base_amount_usd, use_multiplier,
                      next_run_at, last_run_at
               FROM crypto_dca_schedule
               WHERE active = 1
                 AND (next_run_at IS NULL OR next_run_at <= ?)""",
            (now,),
        ).fetchall()
    return _rows(rows)


# ── Tax: dispositions ───────────────────────────────────────────────────────

def insert_disposition(user_id: str, ticker: str, qty: float, sell_price: float,
                       sell_date: str, method: str, exchange: str,
                       execution_id: int | None, realized_gain: float,
                       lt_gain: float, st_gain: float, notes: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_dispositions
                 (user_id, ticker, qty, sell_price, sell_date, method, exchange,
                  execution_id, realized_gain, lt_gain, st_gain, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, ticker, qty, sell_price, sell_date, method, exchange,
             execution_id, realized_gain, lt_gain, st_gain, notes),
        )
        return int(cur.lastrowid)


def get_dispositions(user_id: str, since: str | None = None,
                     until: str | None = None, limit: int = 500) -> list[Row]:
    sql = "SELECT * FROM crypto_dispositions WHERE user_id = ?"
    params: list = [user_id]
    if since:
        sql += " AND sell_date >= ?"; params.append(since)
    if until:
        sql += " AND sell_date < ?"; params.append(until)
    sql += " ORDER BY sell_date DESC, id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return _rows(rows)


def disposition_exists_for_execution(execution_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM crypto_dispositions WHERE execution_id = ? LIMIT 1",
            (execution_id,),
        ).fetchone()
    return row is not None


def get_disposition_by_execution(execution_id: int) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM crypto_dispositions WHERE execution_id = ? LIMIT 1",
            (execution_id,),
        ).fetchone()
    return _row(row)


# ── Tax: lot consumption ────────────────────────────────────────────────────

def insert_lot_consumption(rows: list[tuple]) -> None:
    """Bulk insert. Each row: (disposition_id, holding_id, consumed_qty,
    cost_basis, realized_gain, classification, days_held)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_tax_lot_consumption
                 (disposition_id, holding_id, consumed_qty, cost_basis,
                  realized_gain, classification, days_held)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def get_lot_consumption_for_disposition(disposition_id: int) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            """SELECT c.*, h.acquired_at, h.ticker
               FROM crypto_tax_lot_consumption c
               JOIN crypto_holdings h ON h.id = c.holding_id
               WHERE c.disposition_id = ?
               ORDER BY c.id""",
            (disposition_id,),
        ).fetchall()
    return _rows(rows)


def get_consumption_by_holding(user_id: str) -> dict:
    """Sum consumed qty grouped by holding_id, scoped to this user's lots.
    Returns {holding_id: consumed_qty}."""
    with _conn() as c:
        rows = c.execute(
            """SELECT c.holding_id, COALESCE(SUM(c.consumed_qty), 0) AS consumed
               FROM crypto_tax_lot_consumption c
               JOIN crypto_holdings h ON h.id = c.holding_id
               WHERE h.user_id = ?
               GROUP BY c.holding_id""",
            (user_id,),
        ).fetchall()
    return {r["holding_id"]: float(r["consumed"]) for r in rows}


# ── Tax: settings ───────────────────────────────────────────────────────────

def get_tax_settings(user_id: str) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            """SELECT jurisdiction, default_lot_method, harvest_min_loss_usd,
                      harvest_min_age_days, st_rate, lt_rate, updated_at
               FROM crypto_tax_settings WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
    return _row(row)


def upsert_tax_settings(user_id: str, settings: dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_tax_settings
                 (user_id, jurisdiction, default_lot_method,
                  harvest_min_loss_usd, harvest_min_age_days,
                  st_rate, lt_rate, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 jurisdiction=excluded.jurisdiction,
                 default_lot_method=excluded.default_lot_method,
                 harvest_min_loss_usd=excluded.harvest_min_loss_usd,
                 harvest_min_age_days=excluded.harvest_min_age_days,
                 st_rate=excluded.st_rate,
                 lt_rate=excluded.lt_rate,
                 updated_at=datetime('now')""",
            (user_id,
             str(settings["jurisdiction"]),
             str(settings["default_lot_method"]),
             float(settings["harvest_min_loss_usd"]),
             int(settings["harvest_min_age_days"]),
             float(settings["st_rate"]),
             float(settings["lt_rate"])),
        )


# ── Push subscriptions ──────────────────────────────────────────────────────

def upsert_push_subscription(user_id: str, endpoint: str, p256dh: str,
                             auth: str, user_agent: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_push_subscriptions
                 (user_id, endpoint, p256dh, auth, user_agent)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, endpoint) DO UPDATE SET
                 p256dh = excluded.p256dh,
                 auth = excluded.auth,
                 user_agent = excluded.user_agent""",
            (user_id, endpoint, p256dh, auth, user_agent),
        )
        return int(cur.lastrowid)


def get_push_subscriptions(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, endpoint, p256dh, auth, user_agent, created_at "
            "FROM crypto_push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def delete_push_subscription(user_id: str, endpoint: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (user_id, endpoint),
        )


def delete_push_subscription_by_id(sub_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM crypto_push_subscriptions WHERE id = ?", (sub_id,))


# ── Pending notifications ───────────────────────────────────────────────────

def insert_pending_notification(user_id: str, title: str, body: str,
                                 url: str, tag: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_pending_notifications
                 (user_id, title, body, url, tag)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, title, body, url, tag),
        )
        return int(cur.lastrowid)


def get_pending_notifications(user_id: str, limit: int = 20) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, title, body, url, tag, created_at
               FROM crypto_pending_notifications
               WHERE user_id = ? AND delivered_at IS NULL
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return _rows(rows)


def mark_notifications_delivered(user_id: str, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with _conn() as c:
        c.execute(
            f"UPDATE crypto_pending_notifications "
            f"SET delivered_at = datetime('now') "
            f"WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *ids),
        )


# ── Strategies (Phase 4) ────────────────────────────────────────────────────

def insert_strategy(owner_user_id: str, name: str, description: str,
                    rules_json: str, base_ticker: str,
                    starting_capital_usd: float, visibility: str,
                    forked_from_id: int | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_strategies
                 (owner_user_id, name, description, rules_json, base_ticker,
                  starting_capital_usd, visibility, forked_from_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, name, description, rules_json, base_ticker,
             starting_capital_usd, visibility, forked_from_id),
        )
        return int(cur.lastrowid)


def update_strategy_row(strategy_id: int, name: str, description: str,
                        rules_json: str, base_ticker: str,
                        starting_capital_usd: float, visibility: str) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_strategies SET
                 name=?, description=?, rules_json=?, base_ticker=?,
                 starting_capital_usd=?, visibility=?, updated_at=datetime('now')
               WHERE id=?""",
            (name, description, rules_json, base_ticker, starting_capital_usd,
             visibility, strategy_id),
        )


def delete_strategy_row(strategy_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM crypto_strategy_follows WHERE strategy_id = ?", (strategy_id,))
        c.execute("DELETE FROM crypto_strategy_backtests WHERE strategy_id = ?", (strategy_id,))
        c.execute("DELETE FROM crypto_strategies WHERE id = ?", (strategy_id,))


def get_strategy(strategy_id: int) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM crypto_strategies WHERE id = ?", (strategy_id,),
        ).fetchone()
    return _row(row)


def list_strategies_for_user(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_strategies WHERE owner_user_id = ? "
            "ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def list_public_strategies(limit: int = 50) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_strategies WHERE visibility = 'public' "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return _rows(rows)


def insert_strategy_backtest(strategy_id: int, start_date: str, end_date: str,
                              final_value_usd: float, total_return_pct: float,
                              sharpe: float, sortino: float,
                              max_drawdown_pct: float, win_rate: float,
                              trade_count: int, equity_curve_json: str) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_strategy_backtests
                 (strategy_id, start_date, end_date, final_value_usd,
                  total_return_pct, sharpe, sortino, max_drawdown_pct,
                  win_rate, trade_count, equity_curve_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, start_date, end_date, final_value_usd,
             total_return_pct, sharpe, sortino, max_drawdown_pct,
             win_rate, trade_count, equity_curve_json),
        )
        return int(cur.lastrowid)


def get_latest_strategy_backtest(strategy_id: int) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM crypto_strategy_backtests
               WHERE strategy_id = ? ORDER BY computed_at DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
    return _row(row)


def upsert_strategy_follow(user_id: str, strategy_id: int) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_strategy_follows (user_id, strategy_id)
               VALUES (?, ?) ON CONFLICT DO NOTHING""",
            (user_id, strategy_id),
        )


def delete_strategy_follow(user_id: str, strategy_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_strategy_follows WHERE user_id = ? AND strategy_id = ?",
            (user_id, strategy_id),
        )


def leaderboard_data(limit: int) -> list[Row]:
    """Join strategies with their most-recent backtest, public-only."""
    with _conn() as c:
        rows = c.execute(
            """SELECT s.id, s.name, s.description, s.base_ticker,
                      s.owner_user_id, s.created_at,
                      b.start_date, b.end_date, b.final_value_usd,
                      b.total_return_pct, b.sharpe, b.sortino,
                      b.max_drawdown_pct, b.win_rate, b.trade_count,
                      b.computed_at
               FROM crypto_strategies s
               JOIN (
                 SELECT bb.* FROM crypto_strategy_backtests bb
                 INNER JOIN (
                   SELECT strategy_id, MAX(computed_at) AS m
                   FROM crypto_strategy_backtests
                   GROUP BY strategy_id
                 ) x ON bb.strategy_id = x.strategy_id AND bb.computed_at = x.m
               ) b ON b.strategy_id = s.id
               WHERE s.visibility = 'public'
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return _rows(rows)


# ── Onboarding ──────────────────────────────────────────────────────────────

def get_onboarding(user_id: str) -> Optional[Row]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM crypto_user_onboarding WHERE user_id = ?", (user_id,),
        ).fetchone()
    return _row(row)


def upsert_onboarding(user_id: str, step: str, settings_json: str = "{}",
                      completed: bool = False) -> None:
    completed_at = "datetime('now')" if completed else "NULL"
    with _conn() as c:
        c.execute(
            f"""INSERT INTO crypto_user_onboarding
                  (user_id, step, settings_json, completed_at, updated_at)
                VALUES (?, ?, ?, {completed_at}, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                  step = excluded.step,
                  settings_json = excluded.settings_json,
                  completed_at = COALESCE(crypto_user_onboarding.completed_at, excluded.completed_at),
                  updated_at = datetime('now')""",
            (user_id, step, settings_json),
        )


# ── Strategy subscriptions ──────────────────────────────────────────────────

def upsert_strategy_subscription(user_id: str, strategy_id: int,
                                 next_run_at: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_strategy_subscriptions
                 (user_id, strategy_id, next_run_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, strategy_id) DO UPDATE SET
                 active = 1, paused = 0,
                 next_run_at = COALESCE(?, crypto_strategy_subscriptions.next_run_at)""",
            (user_id, strategy_id, next_run_at, next_run_at),
        )
        # ON CONFLICT doesn't return a new lastrowid; look it up.
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = c.execute(
            "SELECT id FROM crypto_strategy_subscriptions WHERE user_id = ? AND strategy_id = ?",
            (user_id, strategy_id),
        ).fetchone()
        return int(row["id"]) if row else 0


def get_strategy_subscriptions(user_id: str) -> list[Row]:
    """Subscriptions for one user, joined with strategy name + visibility."""
    with _conn() as c:
        rows = c.execute(
            """SELECT sub.*, s.name AS strategy_name, s.base_ticker, s.visibility
               FROM crypto_strategy_subscriptions sub
               JOIN crypto_strategies s ON s.id = sub.strategy_id
               WHERE sub.user_id = ? ORDER BY sub.created_at DESC""",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def get_due_strategy_subscriptions() -> list[Row]:
    """All active, unpaused subscriptions whose next_run_at <= now."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT sub.*, s.rules_json, s.name AS strategy_name, s.base_ticker
               FROM crypto_strategy_subscriptions sub
               JOIN crypto_strategies s ON s.id = sub.strategy_id
               WHERE sub.active = 1 AND sub.paused = 0
                 AND (sub.next_run_at IS NULL OR sub.next_run_at <= ?)""",
            (now,),
        ).fetchall()
    return _rows(rows)


def update_strategy_subscription_run(subscription_id: int, next_run_at: str,
                                     last_action: str = "") -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_strategy_subscriptions
               SET last_run_at = datetime('now'),
                   next_run_at = ?,
                   last_action = ?
               WHERE id = ?""",
            (next_run_at, last_action[:200], subscription_id),
        )


def set_strategy_subscription_paused(user_id: str, subscription_id: int,
                                     paused: bool) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE crypto_strategy_subscriptions SET paused = ? "
            "WHERE id = ? AND user_id = ?",
            (1 if paused else 0, subscription_id, user_id),
        )
        return cur.rowcount > 0


def delete_strategy_subscription(user_id: str, subscription_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM crypto_strategy_subscriptions WHERE id = ? AND user_id = ?",
            (subscription_id, user_id),
        )
        return cur.rowcount > 0


# ── Billing ─────────────────────────────────────────────────────────────────

def get_billing_row(user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, tier, stripe_customer_id, stripe_subscription_id, "
            "       status, current_period_end, updated_at "
            "FROM crypto_billing WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_billing_by_customer(stripe_customer_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, tier, stripe_customer_id, stripe_subscription_id, "
            "       status, current_period_end "
            "FROM crypto_billing WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_billing(user_id: str, tier: str,
                   stripe_customer_id: str | None = None,
                   stripe_subscription_id: str | None = None,
                   status: str = "active",
                   current_period_end: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_billing
                 (user_id, tier, stripe_customer_id, stripe_subscription_id,
                  status, current_period_end, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 tier = excluded.tier,
                 stripe_customer_id = COALESCE(excluded.stripe_customer_id, crypto_billing.stripe_customer_id),
                 stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, crypto_billing.stripe_subscription_id),
                 status = excluded.status,
                 current_period_end = COALESCE(excluded.current_period_end, crypto_billing.current_period_end),
                 updated_at = datetime('now')""",
            (user_id, tier, stripe_customer_id, stripe_subscription_id,
             status, current_period_end),
        )


# ── User preferences (digest, email) ────────────────────────────────────────

def get_user_preferences(user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, email, digest_enabled, digest_day_of_week, "
            "       last_digest_sent_at FROM crypto_user_preferences "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["digest_enabled"] = bool(d["digest_enabled"])
    return d


def upsert_user_preferences(user_id: str, email: str | None = None,
                            digest_enabled: bool | None = None,
                            digest_day_of_week: int | None = None) -> None:
    existing = get_user_preferences(user_id) or {}
    final_email = email if email is not None else existing.get("email")
    final_enabled = digest_enabled if digest_enabled is not None else existing.get("digest_enabled", True)
    final_dow = digest_day_of_week if digest_day_of_week is not None else existing.get("digest_day_of_week", 0)
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_user_preferences
                 (user_id, email, digest_enabled, digest_day_of_week, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 email = COALESCE(excluded.email, crypto_user_preferences.email),
                 digest_enabled = excluded.digest_enabled,
                 digest_day_of_week = excluded.digest_day_of_week,
                 updated_at = datetime('now')""",
            (user_id, final_email, 1 if final_enabled else 0,
             max(0, min(6, int(final_dow)))),
        )


def update_user_preference_digest_sent(user_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE crypto_user_preferences SET last_digest_sent_at = datetime('now') "
            "WHERE user_id = ?",
            (user_id,),
        )


def get_users_due_for_digest(today_dow: int, debounce_days: int = 6) -> list[dict]:
    """Users whose digest_day_of_week matches today AND haven't received in
    the last `debounce_days` days. Cron should run hourly; debounce avoids
    double-fires within a single day."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=debounce_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT user_id, email, digest_day_of_week, last_digest_sent_at
               FROM crypto_user_preferences
               WHERE digest_enabled = 1
                 AND digest_day_of_week = ?
                 AND email IS NOT NULL AND email != ''
                 AND (last_digest_sent_at IS NULL OR last_digest_sent_at < ?)""",
            (today_dow, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Referrals ───────────────────────────────────────────────────────────────

def get_referral_code(user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT code, created_at FROM crypto_referral_codes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_referral_by_code(code: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, created_at FROM crypto_referral_codes WHERE code = ?",
            (code,),
        ).fetchone()
    return dict(row) if row else None


def try_insert_referral_code(user_id: str, code: str) -> bool:
    """Insert IGNORE on (code, user_id). Returns True if the row was new
    (i.e. no collision on either column). Caller retries on False."""
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO crypto_referral_codes (code, user_id) VALUES (?, ?)",
                (code, user_id),
            )
            return cur.rowcount > 0
    except Exception:
        return False


def insert_referral_visit(referrer_user_id: str, anon_id: str,
                          referral_code: str, source: str) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_referral_visits
                 (referrer_user_id, anon_id, referral_code, source)
               VALUES (?, ?, ?, ?)""",
            (referrer_user_id, anon_id, referral_code, source),
        )
        return int(cur.lastrowid)


def get_latest_unbound_visit(anon_id: str) -> Optional[dict]:
    """Return the most recent visit for an anon_id that hasn't already been
    promoted to an attribution. Used at signup-time."""
    with _conn() as c:
        row = c.execute(
            """SELECT id, referrer_user_id, referral_code, source
               FROM crypto_referral_visits
               WHERE anon_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (anon_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_referral_visit(visit_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM crypto_referral_visits WHERE id = ?", (visit_id,))


def count_referral_visits(referrer_user_id: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM crypto_referral_visits WHERE referrer_user_id = ?",
            (referrer_user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def insert_referral_attribution(referred_user_id: str, referrer_user_id: str,
                                 referral_code: str, source: str) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_referral_attributions
                 (referred_user_id, referrer_user_id, referral_code, source)
               VALUES (?, ?, ?, ?)""",
            (referred_user_id, referrer_user_id, referral_code, source),
        )
        return int(cur.lastrowid)


def get_attribution_by_referred(referred_user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM crypto_referral_attributions
               WHERE referred_user_id = ?""",
            (referred_user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_attributions_by_referrer(referrer_user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, referred_user_id, referral_code, source, recorded_at,
                      converted_at, conversion_tier, conversion_value_cents,
                      payout_owed_cents, payout_status
               FROM crypto_referral_attributions
               WHERE referrer_user_id = ?
               ORDER BY recorded_at DESC""",
            (referrer_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_attribution_converted(attribution_id: int, conversion_tier: str,
                                conversion_value_cents: int,
                                payout_owed_cents: int) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_referral_attributions
               SET converted_at = datetime('now'),
                   conversion_tier = ?,
                   conversion_value_cents = ?,
                   payout_owed_cents = ?
               WHERE id = ?""",
            (conversion_tier, conversion_value_cents, payout_owed_cents,
             attribution_id),
        )


def update_attribution_tier(attribution_id: int, tier: str) -> None:
    """User upgraded tier post-conversion; bump the recorded tier but don't
    accrue another payout (payout cron handles ongoing months)."""
    with _conn() as c:
        c.execute(
            "UPDATE crypto_referral_attributions SET conversion_tier = ? WHERE id = ?",
            (tier, attribution_id),
        )


# ── News items + alert rules ────────────────────────────────────────────────

def upsert_news_items(rows: list[tuple]) -> dict:
    """Insert news items. Returns {new_ids: [...]} so the caller can fire
    alerts only on truly new items (RSS feeds repeat items every refresh).
    rows: (id, source, title, url, published_at, body_snippet, sentiment,
           topics, tickers, regulators, entities, tags)."""
    if not rows:
        return {"new_ids": []}
    new_ids = []
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                """INSERT OR IGNORE INTO crypto_news_items
                     (id, source, title, url, published_at, body_snippet,
                      sentiment, topics, tickers, regulators, entities, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r,
            )
            if cur.rowcount > 0:
                new_ids.append(r[0])
    return {"new_ids": new_ids}


def get_news_items(filters: dict, limit: int = 50) -> list[Row]:
    """Query news with filters. Tickers / regulators / topics are CSV
    columns; we use LIKE with '%' boundaries and a comma to match exactly
    one CSV entry without matching substrings (e.g. ',SEC,' won't match
    ',SECTOR,')."""
    sql = "SELECT * FROM crypto_news_items WHERE 1=1"
    params: list = []

    def _csv_match(col: str, needles: list[str]) -> tuple[str, list]:
        # Any-of match across the CSV column.
        sub = []
        sub_params: list = []
        for n in needles:
            sub.append(f"',' || {col} || ',' LIKE ?")
            sub_params.append(f"%,{n},%")
        return "(" + " OR ".join(sub) + ")", sub_params

    for col in ("tickers", "regulators", "topics", "sources"):
        vals = filters.get(col)
        if not vals:
            continue
        if not isinstance(vals, list):
            continue
        if col == "sources":
            # `source` is a single-value column, not CSV.
            placeholders = ",".join("?" * len(vals))
            sql += f" AND source IN ({placeholders})"
            params.extend(vals)
        else:
            clause, sub_params = _csv_match(col, vals)
            sql += " AND " + clause
            params.extend(sub_params)

    if filters.get("since"):
        sql += " AND scraped_at >= ?"
        params.append(str(filters["since"]))
    if filters.get("min_sentiment") is not None:
        sql += " AND sentiment >= ?"
        params.append(float(filters["min_sentiment"]))
    if filters.get("max_sentiment") is not None:
        sql += " AND sentiment <= ?"
        params.append(float(filters["max_sentiment"]))
    if filters.get("q"):
        sql += " AND (title LIKE ? OR body_snippet LIKE ?)"
        like = f"%{filters['q']}%"
        params.extend([like, like])

    sql += " ORDER BY scraped_at DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return _rows(rows)


def get_active_news_alert_rules() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, user_id, name, query_json, notify_push, notify_email
               FROM crypto_news_alert_rules WHERE active = 1"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["notify_push"] = bool(d["notify_push"])
        d["notify_email"] = bool(d["notify_email"])
        out.append(d)
    return out


def get_user_news_alert_rules(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, query_json, notify_push, notify_email,
                      active, created_at
               FROM crypto_news_alert_rules
               WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def insert_news_alert_rule(user_id: str, name: str, query_json: str,
                            notify_push: bool, notify_email: bool) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_news_alert_rules
                 (user_id, name, query_json, notify_push, notify_email)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, name[:120], query_json,
             1 if notify_push else 0, 1 if notify_email else 0),
        )
        return int(cur.lastrowid)


def update_news_alert_rule(user_id: str, rule_id: int, active: bool) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE crypto_news_alert_rules SET active = ? "
            "WHERE id = ? AND user_id = ?",
            (1 if active else 0, rule_id, user_id),
        )
        return cur.rowcount > 0


def delete_news_alert_rule(user_id: str, rule_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM crypto_news_alert_rules WHERE id = ? AND user_id = ?",
            (rule_id, user_id),
        )
        return cur.rowcount > 0


def insert_news_alert_history(rule_id: int, user_id: str, news_id: str) -> bool:
    """Insert OR IGNORE — the UNIQUE(rule_id, news_id) constraint enforces
    one fire per (rule, item). Returns True if a new history row was created."""
    with _conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO crypto_news_alert_history
                 (rule_id, user_id, news_id)
               VALUES (?, ?, ?)""",
            (rule_id, user_id, news_id),
        )
        return cur.rowcount > 0


def has_alert_fired(rule_id: int, news_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM crypto_news_alert_history "
            "WHERE rule_id = ? AND news_id = ? LIMIT 1",
            (rule_id, news_id),
        ).fetchone()
    return row is not None


def count_user_news_alert_rules(user_id: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM crypto_news_alert_rules "
            "WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


# ── Stubs for removed functions (gateway handles auth now) ──────────────────
# These are kept as no-ops so any residual server.py calls don't crash.

def validate_session(token: str) -> dict | None:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return None


def create_session(user_id: str, ip: str = "", user_agent: str = "", max_age: int = 604800) -> str:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return ""


def delete_session(token: str):
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass


def create_user(email: str, password: str, display_name: str = "", tier: str = "free") -> str | None:
    """User creation is managed by the gateway. This is a no-op stub."""
    return None


def cleanup_sessions():
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass
