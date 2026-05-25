from __future__ import annotations
"""SQLite database layer for the Whale Dashboard.

Tracks institutional whales (JPMorgan, Morgan Stanley, BlackRock, etc.) via
SEC filings: 13F-HR (quarterly long equity holdings), Form 4 (insider txns),
and Schedule 13D/G (5%+ stakes). Uses sqlite3 with WAL mode and a threading
lock, mirroring the pattern in midterm-dashboard/backend/database.py.

User auth is handled by the gateway; we only persist domain data here.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_DB_DIR = Path(__file__).resolve().parent
DB_PATH = _DB_DIR / "data.db"

_lock = threading.Lock()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with WAL mode and Row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_SCHEMA = """
-- Parent entities. JPMorgan files under ~30 different CIKs (JPM Asset Mgmt,
-- JPM Investment Mgmt, JPM Securities, etc.). The cik_map collapses them
-- into one logical entity so the dashboard doesn't undercount their book.
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_name     TEXT NOT NULL UNIQUE,
    slug            TEXT NOT NULL UNIQUE,         -- e.g. "jpmorgan"
    entity_type     TEXT,                          -- bank, hedge_fund, asset_mgr, family_office, activist
    description     TEXT,
    last_aum_usd    REAL,
    last_seen       TEXT
);

