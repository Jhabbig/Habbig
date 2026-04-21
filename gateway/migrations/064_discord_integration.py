"""Discord integration: per-guild alert channels + per-user links.

Two tables because Discord's permission model is guild-scoped:

  discord_servers
    One row per guild the bot has been ``/narve setup``'d on. Each guild
    has one alert channel and one set of thresholds — any admin in the
    guild can tune them.

  discord_user_connections
    One row per user who has run ``/narve connect``. Scoped to a guild
    so the same Discord user can connect in multiple servers with
    potentially different narve accounts (paranoid but cheap).

The bot itself authenticates as a single bot token (env DISCORD_BOT_TOKEN);
these tables only hold state about *who* we're allowed to DM / which
channel receives broadcasts.
"""

from __future__ import annotations

import sqlite3


revision = "064"
down_revision = "063"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_servers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id            TEXT NOT NULL UNIQUE,
            alert_channel_id    TEXT,
            setup_by_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            connected_at        INTEGER NOT NULL,
            is_active           INTEGER NOT NULL DEFAULT 1,
            min_ev_threshold    REAL NOT NULL DEFAULT 0.05,
            min_credibility     REAL NOT NULL DEFAULT 0.7,
            send_best_bets      INTEGER NOT NULL DEFAULT 1,
            send_market_movers  INTEGER NOT NULL DEFAULT 1,
            send_insider        INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_servers_active "
        "ON discord_servers(is_active)"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_user_connections (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER NOT NULL
                               REFERENCES users(id) ON DELETE CASCADE,
            discord_user_id    TEXT NOT NULL UNIQUE,
            guild_id           TEXT,
            connected_at       INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_user_conn_user "
        "ON discord_user_connections(user_id)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_discord_user_conn_user")
    c.execute("DROP TABLE IF EXISTS discord_user_connections")
    c.execute("DROP INDEX IF EXISTS idx_discord_servers_active")
    c.execute("DROP TABLE IF EXISTS discord_servers")
