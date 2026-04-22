"""Add users.preferred_language for the i18n foundation (EN/ES/DE/PT-BR).

The column stores the BCP-47 tag the user chose via the language switcher.
Null / 'en' means "use the default", so existing rows need no backfill.

Slot 125 is the assignment from the session brief. The actual current
head in this tree is 116 — any migrations filling slots 117-124 will
apply between 116 and this one in sort order and the chain holds.
"""

from __future__ import annotations


revision = "125"
down_revision = "116"


def upgrade(cur) -> None:
    # SQLite doesn't let us ADD COLUMN IF NOT EXISTS, so probe PRAGMA
    # first — rerunning migrations during local dev is common enough
    # that crashing here would be friendly-fire.
    # NB: the runner passes a sqlite3 Connection here, so chain
    # `.execute(...).fetchall()` — Connection has no standalone
    # fetchall() method.
    cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "preferred_language" not in cols:
        cur.execute(
            "ALTER TABLE users ADD COLUMN preferred_language TEXT DEFAULT 'en'"
        )


def downgrade(cur) -> None:
    # SQLite < 3.35 can't DROP COLUMN. Rebuild via the table-rename dance
    # only if the column exists. No-op otherwise so the migration is safe
    # to replay on a fresh DB.
    cols = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "preferred_language" not in cols:
        return
    # Preserve the original column list except preferred_language.
    keep = [c for c in cols if c != "preferred_language"]
    cols_sql = ", ".join(keep)
    cur.execute("CREATE TABLE users_new AS SELECT " + cols_sql + " FROM users")
    cur.execute("DROP TABLE users")
    cur.execute("ALTER TABLE users_new RENAME TO users")