-- CIK -> entity. One CIK is one filer in EDGAR; many CIKs roll up to one entity.
CREATE TABLE IF NOT EXISTS cik_map (
    cik             INTEGER PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    sub_name        TEXT NOT NULL,                 -- "JPMorgan Chase Bank, NA"
    filing_authority TEXT,                          -- "13F", "13D", "Form 4", or "all"
    confidence      REAL DEFAULT 1.0,              -- 0..1, for fuzzy matches
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_cik_map_entity ON cik_map(entity_id);

-- 13F-HR filings: one row per (filer CIK, accession). The information_table
-- (positions) lives in `holdings` keyed by filing_id.
CREATE TABLE IF NOT EXISTS filings_13f (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cik             INTEGER NOT NULL,
    accession       TEXT NOT NULL UNIQUE,          -- e.g. "0001104659-25-001234"
    form_type       TEXT NOT NULL,                 -- "13F-HR" or "13F-HR/A"
    quarter_end     TEXT NOT NULL,                 -- "2026-03-31"
    filed_date      TEXT NOT NULL,
    total_value_usd REAL,                          -- sum of value across positions
    n_positions     INTEGER,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_13f_cik_q ON filings_13f(cik, quarter_end);

-- One row per (filing, ticker/cusip). 13F reports CUSIP not ticker; we resolve
-- the ticker via the cusip_ticker mapping table when available.
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id       INTEGER NOT NULL REFERENCES filings_13f(id) ON DELETE CASCADE,
    cusip           TEXT NOT NULL,
    ticker          TEXT,                          -- nullable until resolved
    issuer_name     TEXT NOT NULL,
    title_of_class  TEXT,
    shares          INTEGER NOT NULL,
    value_usd       REAL NOT NULL,
    put_call        TEXT,                          -- NULL, "Put", "Call"
    investment_disc TEXT,                          -- "SOLE", "DEFINED", "OTHER"
    UNIQUE(filing_id, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS idx_holdings_filing ON holdings(filing_id);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON holdings(ticker);
CREATE INDEX IF NOT EXISTS idx_holdings_cusip ON holdings(cusip);

-- CUSIP -> ticker mapping. Seeded from SEC's company_tickers.json + curated
-- corrections. CUSIPs are 9 chars; we usually match on the 8-char issuer prefix.
CREATE TABLE IF NOT EXISTS cusip_ticker (
    cusip8          TEXT PRIMARY KEY,              -- 8-char issuer prefix
    ticker          TEXT NOT NULL,
    issuer_name     TEXT,
    last_updated    TEXT
);

-- Q-over-Q deltas, computed after each ingest. This is the table the UI hits
-- for "biggest moves last quarter" — pre-computed so the timeline is fast.
CREATE TABLE IF NOT EXISTS holdings_delta (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    cusip           TEXT NOT NULL,
    ticker          TEXT,
    issuer_name     TEXT,
    quarter_end     TEXT NOT NULL,
    prev_shares     INTEGER NOT NULL DEFAULT 0,
    new_shares      INTEGER NOT NULL DEFAULT 0,
    delta_shares    INTEGER NOT NULL,
    delta_value_usd REAL,
    delta_pct       REAL,                          -- vs prev_shares; NULL if NEW
    action          TEXT NOT NULL,                 -- NEW | EXIT | ADD | TRIM | HOLD
    UNIQUE(entity_id, cusip, quarter_end)
);
CREATE INDEX IF NOT EXISTS idx_delta_entity_q ON holdings_delta(entity_id, quarter_end);
CREATE INDEX IF NOT EXISTS idx_delta_ticker_q ON holdings_delta(ticker, quarter_end);

-- Form 4 insider transactions (real-time-ish, 2 business day filing window).
CREATE TABLE IF NOT EXISTS insider_txns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession       TEXT NOT NULL,
    issuer_cik      INTEGER NOT NULL,
    issuer_ticker   TEXT,
    issuer_name     TEXT,
    insider_cik     INTEGER,
    insider_name    TEXT NOT NULL,
    insider_role    TEXT,                          -- Director, Officer, 10%+ Owner
    txn_date        TEXT NOT NULL,
    txn_code        TEXT,                          -- P (purchase), S (sale), A (grant), ...
    shares          REAL,
    price           REAL,
    value_usd       REAL,
    post_holdings   REAL,
    fetched_at      TEXT NOT NULL,
    UNIQUE(accession, insider_cik, txn_date, txn_code, shares, price)
);
CREATE INDEX IF NOT EXISTS idx_insider_issuer_date ON insider_txns(issuer_ticker, txn_date);

-- Schedule 13D / 13G filings: 5%+ stakes. 13D = activist intent.
CREATE TABLE IF NOT EXISTS activist_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession       TEXT NOT NULL UNIQUE,
    schedule        TEXT NOT NULL,                 -- "13D", "13G", "13D/A", "13G/A"
    filer_cik       INTEGER NOT NULL,
    filer_entity_id INTEGER REFERENCES entities(id),
    target_cik      INTEGER,
    target_ticker   TEXT,
    target_name     TEXT NOT NULL,
    filed_date      TEXT NOT NULL,
    event_date      TEXT,                          -- "Date of Event Which Requires Filing"
    ownership_pct   REAL,
    shares_owned    INTEGER,
    intent_summary  TEXT,                          -- parsed from Item 4 (best-effort)
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activist_target ON activist_filings(target_ticker);
CREATE INDEX IF NOT EXISTS idx_activist_filer ON activist_filings(filer_entity_id);

-- Polymarket correlation: when a whale filing lands, search Polymarket for
-- markets whose slug/title references the same ticker or company; record the
-- price at filing and at +24h / +7d / +30d so we can score "edge" later.
CREATE TABLE IF NOT EXISTS market_correlation (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table        TEXT NOT NULL,             -- "activist_filings", "insider_txns", "holdings_delta"
    source_id           INTEGER NOT NULL,
    polymarket_market_id TEXT NOT NULL,
    polymarket_slug     TEXT,
    polymarket_question TEXT,
    price_at_filing     REAL,
    price_24h_after     REAL,
    price_7d_after      REAL,
    price_30d_after     REAL,
    edge_bps            REAL,                      -- abs change, basis points
    recorded_at         TEXT NOT NULL,
    UNIQUE(source_table, source_id, polymarket_market_id)
);
CREATE INDEX IF NOT EXISTS idx_corr_source ON market_correlation(source_table, source_id);

-- Issuer watchlist for Form 4 / 13D polling. Seeded from SEC company_tickers.json
-- (every S&P-listed issuer) plus any ad-hoc additions. The Form 4 / 13D ingesters
-- iterate over this list and fetch each issuer's filings atom feed.
CREATE TABLE IF NOT EXISTS issuer_watchlist (
    cik             INTEGER PRIMARY KEY,
    ticker          TEXT,
    issuer_name     TEXT NOT NULL,
    market_cap_tier TEXT,                          -- "mega", "large", "mid", "small", or NULL
    last_form4_check TEXT,
    last_13d_check  TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_ticker ON issuer_watchlist(ticker);

-- Ingest run log so we can audit what's been pulled and when.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,                 -- "edgar_13f", "edgar_form4", "edgar_13d", ...
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,                 -- "running", "ok", "error"
    n_new           INTEGER DEFAULT 0,
    error           TEXT
);

-- ============================================================================
-- v2 tables
-- ============================================================================

-- Per-ticker, per-quarter aggregate computed from holdings_delta. Lets the UI
-- answer "which stocks are smart-money piling into?" in one query.
CREATE TABLE IF NOT EXISTS consensus_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter_end         TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    issuer_name         TEXT,
    n_whales_long       INTEGER NOT NULL DEFAULT 0,   -- distinct entities holding >0 shares
    n_whales_added      INTEGER NOT NULL DEFAULT 0,   -- ADD or NEW
    n_whales_trimmed    INTEGER NOT NULL DEFAULT 0,   -- TRIM or EXIT
    n_whales_new        INTEGER NOT NULL DEFAULT 0,
    n_whales_exited     INTEGER NOT NULL DEFAULT 0,
    consensus_score     REAL,                          -- (added - trimmed) / total
    aggregate_value_usd REAL,
    crowdedness_pctile  REAL,                          -- 0-100, vs all tickers this quarter
    computed_at         TEXT NOT NULL,
    UNIQUE(quarter_end, ticker)
);
CREATE INDEX IF NOT EXISTS idx_consensus_q ON consensus_snapshots(quarter_end);

-- User watchlists (gateway UUIDs, no user table here).
CREATE TABLE IF NOT EXISTS user_watchlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,                         -- "ticker" or "whale"
    target      TEXT NOT NULL,                          -- ticker symbol or entity slug
    note        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(user_id, kind, target)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlists(user_id);

-- Alert rules. The dispatcher polls the DB for fresh signals and matches them
-- against enabled rules; matches go into alert_deliveries and out via webhook.
CREATE TABLE IF NOT EXISTS alert_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    rule_type   TEXT NOT NULL,            -- "13d_filed", "cluster_buy", "whale_move", "consensus_cross"
    target      TEXT,                      -- ticker / whale_slug / NULL = any
    threshold   REAL,                      -- ownership %, insider count, etc.
    webhook_url TEXT,
    email       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_fired  TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_user ON alert_rules(user_id);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON alert_rules(enabled);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    fired_at        TEXT NOT NULL,
    source_table    TEXT NOT NULL,        -- "activist_filings", "insider_txns", ...
    source_id       INTEGER NOT NULL,
    payload         TEXT NOT NULL,         -- JSON
    delivery_status TEXT NOT NULL,         -- "sent", "failed", "skipped_no_webhook"
    response_code   INTEGER,
    error           TEXT,
    UNIQUE(rule_id, source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_rule ON alert_deliveries(rule_id);

-- CFTC Commitment of Traders — weekly futures positioning, free from cftc.gov.
CREATE TABLE IF NOT EXISTS cftc_cot (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_code         TEXT NOT NULL,    -- e.g. "CL" crude, "GC" gold, "ES" S&P
    market_name         TEXT NOT NULL,
    report_date         TEXT NOT NULL,    -- Tuesday-of-week, ISO date
    commercial_long     INTEGER,
    commercial_short    INTEGER,
    noncommercial_long  INTEGER,
    noncommercial_short INTEGER,
    nonreportable_long  INTEGER,
    nonreportable_short INTEGER,
    open_interest       INTEGER,
    fetched_at          TEXT NOT NULL,
    UNIQUE(market_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_cot_market ON cftc_cot(market_code, report_date);
"""


# Additive migrations — additional columns that came in v2 but the v1 schema
# didn't have. SQLite ALTER TABLE only supports adding columns, which is
# exactly what we need. We try each, swallowing the "duplicate column" error.
_MIGRATIONS = [
    "ALTER TABLE activist_filings ADD COLUMN intent_class TEXT",
    "ALTER TABLE activist_filings ADD COLUMN intent_score REAL",
    # Track where a CUSIP→ticker mapping came from: 'openfigi' is authoritative,
    # 'fuzzy_name' is the SEC company_tickers.json name-match fallback. We never
    # let a fuzzy_name row clobber an openfigi row.
    "ALTER TABLE cusip_ticker ADD COLUMN source TEXT DEFAULT 'fuzzy_name'",
]


def init_db() -> None:
    """Create tables if they don't exist + apply additive migrations.
    Idempotent — safe to call on every boot."""
    with _lock, get_conn() as conn:
        conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # "duplicate column name: ..." — the migration was already
                # applied. Any other error is genuine and worth logging.
                if "duplicate column" not in str(e).lower():
                    logger.warning("migration skipped: %s — %s", stmt, e)
    logger.info("whale-dashboard db initialised at %s", DB_PATH)


def upsert_entity(slug: str, parent_name: str, entity_type: Optional[str] = None,
                  description: Optional[str] = None) -> int:
    """Insert or update an entity, return its id."""
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO entities (slug, parent_name, entity_type, description)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 parent_name=excluded.parent_name,
                 entity_type=COALESCE(excluded.entity_type, entities.entity_type),
                 description=COALESCE(excluded.description, entities.description)""",
            (slug, parent_name, entity_type, description),
        )
        row = conn.execute("SELECT id FROM entities WHERE slug=?", (slug,)).fetchone()
        return int(row["id"])


def map_cik(cik: int, entity_id: int, sub_name: str,
            filing_authority: str = "all", confidence: float = 1.0) -> None:
    """Bind a CIK to a parent entity. Idempotent."""
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO cik_map (cik, entity_id, sub_name, filing_authority, confidence)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(cik) DO UPDATE SET
                 entity_id=excluded.entity_id,
                 sub_name=excluded.sub_name,
                 filing_authority=excluded.filing_authority,
                 confidence=excluded.confidence""",
            (cik, entity_id, sub_name, filing_authority, confidence),
        )


def entity_for_cik(cik: int) -> Optional[int]:
    """Return entity_id for a CIK, or None if not mapped."""
    with get_conn() as conn:
        row = conn.execute("SELECT entity_id FROM cik_map WHERE cik=?", (cik,)).fetchone()
        return int(row["entity_id"]) if row else None
