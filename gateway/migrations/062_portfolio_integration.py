"""Portfolio integration tables — Polymarket + Kalshi connections + positions.

Polymarket uses on-chain wallet addresses (no API key, no secret), so
``polymarket_connections`` only stores a wallet address and a last-sync
timestamp. Treat the wallet as public information — no PII.

Kalshi logins produce a bearer token that expires every 24-48h. We
encrypt the token with CREDENTIALS_ENCRYPTION_KEY (Fernet) before
storing it; the plaintext never touches the DB. Passwords are NEVER
stored — we only keep the email for display and whatever token
Kalshi's /login endpoint hands back.

``user_positions`` is a denormalised view of "what does user X hold
across both platforms right now". The sync jobs upsert this table
every 10-15 minutes; everything that reads it treats the rows as
eventually-consistent and re-fetches when the caller wants real-time.

``users.bankroll_usd`` feeds the Kelly calculator. Default 0 means
"not configured"; the calculator refuses to recommend a size until
the user sets a value.

Indexes focus on the hot path: "fetch all positions for user X" and
"all active Kalshi tokens that need a refresh".
"""

from __future__ import annotations

import sqlite3


revision = "062"
down_revision = "061"


def _existing_cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c: sqlite3.Connection) -> None:
    # ── Polymarket: one wallet per user (no auth, on-chain). ────────────
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_connections (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL UNIQUE
                              REFERENCES users(id) ON DELETE CASCADE,
            wallet_address    TEXT NOT NULL,
            connected_at      INTEGER NOT NULL,
            last_synced_at    INTEGER,
            sync_error        TEXT,
            sync_error_count  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_poly_conn_last_sync "
        "ON polymarket_connections(last_synced_at)"
    )

    # ── Kalshi: one session per user, encrypted token. ──────────────────
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS kalshi_connections (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL UNIQUE
                              REFERENCES users(id) ON DELETE CASCADE,
            email             TEXT NOT NULL,
            encrypted_token   TEXT NOT NULL,
            member_id         TEXT,
            connected_at      INTEGER NOT NULL,
            last_synced_at    INTEGER,
            token_expires_at  INTEGER,
            sync_error        TEXT,
            sync_error_count  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_kalshi_conn_last_sync "
        "ON kalshi_connections(last_synced_at)"
    )

    # ── Positions: denormalised holdings across both platforms. ─────────
    # Primary read pattern: by user_id — dominate the index. Secondary
    # read "all positions on market X" for cross-user aggregation.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_positions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER NOT NULL
                               REFERENCES users(id) ON DELETE CASCADE,
            platform           TEXT NOT NULL CHECK (platform IN ('polymarket','kalshi')),
            market_id          TEXT NOT NULL,
            market_question    TEXT,
            side               TEXT NOT NULL CHECK (side IN ('yes','no')),
            shares             REAL NOT NULL DEFAULT 0,
            entry_price        REAL,
            current_price      REAL,
            position_value_usd REAL,
            unrealised_pnl_usd REAL,
            realised_pnl_usd   REAL NOT NULL DEFAULT 0,
            opened_at          INTEGER,
            last_synced_at     INTEGER NOT NULL,
            UNIQUE(user_id, platform, market_id, side)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_positions_user "
        "ON user_positions(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_positions_market "
        "ON user_positions(platform, market_id)"
    )

    # ── Bankroll field on users for Kelly sizing. ───────────────────────
    user_cols = _existing_cols(c, "users")
    if "bankroll_usd" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN bankroll_usd REAL NOT NULL DEFAULT 0"
        )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_user_positions_market")
    c.execute("DROP INDEX IF EXISTS idx_user_positions_user")
    c.execute("DROP INDEX IF EXISTS idx_kalshi_conn_last_sync")
    c.execute("DROP INDEX IF EXISTS idx_poly_conn_last_sync")
    c.execute("DROP TABLE IF EXISTS user_positions")
    c.execute("DROP TABLE IF EXISTS kalshi_connections")
    c.execute("DROP TABLE IF EXISTS polymarket_connections")
    # Leave bankroll_usd in place — additive column on users, matches the
    # pattern from prior migrations.
