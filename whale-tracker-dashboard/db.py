"""SQLite schema + helpers for the whale tracker.

Three tables:
  - insider_txn   : Form 4 transactions (one row per (filing, reporter, line))
  - activist_stake: SC 13D / 13G filings (one row per filing)
  - ma_event      : 8-K filings flagged as M&A (one row per filing)

The schema is intentionally denormalised — we want a tiny, fast read path
for the dashboard, not a perfect filing model. Issuer ticker may be NULL
when EDGAR doesn't surface it on the filing index.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "whales.db"

_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_txn (
    accession         TEXT NOT NULL,
    line_no           INTEGER NOT NULL DEFAULT 0,
    filed_at          TEXT NOT NULL,
    reporter_cik      TEXT,
    reporter_name     TEXT,
    reporter_relation TEXT,
    issuer_cik        TEXT,
    issuer_ticker     TEXT,
    issuer_name       TEXT,
    txn_date          TEXT,
    txn_code          TEXT,
    shares            REAL,
    price             REAL,
    value_usd         REAL,
    is_buy            INTEGER NOT NULL DEFAULT 0,
    filing_url        TEXT,
    PRIMARY KEY (accession, line_no)
);
CREATE INDEX IF NOT EXISTS idx_insider_ticker_date ON insider_txn(issuer_ticker, txn_date);
CREATE INDEX IF NOT EXISTS idx_insider_filed ON insider_txn(filed_at);
CREATE INDEX IF NOT EXISTS idx_insider_isbuy ON insider_txn(is_buy, filed_at);

CREATE TABLE IF NOT EXISTS activist_stake (
    accession      TEXT PRIMARY KEY,
    filed_at       TEXT NOT NULL,
    filer_name     TEXT,
    filer_cik      TEXT,
    issuer_name    TEXT,
    issuer_ticker  TEXT,
    issuer_cik     TEXT,
    pct_owned      REAL,
    shares_owned   REAL,
    filing_type    TEXT,
    filing_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activist_filed ON activist_stake(filed_at);
CREATE INDEX IF NOT EXISTS idx_activist_ticker ON activist_stake(issuer_ticker);

CREATE TABLE IF NOT EXISTS ma_event (
    accession      TEXT PRIMARY KEY,
    filed_at       TEXT NOT NULL,
    issuer_name    TEXT,
    issuer_ticker  TEXT,
    issuer_cik     TEXT,
    items          TEXT,
    headline       TEXT,
    ma_score       REAL NOT NULL DEFAULT 0,
    filing_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ma_filed ON ma_event(filed_at);
CREATE INDEX IF NOT EXISTS idx_ma_score ON ma_event(ma_score DESC, filed_at DESC);

CREATE TABLE IF NOT EXISTS ingest_state (
    feed         TEXT PRIMARY KEY,
    last_run_at  TEXT,
    last_seen    TEXT
);

-- One row per Form 13F-HR filing (a fund's quarterly portfolio snapshot).
CREATE TABLE IF NOT EXISTS fund_filing (
    accession      TEXT PRIMARY KEY,
    filed_at       TEXT NOT NULL,
    period_of_report TEXT,             -- e.g. "2025-03-31"
    fund_cik       TEXT NOT NULL,
    fund_name      TEXT,
    total_value    REAL,               -- sum of holdings (as-reported units; see README)
    holding_count  INTEGER,
    filing_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_fund_filing_cik_period ON fund_filing(fund_cik, period_of_report);
CREATE INDEX IF NOT EXISTS idx_fund_filing_filed ON fund_filing(filed_at);

-- One row per holding line in a 13F filing.
CREATE TABLE IF NOT EXISTS fund_holding (
    accession      TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    fund_cik       TEXT NOT NULL,
    period_of_report TEXT,
    cusip          TEXT,
    issuer_name    TEXT,
    title_of_class TEXT,
    issuer_ticker  TEXT,                -- resolved via cusip→ticker map when possible
    value          REAL,                -- as-reported (see README for units caveat)
    shares         REAL,
    shares_type    TEXT,                -- 'SH' or 'PRN'
    put_call       TEXT,                -- 'Put', 'Call', or NULL
    PRIMARY KEY (accession, line_no)
);
CREATE INDEX IF NOT EXISTS idx_fund_holding_cik ON fund_holding(fund_cik, period_of_report);
CREATE INDEX IF NOT EXISTS idx_fund_holding_cusip ON fund_holding(cusip);
CREATE INDEX IF NOT EXISTS idx_fund_holding_ticker ON fund_holding(issuer_ticker);

-- Congressional periodic transaction reports (House + Senate).
CREATE TABLE IF NOT EXISTS congress_trade (
    transaction_id   TEXT PRIMARY KEY,
    chamber          TEXT NOT NULL,    -- 'House' or 'Senate'
    representative   TEXT,
    party            TEXT,             -- 'R', 'D', 'I' (not always available)
    state            TEXT,
    transaction_date TEXT,
    disclosure_date  TEXT,
    ticker           TEXT,
    asset_description TEXT,
    asset_type       TEXT,             -- 'Stock', 'Option', 'Bond', ...
    transaction_type TEXT,             -- 'Purchase', 'Sale', 'Exchange', ...
    amount_range     TEXT,             -- the raw band as disclosed
    amount_min       REAL,             -- midpoint min in $
    amount_max       REAL,
    comment          TEXT,
    source_url       TEXT
);
CREATE INDEX IF NOT EXISTS idx_congress_disclosure ON congress_trade(disclosure_date DESC);
CREATE INDEX IF NOT EXISTS idx_congress_ticker ON congress_trade(ticker, disclosure_date DESC);
CREATE INDEX IF NOT EXISTS idx_congress_rep ON congress_trade(representative);

-- Daily price closes (cached from Stooq). Tiny rows, big upside.
CREATE TABLE IF NOT EXISTS price_daily (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_price_ticker_date ON price_daily(ticker, date DESC);

-- Unusual options activity alerts (paid feed; unusual_whales adapter by default).
CREATE TABLE IF NOT EXISTS options_flow_trade (
    alert_id         TEXT PRIMARY KEY,
    alerted_at       TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    side             TEXT,
    sentiment        TEXT,
    sweep            INTEGER,
    strike           REAL,
    expiry           TEXT,
    premium          REAL,
    volume           REAL,
    open_interest    REAL,
    volume_oi_ratio  REAL,
    spot_price       REAL,
    source           TEXT,
    raw_url          TEXT
);
CREATE INDEX IF NOT EXISTS idx_options_ticker ON options_flow_trade(ticker, alerted_at DESC);
CREATE INDEX IF NOT EXISTS idx_options_alerted ON options_flow_trade(alerted_at DESC);
CREATE INDEX IF NOT EXISTS idx_options_premium ON options_flow_trade(premium DESC);

-- Dark pool / off-exchange block prints.
CREATE TABLE IF NOT EXISTS dark_pool_print (
    print_id      TEXT PRIMARY KEY,
    executed_at   TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    size          REAL,
    price         REAL,
    premium       REAL,
    market_center TEXT,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_dp_ticker ON dark_pool_print(ticker, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dp_executed ON dark_pool_print(executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dp_premium ON dark_pool_print(premium DESC);

-- CUSIP → ticker cache (resolved via OpenFIGI for 13F holdings).
CREATE TABLE IF NOT EXISTS cusip_ticker (
    cusip       TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    name        TEXT,
    exch_code   TEXT,
    resolved_at TEXT
);

-- One row per labeled filing outcome (used to compute Bayesian skill).
-- filer_type ∈ {insider, activist, congress}; filer_id is the natural key per type
-- (reporter_cik, filer_cik, representative respectively). source_id is the
-- filing/transaction id for traceability.
CREATE TABLE IF NOT EXISTS filer_outcome (
    filer_type       TEXT NOT NULL,
    filer_id         TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    direction        TEXT NOT NULL,   -- 'buy' or 'sell'
    ticker           TEXT NOT NULL,
    filing_date      TEXT NOT NULL,
    horizon_days     INTEGER NOT NULL,
    return_pct       REAL,            -- ticker return over the horizon
    benchmark_pct    REAL,            -- SPY return over the same horizon
    alpha_pct        REAL,            -- return_pct - benchmark_pct
    win              INTEGER NOT NULL,-- 1 if directional alpha > 0
    computed_at      TEXT NOT NULL,
    PRIMARY KEY (filer_type, filer_id, source_id, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_filer_outcome_filer ON filer_outcome(filer_type, filer_id);
CREATE INDEX IF NOT EXISTS idx_filer_outcome_ticker ON filer_outcome(ticker);
CREATE INDEX IF NOT EXISTS idx_filer_outcome_computed ON filer_outcome(computed_at);
"""


