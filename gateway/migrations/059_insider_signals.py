"""Insider trading signals — public-disclosure aggregation + correlation.

All data is derived from mandatory public disclosures (congressional
trades, SEC Form 4, Form 13F, unusual options flow, FEC campaign filings,
Senate lobbying). The tables:

  insider_signals
    One row per disclosed event. The fetchers (gateway/insider/*.py) are
    idempotent against ``external_id`` so re-polls don't duplicate rows.

  insider_market_correlations
    Claude Sonnet cross-references each new signal with active Polymarket
    / Kalshi markets. One row per (signal, market) link the correlator
    decides is meaningful. insider_score derives from signal_strength +
    disclosure_delay + amount_significance + correlation_confidence.

  insider_fetchers
    Per-source housekeeping: last fetch timestamps, error counters, whether
    the fetcher is currently enabled. Lets the admin page show fetcher
    health without cranking the jobs table.

  users: insider_alerts_enabled + insider_alert_threshold (0.6 default).
"""

revision = "059"
down_revision = "058"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_signals (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            source                   TEXT NOT NULL,
            external_id              TEXT NOT NULL,
            disclosed_at             INTEGER NOT NULL,
            event_at                 INTEGER,
            actor_name               TEXT NOT NULL,
            actor_role               TEXT,
            ticker                   TEXT,
            company_name             TEXT,
            action                   TEXT,
            amount_usd               REAL,
            amount_shares            REAL,
            raw_payload              TEXT,
            signal_strength          TEXT NOT NULL DEFAULT 'weak',
            disclosure_delay_days    REAL,
            amount_significance      REAL,
            committees               TEXT,
            relevant_sectors         TEXT,
            narrative                TEXT,
            fetched_at               INTEGER NOT NULL,
            UNIQUE(source, external_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_ticker ON insider_signals(ticker, disclosed_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_source ON insider_signals(source, disclosed_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_strength ON insider_signals(signal_strength, disclosed_at DESC)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_market_correlations (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id                 INTEGER NOT NULL,
            market_slug               TEXT NOT NULL,
            correlation_type          TEXT NOT NULL,
            correlation_explanation   TEXT,
            implied_direction         TEXT,
            implied_confidence        TEXT,
            insider_score             REAL NOT NULL DEFAULT 0,
            computed_at               INTEGER NOT NULL,
            notified_at               INTEGER,
            FOREIGN KEY (signal_id) REFERENCES insider_signals(id) ON DELETE CASCADE,
            UNIQUE(signal_id, market_slug)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_corr_market ON insider_market_correlations(market_slug, computed_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_corr_score ON insider_market_correlations(insider_score DESC, computed_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_corr_notify ON insider_market_correlations(notified_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS insider_fetchers (
            source              TEXT PRIMARY KEY,
            enabled             INTEGER NOT NULL DEFAULT 1,
            last_fetched_at     INTEGER,
            last_success_at     INTEGER,
            last_error_at       INTEGER,
            last_error_message  TEXT,
            consecutive_errors  INTEGER NOT NULL DEFAULT 0,
            rows_fetched_total  INTEGER NOT NULL DEFAULT 0
        )
    """)
    for source_name in (
        "congressional_trades", "sec_form4", "sec_form13f",
        "unusual_options", "fec_campaign", "lobbying",
    ):
        c.execute(
            "INSERT OR IGNORE INTO insider_fetchers (source, enabled) VALUES (?, 1)",
            (source_name,),
        )

    if _table_exists(c, "users"):
        ucols = _existing_cols(c, "users")
        if "insider_alerts_enabled" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN insider_alerts_enabled INTEGER NOT NULL DEFAULT 0")
        if "insider_alert_threshold" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN insider_alert_threshold REAL NOT NULL DEFAULT 0.6")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_notify")
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_score")
    c.execute("DROP INDEX IF EXISTS idx_insider_corr_market")
    c.execute("DROP TABLE IF EXISTS insider_market_correlations")
    c.execute("DROP INDEX IF EXISTS idx_insider_strength")
    c.execute("DROP INDEX IF EXISTS idx_insider_source")
    c.execute("DROP INDEX IF EXISTS idx_insider_ticker")
    c.execute("DROP TABLE IF EXISTS insider_signals")
    c.execute("DROP TABLE IF EXISTS insider_fetchers")
