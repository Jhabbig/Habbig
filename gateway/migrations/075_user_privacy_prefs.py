"""User privacy preference columns — promotes the inline ALTERs from
``security_routes._ensure_user_privacy_columns`` into a real migration
so test runs (and any fresh DB) always land both columns regardless of
whether ``security_routes`` has been imported yet.

Idempotent — uses the standard "try ALTER, swallow duplicate-column"
pattern that the older inline code used. Existing rows get the default
value (1, "all protections on"), matching the inline default.
"""

from __future__ import annotations

import sqlite3


revision = "075"
down_revision = "074"


_COLUMNS = (
    ("watermark_blur_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("devtools_blur_enabled", "INTEGER NOT NULL DEFAULT 1"),
)


def upgrade(c: sqlite3.Connection) -> None:
    for name, decl in _COLUMNS:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def downgrade(c: sqlite3.Connection) -> None:
    # SQLite < 3.35 can't DROP COLUMN. Leave the columns in place — they're
    # nullable-with-default and harmless if the feature is rolled back.
    return