def init_db() -> None:
    with connect() as cx:
        cx.executescript(SCHEMA)
        cx.commit()


@contextmanager
def connect():
    cx = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA synchronous=NORMAL")
    try:
        yield cx
    finally:
        cx.close()


def upsert_insider_txns(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO insider_txn (
                accession, line_no, filed_at, reporter_cik, reporter_name,
                reporter_relation, issuer_cik, issuer_ticker, issuer_name,
                txn_date, txn_code, shares, price, value_usd, is_buy, filing_url
            ) VALUES (
                :accession, :line_no, :filed_at, :reporter_cik, :reporter_name,
                :reporter_relation, :issuer_cik, :issuer_ticker, :issuer_name,
                :txn_date, :txn_code, :shares, :price, :value_usd, :is_buy, :filing_url
            )
            """,
            rows,
        )
        return cur.rowcount


def upsert_activist_stake(row: dict) -> bool:
    with _lock, connect() as cx:
        cur = cx.execute(
            """
            INSERT OR REPLACE INTO activist_stake (
                accession, filed_at, filer_name, filer_cik, issuer_name,
                issuer_ticker, issuer_cik, pct_owned, shares_owned,
                filing_type, filing_url
            ) VALUES (
                :accession, :filed_at, :filer_name, :filer_cik, :issuer_name,
                :issuer_ticker, :issuer_cik, :pct_owned, :shares_owned,
                :filing_type, :filing_url
            )
            """,
            row,
        )
        return cur.rowcount > 0


def upsert_ma_event(row: dict) -> bool:
    with _lock, connect() as cx:
        cur = cx.execute(
            """
            INSERT OR REPLACE INTO ma_event (
                accession, filed_at, issuer_name, issuer_ticker, issuer_cik,
                items, headline, ma_score, filing_url
            ) VALUES (
                :accession, :filed_at, :issuer_name, :issuer_ticker, :issuer_cik,
                :items, :headline, :ma_score, :filing_url
            )
            """,
            row,
        )
        return cur.rowcount > 0


def have_accession(table: str, accession: str) -> bool:
    if table not in {"insider_txn", "activist_stake", "ma_event"}:
        raise ValueError(f"unknown table: {table}")
    with connect() as cx:
        row = cx.execute(
            f"SELECT 1 FROM {table} WHERE accession = ? LIMIT 1",  # noqa: S608 (table name validated above)
            (accession,),
        ).fetchone()
    return row is not None


def set_ingest_state(feed: str, last_seen: str) -> None:
    with _lock, connect() as cx:
        cx.execute(
            """
            INSERT INTO ingest_state (feed, last_run_at, last_seen)
            VALUES (?, datetime('now'), ?)
            ON CONFLICT(feed) DO UPDATE SET last_run_at = excluded.last_run_at,
                                            last_seen   = excluded.last_seen
            """,
            (feed, last_seen),
        )


def get_ingest_state() -> dict[str, dict]:
    with connect() as cx:
        rows = cx.execute(
            "SELECT feed, last_run_at, last_seen FROM ingest_state"
        ).fetchall()
    return {r["feed"]: dict(r) for r in rows}


def counts() -> dict[str, int]:
    with connect() as cx:
        return {
            "insider_txn":    cx.execute("SELECT COUNT(*) FROM insider_txn").fetchone()[0],
            "activist_stake": cx.execute("SELECT COUNT(*) FROM activist_stake").fetchone()[0],
            "ma_event":       cx.execute("SELECT COUNT(*) FROM ma_event").fetchone()[0],
            "fund_filing":    cx.execute("SELECT COUNT(*) FROM fund_filing").fetchone()[0],
            "fund_holding":   cx.execute("SELECT COUNT(*) FROM fund_holding").fetchone()[0],
            "congress_trade": cx.execute("SELECT COUNT(*) FROM congress_trade").fetchone()[0],
            "price_daily":    cx.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0],
            "filer_outcome":  cx.execute("SELECT COUNT(*) FROM filer_outcome").fetchone()[0],
            "options_flow_trade": cx.execute("SELECT COUNT(*) FROM options_flow_trade").fetchone()[0],
            "dark_pool_print":    cx.execute("SELECT COUNT(*) FROM dark_pool_print").fetchone()[0],
            "cusip_ticker":       cx.execute("SELECT COUNT(*) FROM cusip_ticker").fetchone()[0],
        }


def upsert_fund_filing(row: dict) -> bool:
    with _lock, connect() as cx:
        cur = cx.execute(
            """
            INSERT OR REPLACE INTO fund_filing (
                accession, filed_at, period_of_report, fund_cik, fund_name,
                total_value, holding_count, filing_url
            ) VALUES (
                :accession, :filed_at, :period_of_report, :fund_cik, :fund_name,
                :total_value, :holding_count, :filing_url
            )
            """,
            row,
        )
        return cur.rowcount > 0


def upsert_fund_holdings(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO fund_holding (
                accession, line_no, fund_cik, period_of_report, cusip,
                issuer_name, title_of_class, issuer_ticker, value, shares,
                shares_type, put_call
            ) VALUES (
                :accession, :line_no, :fund_cik, :period_of_report, :cusip,
                :issuer_name, :title_of_class, :issuer_ticker, :value, :shares,
                :shares_type, :put_call
            )
            """,
            rows,
        )
        return cur.rowcount


