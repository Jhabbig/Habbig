-- Whale Watch — institutional filings schema.
-- SQLite. Created on first server boot if absent.
--
-- Sources:
--   - SEC EDGAR 13F-HR  (quarterly institutional holdings, >$100M AUM)
--   - SEC EDGAR 13D/13G (>=5% activist / passive beneficial ownership)
--   - SEC EDGAR Form 4  (insider buys/sells by officers, directors, 10%+ holders)
--
-- All three feed a unified "Live Feed" view via the filings_unified view.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── whales ──────────────────────────────────────────────────────────
-- The institutions we track. Seeded from data/whales.yaml on boot.
-- CIK = SEC Central Index Key (10-digit, zero-padded as text).
CREATE TABLE IF NOT EXISTS whales (
    cik             TEXT PRIMARY KEY,            -- 10-digit zero-padded SEC CIK
    name            TEXT NOT NULL,               -- "Berkshire Hathaway Inc"
    short_name      TEXT,                        -- "BRK" / "BlackRock"
    kind            TEXT NOT NULL,               -- 'fund'|'activist'|'insider'|'family_office'
    aum_usd_b       REAL,                        -- AUM in $B (approximate, manual)
    twitter         TEXT,                        -- handle, no @
    website         TEXT,
    notes           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,  -- 1 = include in scheduled scrapes
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whales_kind   ON whales (kind, is_active);
CREATE INDEX IF NOT EXISTS idx_whales_active ON whales (is_active);

-- ── filings_13f ─────────────────────────────────────────────────────
-- One row per (filer, quarter). The holdings array lives in
-- filings_13f_positions to keep this table light for list views.
CREATE TABLE IF NOT EXISTS filings_13f (
    accession_no    TEXT PRIMARY KEY,            -- SEC accession number
    cik             TEXT NOT NULL,
    period_of_report TEXT NOT NULL,              -- 'YYYY-MM-DD' end-of-quarter
    filed_at        TEXT NOT NULL,               -- 'YYYY-MM-DDTHH:MM:SSZ'
    form_type       TEXT NOT NULL DEFAULT '13F-HR',
    total_value_usd REAL,                        -- aggregate $ market value reported
    n_positions     INTEGER NOT NULL DEFAULT 0,
    raw_url         TEXT,                        -- link to filing index
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (cik) REFERENCES whales(cik) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_13f_cik_period ON filings_13f (cik, period_of_report DESC);
CREATE INDEX IF NOT EXISTS idx_13f_filed      ON filings_13f (filed_at DESC);

-- ── filings_13f_positions ───────────────────────────────────────────
-- One row per (accession, security). Quarter-over-quarter deltas are
-- computed on demand by joining to the previous quarter's row.
CREATE TABLE IF NOT EXISTS filings_13f_positions (
    accession_no    TEXT NOT NULL,
    cusip           TEXT NOT NULL,               -- 9-char security id
    ticker          TEXT,                        -- enriched after-the-fact
    issuer_name     TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    value_usd       REAL NOT NULL,               -- reported $ market value (000s in raw, *1000 stored here)
    pct_portfolio   REAL,                        -- value / total_value_usd
    PRIMARY KEY (accession_no, cusip),
    FOREIGN KEY (accession_no) REFERENCES filings_13f(accession_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_13fp_cusip  ON filings_13f_positions (cusip);
CREATE INDEX IF NOT EXISTS idx_13fp_ticker ON filings_13f_positions (ticker) WHERE ticker IS NOT NULL;

-- ── filings_13d ─────────────────────────────────────────────────────
-- Beneficial ownership filings: 13D (active intent), 13G (passive).
-- One row per filing. The interesting payload is the % held + the
-- "purpose of transaction" prose, which we store in summary.
CREATE TABLE IF NOT EXISTS filings_13d (
    accession_no    TEXT PRIMARY KEY,
    cik             TEXT NOT NULL,               -- filer CIK
    subject_cik     TEXT,                        -- target company CIK (may be NULL)
    subject_name    TEXT NOT NULL,               -- target company name
    subject_ticker  TEXT,
    form_type       TEXT NOT NULL,               -- 'SC 13D'|'SC 13G'|'SC 13D/A'|'SC 13G/A'
    event_date      TEXT NOT NULL,               -- 'date of event' on cover
    filed_at        TEXT NOT NULL,
    pct_held        REAL,                        -- percent of class held
    shares_held     INTEGER,
    summary         TEXT,                        -- extracted "purpose" or item 4 text
    is_activist     INTEGER NOT NULL DEFAULT 0,  -- heuristic: 13D non-amendment + activist filer
    raw_url         TEXT,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (cik) REFERENCES whales(cik) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_13d_filed    ON filings_13d (filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_13d_cik      ON filings_13d (cik, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_13d_subject  ON filings_13d (subject_ticker, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_13d_activist ON filings_13d (is_activist, filed_at DESC) WHERE is_activist = 1;

-- ── filings_form4 ───────────────────────────────────────────────────
-- Insider transactions (officers, directors, 10%+ holders).
CREATE TABLE IF NOT EXISTS filings_form4 (
    accession_no    TEXT PRIMARY KEY,
    cik             TEXT NOT NULL,               -- reporting person CIK
    reporter_name   TEXT NOT NULL,
    reporter_title  TEXT,                        -- 'CEO'|'Director'|'CFO'|...
    issuer_cik      TEXT,
    issuer_name     TEXT NOT NULL,
    issuer_ticker   TEXT,
    txn_date        TEXT NOT NULL,
    txn_code        TEXT,                        -- 'P'=open-mkt purchase, 'S'=sale, 'A'=grant, ...
    is_buy          INTEGER NOT NULL DEFAULT 0,  -- 1 if txn_code in ('P','A') and shares > 0
    shares          INTEGER,
    price_usd       REAL,
    value_usd       REAL,
    filed_at        TEXT NOT NULL,
    raw_url         TEXT,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_form4_filed   ON filings_form4 (filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_form4_ticker  ON filings_form4 (issuer_ticker, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_form4_buy     ON filings_form4 (is_buy, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_form4_reporter ON filings_form4 (cik, filed_at DESC);

-- ── watchlist ───────────────────────────────────────────────────────
-- Per-user followed tickers + filers. user_id is the gateway user id.
CREATE TABLE IF NOT EXISTS watchlist (
    user_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL,               -- 'whale' | 'ticker'
    target          TEXT NOT NULL,               -- CIK if kind='whale', ticker if kind='ticker'
    label           TEXT,                        -- optional display override
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind, target)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist (user_id);

-- ── unified feed view ──────────────────────────────────────────────
-- The Live Feed tab unions all three filing types, exposing a common
-- {filed_at, form, filer, subject, summary} shape.
DROP VIEW IF EXISTS filings_unified;
CREATE VIEW filings_unified AS
    SELECT
        '13F-HR'        AS form,
        f.accession_no  AS accession_no,
        f.cik           AS filer_cik,
        w.name          AS filer_name,
        NULL            AS subject_ticker,
        NULL            AS subject_name,
        f.period_of_report AS event_date,
        f.filed_at      AS filed_at,
        f.total_value_usd AS value_usd,
        f.n_positions   AS detail_count,
        f.raw_url       AS raw_url,
        NULL            AS summary
    FROM filings_13f f
    LEFT JOIN whales w ON w.cik = f.cik

    UNION ALL

    SELECT
        d.form_type     AS form,
        d.accession_no  AS accession_no,
        d.cik           AS filer_cik,
        w.name          AS filer_name,
        d.subject_ticker AS subject_ticker,
        d.subject_name  AS subject_name,
        d.event_date    AS event_date,
        d.filed_at      AS filed_at,
        NULL            AS value_usd,
        d.shares_held   AS detail_count,
        d.raw_url       AS raw_url,
        d.summary       AS summary
    FROM filings_13d d
    LEFT JOIN whales w ON w.cik = d.cik

    UNION ALL

    SELECT
        'Form 4'        AS form,
        f4.accession_no AS accession_no,
        f4.cik          AS filer_cik,
        f4.reporter_name AS filer_name,
        f4.issuer_ticker AS subject_ticker,
        f4.issuer_name  AS subject_name,
        f4.txn_date     AS event_date,
        f4.filed_at     AS filed_at,
        f4.value_usd    AS value_usd,
        f4.shares       AS detail_count,
        f4.raw_url      AS raw_url,
        f4.txn_code     AS summary
    FROM filings_form4 f4;
