"""Reconcile bankroll columns — backfill ``users.bankroll`` then drop
``users.bankroll_usd`` (audits/audit_kelly.md CRIT-1).

Background
----------
Two parallel bankroll columns were added to ``users`` by different
features:

  * Migration 017 (``user_bankroll``) added ``users.bankroll REAL`` and
    ``users.kelly_fraction REAL NOT NULL DEFAULT 0.5``. This is the
    column used by ``queries/markets.py``, ``market_routes.py``,
    ``server.py`` (settings + dashboard renderers), and every test in
    ``tests/test_portfolio_integration.py``.

  * Migration 062 (``portfolio_integration``) added
    ``users.bankroll_usd REAL NOT NULL DEFAULT 0``. This is the column
    that ``gateway/portfolio/kelly.py`` (until the same audit-driven
    sweep that ships this migration) read and wrote via
    ``kelly.get_user_bankroll`` and ``kelly.set_user_bankroll``.

The audit found that POSTing to ``/api/kelly/bankroll`` wrote
``bankroll_usd`` while the user loading their own settings page hit
``db.get_user_bankroll`` which reads ``bankroll`` — so the value
silently diverged. Either endpoint "looked successful" to the user;
neither agreed on the stored number. Bet-sizing recommendations were
quietly run against whichever column the calling path happened to
read, often the never-updated default of 0.

Resolution
----------
Pick one column. The rest of the codebase uses ``users.bankroll``
(canonical column from migration 017), so we consolidate onto it:

  1. Backfill: copy any non-NULL ``users.bankroll_usd`` into
     ``users.bankroll`` for rows where ``bankroll IS NULL``. We never
     overwrite a populated ``bankroll`` — if a user wrote to both
     surfaces (e.g. settings page on Mon, Kelly bankroll endpoint on
     Tue), the canonical column wins. The non-canonical ``bankroll_usd``
     write is discarded; this matches the audit's recommended fix.

  2. Drop ``users.bankroll_usd``. SQLite ALTER TABLE DROP COLUMN landed
     in 3.35, but the cluster of FK rewrites that affect ``users``
     (migrations 162 + 188) makes the safest path the explicit
     rename-create-copy-drop dance — mirroring what 162's
     ``_rebuild_users`` does — so we both drop the unwanted column AND
     preserve the FK declarations that 162/188 stitched back together.

Idempotency
-----------
Both halves check for the column's presence before acting. Re-running
the migration on a DB that already lacks ``bankroll_usd`` is a no-op.
A fresh ``db.init_db()``-only DB (built from ``db.SCHEMA``) doesn't
have ``bankroll_usd`` either, so the migration is a no-op there too.

Why a table rebuild (not ``ALTER TABLE DROP COLUMN``)?
------------------------------------------------------
Migrations 162 and 188 rebuilt ``users`` to restore an
``ON DELETE SET NULL`` FK clause that SQLite's auto-FK-rewrite trashed.
Using ``ALTER TABLE users DROP COLUMN bankroll_usd`` would work on
modern SQLite, but the rebuild pattern is what 162/188 used so the FK
declarations stay handled by one well-tested code path. We re-declare
the same two FKs ``188_fix_users_invite_token_fk.py`` re-declared
(invite_token_id -> invite_tokens, referred_by_user_id -> users) so
the post-rebuild table matches the post-188 shape minus the unwanted
column.
"""

from __future__ import annotations


revision = "195"
down_revision = "194"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _backfill_bankroll_from_usd(c) -> None:
    """Copy non-NULL bankroll_usd into bankroll where bankroll IS NULL.

    The audit's recommendation is to never overwrite a populated
    canonical column; if a user wrote to both surfaces, the canonical
    value wins. Rows where both are NULL stay NULL (the "unset" state
    the settings UI renders as "Set a bankroll").

    Treat ``bankroll_usd = 0`` the same as NULL: migration 062
    declared it ``NOT NULL DEFAULT 0``, so every existing row has a
    ``0`` value unless the Kelly endpoint wrote a real number. Copying
    0 into a NULL ``bankroll`` would mask the "unset" state the UI
    depends on — and 0 is the "no recommendation" sentinel anyway, so
    we'd be changing one zero to another.
    """
    cols = _existing_cols(c, "users")
    if "bankroll_usd" not in cols:
        return  # nothing to backfill from
    if "bankroll" not in cols:
        # Defensive: migration 017 should have run, but be safe.
        c.execute("ALTER TABLE users ADD COLUMN bankroll REAL")
    c.execute(
        "UPDATE users "
        "SET bankroll = bankroll_usd "
        "WHERE bankroll IS NULL "
        "  AND bankroll_usd IS NOT NULL "
        "  AND bankroll_usd > 0"
    )