def upsert_congress_trades(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO congress_trade (
                transaction_id, chamber, representative, party, state,
                transaction_date, disclosure_date, ticker, asset_description,
                asset_type, transaction_type, amount_range, amount_min,
                amount_max, comment, source_url
            ) VALUES (
                :transaction_id, :chamber, :representative, :party, :state,
                :transaction_date, :disclosure_date, :ticker, :asset_description,
                :asset_type, :transaction_type, :amount_range, :amount_min,
                :amount_max, :comment, :source_url
            )
            """,
            rows,
        )
        return cur.rowcount


def upsert_options_flow(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO options_flow_trade (
                alert_id, alerted_at, ticker, side, sentiment, sweep,
                strike, expiry, premium, volume, open_interest,
                volume_oi_ratio, spot_price, source, raw_url
            ) VALUES (
                :alert_id, :alerted_at, :ticker, :side, :sentiment, :sweep,
                :strike, :expiry, :premium, :volume, :open_interest,
                :volume_oi_ratio, :spot_price, :source, :raw_url
            )
            """,
            rows,
        )
        return cur.rowcount


def upsert_dark_pool(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO dark_pool_print (
                print_id, executed_at, ticker, size, price, premium,
                market_center, source
            ) VALUES (
                :print_id, :executed_at, :ticker, :size, :price, :premium,
                :market_center, :source
            )
            """,
            rows,
        )
        return cur.rowcount


def upsert_cusip_tickers(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO cusip_ticker (cusip, ticker, name, exch_code, resolved_at)
            VALUES (:cusip, :ticker, :name, :exch_code, :resolved_at)
            """,
            rows,
        )
        return cur.rowcount


