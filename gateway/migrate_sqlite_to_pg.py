#!/usr/bin/env python3
"""One-shot data migration: copy the gateway's SQLite ``auth.db`` into PostgreSQL.

Usage:
    # target is taken from DATABASE_URL (must point at Postgres)
    DATABASE_URL=postgres://user:pass@host:5432/narve \\
        python migrate_sqlite_to_pg.py [--sqlite path/to/auth.db] [--truncate]

What it does:
  1. Creates the schema in the target Postgres DB (via db.init_db()).
  2. Copies every table row-for-row, preserving primary-key ids.
  3. Resets each table's identity sequence to MAX(id)+1 so new inserts don't
     collide with migrated rows.

Safe to dry-run against a scratch database first. By default it refuses to run
if the target already contains users, unless --truncate is passed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Tables in dependency-friendly order. FK constraints are not created on the
# Postgres side (they were never enforced under SQLite either), so ordering is
# only cosmetic — but keeping parents first keeps the logs readable.
TABLES = [
    "invite_tokens",
    "users",
    "sessions",
    "subscriptions",
    "enquiries",
    "password_resets",
    "trading_credentials",
    "trading_orders",
    "stripe_events",
    "superuser_keys",
    "superuser_key_templates",
    "superuser_key_usage_logs",
    "user_positions",
]


def _sqlite_columns(sconn: sqlite3.Connection, table: str) -> list[str]:
    rows = sconn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _sqlite_has_table(sconn: sqlite3.Connection, table: str) -> bool:
    row = sconn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sqlite",
        default=str(Path(__file__).parent / "auth.db"),
        help="path to the source SQLite auth.db (default: ./auth.db)",
    )
    ap.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE all target tables before loading (destructive)",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn.startswith("postgres"):
        print("ERROR: DATABASE_URL must be set to a postgres:// URL.", file=sys.stderr)
        return 2

    src_path = Path(args.sqlite)
    if not src_path.exists():
        print(f"ERROR: source SQLite DB not found: {src_path}", file=sys.stderr)
        return 2

    # Import db AFTER DATABASE_URL is set so it initialises in Postgres mode and
    # creates the target schema for us.
    import db as gwdb  # noqa: E402

    assert gwdb.IS_PG, "db module did not select the Postgres backend"
    print(f"→ target : {dsn.rsplit('@', 1)[-1]}")
    print(f"→ source : {src_path}")

    gwdb.init_db()  # ensure schema + idempotent column migrations exist

    sconn = sqlite3.connect(str(src_path))
    sconn.row_factory = sqlite3.Row

    import psycopg

    total = 0
    with psycopg.connect(dsn) as pg:
        with pg.cursor() as cur:
            # Guard against clobbering a populated target.
            cur.execute("SELECT COUNT(*) FROM users")
            existing = cur.fetchone()[0]
            if existing and not args.truncate:
                print(
                    f"ERROR: target already has {existing} users. "
                    "Re-run with --truncate to overwrite.",
                    file=sys.stderr,
                )
                return 3

            if args.truncate:
                # Reverse order, RESTART IDENTITY resets sequences too.
                cur.execute(
                    "TRUNCATE %s RESTART IDENTITY CASCADE"
                    % ", ".join(reversed(TABLES))
                )
                print("• truncated target tables")

            for table in TABLES:
                if not _sqlite_has_table(sconn, table):
                    print(f"• {table:28} — absent in source, skipped")
                    continue
                cols = _sqlite_columns(sconn, table)
                rows = sconn.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    print(f"• {table:28} — 0 rows")
                    continue
                collist = ", ".join(cols)
                placeholders = ", ".join(["%s"] * len(cols))
                sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"
                cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
                # Realign the identity sequence if this table has an id column.
                if "id" in cols:
                    cur.execute(
                        "SELECT setval("
                        "  pg_get_serial_sequence(%s, 'id'),"
                        "  COALESCE((SELECT MAX(id) FROM %s), 1),"
                        "  true)" % (f"'{table}'", table)
                    )
                print(f"• {table:28} — {len(rows)} rows")
                total += len(rows)
        pg.commit()

    sconn.close()
    print(f"\n✓ migration complete — {total} rows copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
