"""Tests for migration 188 — users.invite_token_id FK fix."""

from __future__ import annotations

import importlib
import sqlite3
import sys

import pytest


def _load_migration():
    """Import the migration module without going through the runner.

    The migrations package adds `db` to the import chain — but we want
    to drive the migration against a bare in-memory connection, so we
    import the module directly.
    """
    spec = importlib.util.spec_from_file_location(
        "_mig188",
        "/Users/shocakarel/Habbig/gateway/migrations/188_fix_users_invite_token_fk.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row_factory_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _seed_broken_state(c: sqlite3.Connection) -> None:
    """Reproduce the exact post-162 broken schema seen on the server.

    Mirrors the SQLite-FK-auto-rewrite gotcha: a users table whose stored
    CREATE SQL points at "invite_tokens_old" even though that table was
    dropped after rebuild. We turn FK enforcement off during the seed
    INSERT — the whole point of the bug is that the FK can't be
    satisfied, and we need a row in the table to test row preservation.
    """
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(
        """
        CREATE TABLE invite_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    # Create users with the broken FK (verbatim from server inspection).
    c.execute(
        """
        CREATE TABLE users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            username          TEXT UNIQUE NOT NULL,
            email             TEXT UNIQUE NOT NULL,
            password_hash     TEXT NOT NULL,
            password_salt     TEXT NOT NULL,
            created_at        INTEGER NOT NULL,
            is_admin          INTEGER NOT NULL DEFAULT 0,
            invite_token_id   INTEGER REFERENCES "invite_tokens_old"(id)
        )
        """
    )
    c.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("seed", "seed@example.com", "h", "s", 0, 0),
    )
    c.commit()
    c.execute("PRAGMA foreign_keys = ON")


def test_dangling_fk_is_detected():
    mig = _load_migration()
    c = _row_factory_conn()
    _seed_broken_state(c)
    assert mig._users_sql_has_dangling_fk(c) is True


def test_insert_fails_before_migration():
    """Pre-migration: INSERT into users fails because the FK target is gone."""
    c = _row_factory_conn()
    _seed_broken_state(c)
    with pytest.raises(sqlite3.OperationalError, match="invite_tokens_old"):
        c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("admin", "admin@example.com", "h", "s", 0, 1),
        )


def test_migration_fixes_fk_and_preserves_rows():
    mig = _load_migration()
    c = _row_factory_conn()
    _seed_broken_state(c)
    pre_rows = c.execute("SELECT id, username, email FROM users").fetchall()
    assert len(pre_rows) == 1

    mig.upgrade(c)

    # Stored CREATE SQL no longer mentions the dangling target.
    sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()["sql"]
    assert "invite_tokens_old" not in sql
    assert "invite_tokens" in sql  # the corrected target

    # Rows preserved.
    post_rows = c.execute("SELECT id, username, email FROM users").fetchall()
    assert len(post_rows) == 1
    assert post_rows[0]["username"] == "seed"
    assert post_rows[0]["email"] == "seed@example.com"


def test_insert_works_after_migration():
    mig = _load_migration()
    c = _row_factory_conn()
    _seed_broken_state(c)
    mig.upgrade(c)

    # The original failing insert now succeeds.
    c.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("admin", "admin@example.com", "h", "s", 0, 1),
    )
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_migration_is_idempotent():
    """Running upgrade twice on an already-fixed table is a no-op."""
    mig = _load_migration()
    c = _row_factory_conn()
    _seed_broken_state(c)
    mig.upgrade(c)

    pre_sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()["sql"]

    mig.upgrade(c)  # second invocation — should detect already-fixed state

    post_sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()["sql"]

    assert pre_sql == post_sql


def test_migration_skips_clean_db():
    """If a fresh DB was built from db.py SCHEMA, the migration is a no-op."""
    mig = _load_migration()
    c = _row_factory_conn()
    # Build a clean users table without the bug.
    c.execute(
        """
        CREATE TABLE invite_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            invite_token_id INTEGER REFERENCES invite_tokens(id) ON DELETE SET NULL
        )
        """
    )
    c.commit()

    assert mig._users_sql_has_dangling_fk(c) is False
    mig.upgrade(c)  # no-op
    # Nothing renamed or rebuilt — verify table still present.
    assert (
        c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
        == 1
    )
