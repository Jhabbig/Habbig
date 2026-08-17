"""SQLite connection + migrations for the narve sidecar.

One DB file (env NARVE_DB_PATH, default ~/.narve-terminal/terminal.db),
WAL mode. Migrations are numbered .sql files in narve_sidecar/migrations/,
recorded in schema_migrations.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DB_PATH = "~/.narve-terminal/terminal.db"


def utcnow() -> str:
    """UTC timestamp in the one format used everywhere: YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> str:
    return os.path.expanduser(os.environ.get("NARVE_DB_PATH") or DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    p = os.path.expanduser(path) if path else db_path()
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_", f.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in applied:
            continue
        conn.executescript(f.read_text())
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utcnow()),
        )
        conn.commit()
