"""Per-user configuration for the Trading add-on (£25/mo).

Stores the user-tunable knobs surfaced by ``/settings/trading-addon``:

  * ``kelly_fraction``      — 1.0 (full), 0.5 (half, default), 0.25 (quarter).
  * ``max_cap_pct``         — hard ceiling on Kelly's suggested stake as a
                              percentage of bankroll (1-25, default 25).
  * ``auto_execute``        — when 1, the trading runner is allowed to place
                              bets automatically once narve's edge crosses
                              ``auto_execute_min_ev``. Defaults OFF; the UI
                              forces a modal confirmation before flipping on.
  * ``auto_execute_min_ev`` — EV threshold (percentage points) above which
                              auto-execute fires. Only meaningful when
                              ``auto_execute = 1``.
  * ``daily_cap``           — currency-amount ceiling on total spend per UTC
                              day. NULL = no cap. Stored as REAL (USD/GBP
                              ambiguity handled at the API surface via
                              ``daily_cap_currency``).
  * ``daily_cap_currency``  — 'USD' or 'GBP'; pairs with ``daily_cap``.
  * ``max_position_size``   — max single-position bet size in
                              ``daily_cap_currency`` units. NULL = uncapped.
  * ``cooldown_minutes``    — minutes of forced inactivity after a losing
                              bet resolves. NULL = no cooldown.
  * ``updated_at``          — unix seconds; populated on every PATCH.

Why a dedicated table rather than columns on ``users``:
  * The Trading add-on is opt-in and only ~5% of accounts will configure it
    — wide on ``users`` would burn IO for every page load that touches the
    users table.
  * Lets a future migration add per-currency caps, per-market exclusions,
    etc. without re-shaping ``users`` again.
  * Mirrors the ``user_bankroll`` / ``user_credentials`` / ``user_env_*``
    pattern already used throughout the codebase.

The whole row is optional — absence means "user has the add-on but hasn't
opened the settings page yet" and the API serves sensible defaults.
"""

from __future__ import annotations


revision = "176"
down_revision = "175"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_trading_addon_settings (
            user_id              INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            kelly_fraction       REAL NOT NULL DEFAULT 0.5,
            max_cap_pct          INTEGER NOT NULL DEFAULT 25,
            auto_execute         INTEGER NOT NULL DEFAULT 0,
            auto_execute_min_ev  REAL,
            daily_cap            REAL,
            daily_cap_currency   TEXT NOT NULL DEFAULT 'USD',
            max_position_size    REAL,
            cooldown_minutes     INTEGER,
            updated_at           INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
        )
        """
    )
    # CHECK constraints would have been nicer but SQLite alters can't add
    # them retroactively — the PATCH endpoint enforces bounds at the API
    # surface (kelly in {1.0, 0.5, 0.25}, max_cap 1-25, etc.).


def downgrade(cur) -> None:
    cur.execute("DROP TABLE IF EXISTS user_trading_addon_settings")
