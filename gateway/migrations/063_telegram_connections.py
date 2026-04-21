"""Telegram bot per-user link table.

Each row ties a narve user to a Telegram chat. The flow:

  1. User /start's the bot.
  2. Bot replies with a link: https://narve.ai/connect/telegram?token=<link_token>.
  3. User (already logged into narve) clicks the link.
  4. /connect/telegram validates the session + link_token, writes a row.

Notification preferences live here rather than on ``users`` because:

* They're Telegram-specific (send_best_bets, min_ev_threshold) and don't
  apply to the email or web channels.
* One user could link multiple Telegram chats (rare, but the schema
  shouldn't block it) — per-chat preferences keep the shape clean.

``link_token`` is short-lived (handler enforces the TTL; no schema
support needed) and one-shot.
"""

from __future__ import annotations

import sqlite3


revision = "063"
down_revision = "062"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_connections (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER NOT NULL
                                 REFERENCES users(id) ON DELETE CASCADE,
            telegram_chat_id     INTEGER NOT NULL UNIQUE,
            telegram_username    TEXT,
            link_token           TEXT UNIQUE,
            connected_at         INTEGER NOT NULL,
            is_active            INTEGER NOT NULL DEFAULT 1,
            send_best_bets       INTEGER NOT NULL DEFAULT 1,
            send_market_movers   INTEGER NOT NULL DEFAULT 1,
            send_insider         INTEGER NOT NULL DEFAULT 1,
            send_resolution      INTEGER NOT NULL DEFAULT 1,
            morning_briefing     INTEGER NOT NULL DEFAULT 1,
            min_ev_threshold     REAL NOT NULL DEFAULT 0.05,
            min_credibility      REAL NOT NULL DEFAULT 0.7
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_conn_user "
        "ON telegram_connections(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_conn_active "
        "ON telegram_connections(is_active)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_telegram_conn_active")
    c.execute("DROP INDEX IF EXISTS idx_telegram_conn_user")
    c.execute("DROP TABLE IF EXISTS telegram_connections")
