"""
Standalone migrations layered on top of ``db.init_db()``.

The original ``db.py`` is treated as frozen per DECISIONS.md "Existing code
state" clarification — schema work for incremental features lives here.
Every migration module exposes ``apply(conn)``; ``run_all()`` calls them in
filename order and is idempotent (each migration introspects
``PRAGMA table_info`` before issuing DDL).

Wired in from ``server.py`` immediately after ``db.init_db()``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

log = logging.getLogger("annoyance.migrations")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def run_all() -> None:
    """Idempotent. Safe to call repeatedly. Uses the thread-local conn from
    ``db._get_conn`` so we share the same WAL connection as the rest of the
    process. Each migration is wrapped in its own commit; one failure
    doesn't poison the others.
    """
    import db  # local import to avoid top-level cycle

    from . import _001_add_polarity

    migrations: list[Callable[[sqlite3.Connection], None]] = [
        _001_add_polarity.apply,
    ]
    conn = db._get_conn()
    for fn in migrations:
        name = getattr(fn, "__module__", fn.__name__)
        try:
            fn(conn)
            conn.commit()
        except Exception:
            log.exception("migration %s failed", name)
