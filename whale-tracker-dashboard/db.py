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
        }
