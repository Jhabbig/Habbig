"""Add ``api_keys.first_displayed_at`` for one-time-display enforcement (M16).

Background:
  ``api_v1.create_api_key`` returns the raw bearer token ONCE at
  creation. The corresponding ``get_api_key_raw`` helper claims to
  refuse a second display by checking ``first_displayed_at`` — but
  the column never existed in the schema (originally a TODO inside
  the function docstring; see audits/audit_api_v1.md CRIT-1). The
  result: every prod key was created via a blanket ``except
  Exception`` fallback that silently absorbed the
  ``OperationalError: no such column`` AND any other DB error.

  This migration lands the column so:
    * ``create_api_key`` can stamp it synchronously with creation.
    * Any future "show me my key once" GET path has a real
      structural check to refuse re-display.
    * The narrow ``except sqlite3.OperationalError`` fallback in
      ``api_v1.py`` only fires on legacy pre-migration databases
      and can be removed once every deploy is past 196.

Schema:
  ``api_keys.first_displayed_at INTEGER NULLABLE`` — unix epoch
  seconds, NULL until the key is first handed back to a client.
  ``create_api_key`` stamps NOW() in the same transaction as the
  INSERT, so any row created post-migration is born "already
  displayed" — exactly the desired semantics for the one-time
  download flow.

Idempotency:
  Reads ``PRAGMA table_info(api_keys)`` and skips the ADD COLUMN
  when the column already exists. Backfilling existing rows is a
  judgement call:
    * Leaving them NULL means a future GET handler would (wrongly)
      hand back their raw key, but we never stored the plaintext so
      there is nothing to hand back regardless — the GET handler
      MUST still 410 when the column is NULL on a row whose
      ``key_hash`` exists.
    * Backfilling to ``created_at`` says "treat every legacy key
      as already displayed", which is the safer default and what
      we do below.
"""

from __future__ import annotations


revision = "196"
down_revision = "194"  # 195 reserved for a sibling change


def upgrade(cur) -> None:
    cols = {row["name"] for row in cur.execute(
        "PRAGMA table_info(api_keys)",
    )}
    if "first_displayed_at" not in cols:
        cur.execute(
            "ALTER TABLE api_keys "
            "ADD COLUMN first_displayed_at INTEGER"
        )
        # Backfill existing rows as "already displayed" so a future
        # GET handler cannot retroactively hand back legacy keys.
        # The hash is one-way so there is nothing to hand back, but
        # the structural check stays consistent.
        cur.execute(
            "UPDATE api_keys "
            "SET first_displayed_at = COALESCE(created_at, CAST(strftime('%s','now') AS INTEGER)) "
            "WHERE first_displayed_at IS NULL"
        )


def downgrade(cur) -> None:
    # SQLite ALTER TABLE DROP COLUMN landed in 3.35 — but the
    # downgrade pattern in this project is best-effort, so we
    # leave the column in place and let a future migration tidy
    # it. Mirrors the convention in 194_blast_cursor.py.
    pass
