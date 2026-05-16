"""Shared in-memory test DB setup for the new feature tests.

Each test file in this package imports this module instead of
monkey-patching `db.conn` itself, so they all end up talking to the same
in-memory sqlite connection. Without this sharing, pytest would load
every test file at collection time and the LAST file's `db.conn` patch
would win, breaking the others.

Import this module BEFORE importing `email_system`, `jobs`, `migrations`,
or `server`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from pathlib import Path

# Pull the gateway package onto the path for every test that imports this.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


# Only patch once per process.
if not getattr(db.conn, "_is_test_fake", False):
    _fake_conn._is_test_fake = True  # type: ignore[attr-defined]
    db.conn = _fake_conn
    db.init_db()

    # The ``background_jobs`` table is created lazily by
    # ``jobs.backend._ensure_jobs_table`` rather than by a migration. In
    # production, ``server.py`` calls ``upgrade_to_head()`` before the job
    # backend starts, but the table is hot-created out-of-band before
    # migration 199 ships (see its docstring). Tests have no such backstop,
    # so we pre-create the table here to keep migration 199 (composite
    # index on ``background_jobs``) from blowing up on a fresh in-memory DB.
    from jobs.backend import _ensure_jobs_table as _ensure_jobs_table_for_tests
    _ensure_jobs_table_for_tests()

    import migrations  # noqa: E402
    migrations.upgrade_to_head()

    # Force emails into dry-run so the service never hits the network.
    os.environ.setdefault("EMAIL_DRY_RUN", "true")
    # Force in-process job backend.
    os.environ.pop("REDIS_HOST", None)
