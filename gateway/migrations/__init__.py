"""Versioned DB migrations — raw-sqlite3 analogue of Alembic.

Every migration is a Python file in this package named `NNN_slug.py`
(e.g. `001_initial_schema.py`). Each file exposes:

    revision = "001"
    down_revision = None  # or "000"
    def upgrade(c: sqlite3.Connection) -> None: ...
    def downgrade(c: sqlite3.Connection) -> None: ...

A dedicated `schema_version` table tracks which migrations have been
applied. `upgrade_to_head()` is called at server startup and replays
any unapplied migrations in order.

This gives Alembic's guarantees (versioned, ordered, idempotent) without
pulling SQLAlchemy into an otherwise raw-sqlite3 codebase.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import time
from pathlib import Path
from typing import Optional

import db


log = logging.getLogger("migrations")


def _ensure_version_table() -> None:
    with db.conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " revision TEXT PRIMARY KEY,"
            " applied_at INTEGER NOT NULL"
            ")"
        )


def _applied_revisions() -> set[str]:
    _ensure_version_table()
    with db.conn() as c:
        rows = c.execute("SELECT revision FROM schema_version").fetchall()
    return {r["revision"] for r in rows}


def _discover_migrations() -> list:
    """Import every NNN_*.py in this package in sort order.

    Returns a list of module objects. Each must expose `revision`,
    `down_revision`, `upgrade`, and `downgrade`.
    """
    here = Path(__file__).parent
    mods = []
    for info in sorted(pkgutil.iter_modules([str(here)])):
        if not info.name[:3].isdigit():
            continue
        mod = importlib.import_module(f"migrations.{info.name}")
        if not hasattr(mod, "revision") or not hasattr(mod, "upgrade"):
            continue
        mods.append(mod)
    mods.sort(key=lambda m: m.revision)
    return mods


def upgrade_to_head() -> dict:
    """Apply every migration whose revision is not yet in schema_version.

    Called from server.py startup. Safe to run repeatedly — each migration
    is wrapped in a transaction and recorded atomically.
    """
    _ensure_version_table()
    applied = _applied_revisions()
    all_mods = _discover_migrations()
    to_apply = [m for m in all_mods if m.revision not in applied]

    if not to_apply:
        log.info("migrations: already at head (%d applied)", len(applied))
        return {"applied": 0, "head": all_mods[-1].revision if all_mods else None}

    count = 0
    for mod in to_apply:
        try:
            with db.conn() as c:
                mod.upgrade(c)
                c.execute(
                    "INSERT OR IGNORE INTO schema_version (revision, applied_at) VALUES (?, ?)",
                    (mod.revision, int(time.time())),
                )
            count += 1
            log.info("migrations: applied %s", mod.revision)
        except Exception as e:
            log.exception("migrations: %s failed: %s", mod.revision, e)
            raise
    return {"applied": count, "head": all_mods[-1].revision}


def downgrade(target: Optional[str] = None) -> dict:
    """Roll back to `target` revision (inclusive of `target`).

    If `target` is None, rolls back exactly one revision. Dangerous — only
    used for manual recovery and in tests.
    """
    applied = sorted(_applied_revisions())
    if not applied:
        return {"rolled_back": 0}
    to_rollback: list = []
    for rev in reversed(applied):
        to_rollback.append(rev)
        if target is None or rev == target:
            break
    all_mods = {m.revision: m for m in _discover_migrations()}
    count = 0
    for rev in to_rollback:
        mod = all_mods.get(rev)
        if not mod or not hasattr(mod, "downgrade"):
            continue
        try:
            with db.conn() as c:
                mod.downgrade(c)
                c.execute("DELETE FROM schema_version WHERE revision = ?", (rev,))
            count += 1
            log.info("migrations: downgraded %s", rev)
        except Exception as e:
            log.exception("migrations: downgrade %s failed: %s", rev, e)
            raise
    return {"rolled_back": count}


def current_revision() -> Optional[str]:
    applied = _applied_revisions()
    if not applied:
        return None
    return sorted(applied)[-1]
