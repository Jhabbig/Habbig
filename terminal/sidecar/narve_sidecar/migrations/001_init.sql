-- narve Terminal v0.5 — initial schema (CONTRACT.md, exact).
-- Resolutions live ON questions (status/resolved_outcome/resolved_at) —
-- no separate table in v0.5.

CREATE TABLE IF NOT EXISTS sources(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    alpha REAL NOT NULL DEFAULT 2.0,
    beta REAL NOT NULL DEFAULT 2.0,
    is_sample INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'live',
    resolved_outcome INTEGER,
    resolved_at TEXT,
    is_sample INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions(
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    p REAL NOT NULL,
    stated_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(source_id, question_id, stated_at)
);

CREATE TABLE IF NOT EXISTS market_snapshots(
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    yes_price REAL NOT NULL,
    liquidity REAL,
    captured_at TEXT NOT NULL,
    UNIQUE(venue, market_id, captured_at)
);

CREATE TABLE IF NOT EXISTS credibility_events(
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    old_alpha REAL,
    old_beta REAL,
    new_alpha REAL,
    new_beta REAL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log(
    id INTEGER PRIMARY KEY,
    file_name TEXT,
    kind TEXT NOT NULL,
    rows_ok INTEGER NOT NULL,
    rows_err INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