def _rebuild_users_without_bankroll_usd(c) -> None:
    """Rebuild ``users`` to drop the ``bankroll_usd`` column.

    Mirrors the rebuild pattern in ``188_fix_users_invite_token_fk``:
    rename, snapshot column metadata, recreate without the unwanted
    column, copy, drop, restore indexes.

    Both FKs re-declared by migration 188 are re-declared here so the
    post-195 ``users`` table CREATE SQL still carries
    ``invite_token_id -> invite_tokens(id) ON DELETE SET NULL`` and
    ``referred_by_user_id -> users(id) ON DELETE SET NULL``. PRAGMA
    table_info does not surface FKs; the rebuild path 188 used (and
    that this one reuses) explicitly appends the FK clauses so 195
    doesn't silently regress the FK fix.
    """
    cols = _existing_cols(c, "users")
    if "bankroll_usd" not in cols:
        # Already dropped (e.g. re-run, or fresh schema). No-op.
        return

    # The migration runner already wraps us in a transaction. Disable
    # FK enforcement for the rebuild so SQLite doesn't reject the
    # intermediate state (no other table references ``users`` directly
    # while the rebuild is in flight, but turning FKs off is the
    # documented-safe pattern).
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        c.execute("ALTER TABLE users RENAME TO users_drop_bankroll_usd")
        # Snapshot column metadata — same pattern as migration 188.
        cols_info = c.execute(
            "PRAGMA table_info(users_drop_bankroll_usd)"
        ).fetchall()
        col_decls = []
        col_names = []
        for col in cols_info:
            name = col["name"]
            # Skip the column we're dropping.
            if name == "bankroll_usd":
                continue
            col_names.append(name)
            pieces = [f'"{name}"', col["type"] or ""]
            if col["notnull"]:
                pieces.append("NOT NULL")
            if col["dflt_value"] is not None:
                pieces.append(f'DEFAULT {col["dflt_value"]}')
            if col["pk"]:
                pieces.append("PRIMARY KEY")
                if (col["type"] or "").upper() == "INTEGER":
                    pieces.append("AUTOINCREMENT")
            col_decls.append(" ".join(p for p in pieces if p))

        # Re-declare both FKs — see docstring for why. Only include
        # each FK clause if the column it references actually exists on
        # the snapshot, so a minimal users shape (some test harnesses,
        # plus the fresh ``db.SCHEMA`` path that doesn't carry the
        # invite-tokens columns yet) doesn't blow up the CREATE.
        fks: list[str] = []
        if "invite_token_id" in col_names:
            fks.append(
                'FOREIGN KEY ("invite_token_id") REFERENCES invite_tokens(id) '
                "ON DELETE SET NULL"
            )
        if "referred_by_user_id" in col_names:
            fks.append(
                'FOREIGN KEY ("referred_by_user_id") REFERENCES users(id) '
                "ON DELETE SET NULL"
            )
        ddl_parts = list(col_decls) + fks
        c.execute(f"CREATE TABLE users ({', '.join(ddl_parts)})")
        quoted_cols = ", ".join(f'"{n}"' for n in col_names)
        c.execute(
            f"INSERT INTO users ({quoted_cols}) "
            f"SELECT {quoted_cols} FROM users_drop_bankroll_usd"
        )
        c.execute("DROP TABLE users_drop_bankroll_usd")

        # Re-create the canonical indexes — anything extra recreates
        # itself at server boot via CREATE INDEX IF NOT EXISTS.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
            "ON users(email)"
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
            "ON users(username)"
        )
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def upgrade(c) -> None:
    # Backfill first so the rebuild copies the consolidated values.
    _backfill_bankroll_from_usd(c)
    _rebuild_users_without_bankroll_usd(c)


def downgrade(c) -> None:
    # Re-adding ``bankroll_usd`` would restore the split-brain bug the
    # canonical-column consolidation just fixed, so this migration is
    # one-way. The data the column held has already been merged into
    # ``bankroll`` by the upgrade; restoring a ``bankroll_usd = 0``
    # column would not recreate the previous state in any useful way.
    pass