def lookup_cusip_ticker(cusip: str) -> str | None:
    if not cusip:
        return None
    with connect() as cx:
        row = cx.execute(
            "SELECT ticker FROM cusip_ticker WHERE cusip = ? LIMIT 1",
            (cusip.upper(),),
        ).fetchone()
    return row["ticker"] if row else None


def unresolved_cusips(limit: int = 100) -> list[str]:
    """CUSIPs that appear in fund_holding without a ticker and aren't in the cache."""
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT DISTINCT h.cusip
            FROM fund_holding h
            LEFT JOIN cusip_ticker c ON c.cusip = h.cusip
            WHERE h.cusip IS NOT NULL AND h.cusip != ''
              AND h.issuer_ticker IS NULL
              AND c.cusip IS NULL
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [r["cusip"] for r in rows]


def upsert_prices(ticker: str, rows: list[tuple[str, float]]) -> int:
    """rows = [(date, close), ...]"""
    if not rows:
        return 0
    with _lock, connect() as cx:
        cur = cx.executemany(
            "INSERT OR REPLACE INTO price_daily (ticker, date, close) VALUES (?, ?, ?)",
            [(ticker, d, c) for (d, c) in rows],
        )
        return cur.rowcount


def get_close_on_or_after(ticker: str, date: str) -> tuple[str, float] | None:
    with connect() as cx:
        row = cx.execute(
            "SELECT date, close FROM price_daily WHERE ticker = ? AND date >= ? "
            "ORDER BY date ASC LIMIT 1",
            (ticker, date),
        ).fetchone()
    return (row["date"], row["close"]) if row else None


def get_close_on_or_before(ticker: str, date: str) -> tuple[str, float] | None:
    with connect() as cx:
        row = cx.execute(
            "SELECT date, close FROM price_daily WHERE ticker = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (ticker, date),
        ).fetchone()
    return (row["date"], row["close"]) if row else None


def have_price(ticker: str, date: str) -> bool:
    with connect() as cx:
        row = cx.execute(
            "SELECT 1 FROM price_daily WHERE ticker = ? AND date = ? LIMIT 1",
            (ticker, date),
        ).fetchone()
    return row is not None


def upsert_filer_outcome(row: dict) -> bool:
    with _lock, connect() as cx:
        cur = cx.execute(
            """
            INSERT OR REPLACE INTO filer_outcome (
                filer_type, filer_id, source_id, direction, ticker,
                filing_date, horizon_days, return_pct, benchmark_pct,
                alpha_pct, win, computed_at
            ) VALUES (
                :filer_type, :filer_id, :source_id, :direction, :ticker,
                :filing_date, :horizon_days, :return_pct, :benchmark_pct,
                :alpha_pct, :win, :computed_at
            )
            """,
            row,
        )
        return cur.rowcount > 0


def have_outcome(filer_type: str, filer_id: str, source_id: str, horizon_days: int) -> bool:
    with connect() as cx:
        row = cx.execute(
            "SELECT 1 FROM filer_outcome WHERE filer_type = ? AND filer_id = ? "
            "AND source_id = ? AND horizon_days = ? LIMIT 1",
            (filer_type, filer_id, source_id, horizon_days),
        ).fetchone()
    return row is not None
