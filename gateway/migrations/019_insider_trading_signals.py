"""Insider Trading Signal Detection tables (Feature: Insider Signals).

Three tables:
  - insider_signals: raw signals from public disclosures (congressional trades,
    SEC Form 4, FEC filings, unusual options activity, lobbying)
  - insider_market_correlations: Claude-generated links between insider signals
    and active prediction markets, with implied direction and edge
  - insider_fetchers: bookkeeping for each data source's fetch state

Plus user preference columns for insider alert configuration.

All data is derived from mandatory public government disclosures.
"""

revision = "019"
down_revision = "018"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # ── Core signals table ──────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_signals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type         TEXT NOT NULL,
            source_name         TEXT NOT NULL,
            source_type         TEXT NOT NULL,
            action              TEXT NOT NULL,
            asset_or_entity     TEXT NOT NULL,
            amount_usd          REAL,
            disclosed_at        INTEGER NOT NULL,
            transaction_at      INTEGER,
            delay_days          INTEGER,
            raw_data            TEXT,
            fetched_at          INTEGER NOT NULL,
            signal_strength     TEXT NOT NULL DEFAULT 'weak',
            filing_id           TEXT UNIQUE,
            committee           TEXT,
            party               TEXT,
            state               TEXT,
            chamber             TEXT
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_type_time "
        "ON insider_signals(signal_type, disclosed_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_strength "
        "ON insider_signals(signal_strength, disclosed_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_source "
        "ON insider_signals(source_name)"
    )

    # ── Market correlations table ───────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_market_correlations (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id                   INTEGER NOT NULL REFERENCES insider_signals(id) ON DELETE CASCADE,
            market_id                   TEXT NOT NULL,
            market_question             TEXT,
            correlation_type            TEXT NOT NULL DEFAULT 'indirect',
            correlation_explanation     TEXT,
            implied_direction           TEXT,
            implied_confidence          TEXT NOT NULL DEFAULT 'low',
            market_price_at_detection   REAL,
            insider_score               REAL,
            detected_at                 INTEGER NOT NULL,
            resolved                    INTEGER NOT NULL DEFAULT 0,
            resolved_correct            INTEGER
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_corr_market "
        "ON insider_market_correlations(market_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_corr_signal "
        "ON insider_market_correlations(signal_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_insider_corr_score "
        "ON insider_market_correlations(insider_score DESC)"
    )

    # ── Fetcher bookkeeping ─────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_fetchers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source              TEXT NOT NULL UNIQUE,
            last_fetched_at     INTEGER,
            records_fetched     INTEGER NOT NULL DEFAULT 0,
            errors              TEXT,
            next_fetch_at       INTEGER
        )
    """)

    # ── User preference columns ─────────────────────────────────────────
    cols = _existing_cols(c, "users")
    if "insider_alerts_enabled" not in cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN insider_alerts_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if "insider_alert_threshold" not in cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN insider_alert_threshold TEXT NOT NULL DEFAULT 'strong_only'"
        )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_score")
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_signal")
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_market")
    c.execute("DROP INDEX IF EXISTS idx_insider_strength")
    c.execute("DROP INDEX IF EXISTS idx_insider_type_time")
    c.execute("DROP INDEX IF EXISTS idx_insider_source")
    c.execute("DROP TABLE IF EXISTS insider_market_correlations")
    c.execute("DROP TABLE IF EXISTS insider_signals")
    c.execute("DROP TABLE IF EXISTS insider_fetchers")
