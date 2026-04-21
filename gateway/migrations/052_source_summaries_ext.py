"""Source summaries — tolerant to earlier 029_source_summaries.py.

Creates the ``source_summaries`` table if not already present (older
branches shipped the same table via migration 029). Also adds a
``model`` column the on-demand summariser uses to track which Claude
model produced each row.

Additive only. Safe to re-run.
"""

revision = "052"
down_revision = "051"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c):
    if not _table_exists(c, "source_summaries"):
        c.execute("""
            CREATE TABLE source_summaries (
                source_handle           TEXT PRIMARY KEY,
                summary                 TEXT NOT NULL,
                model                   TEXT,
                generated_at            INTEGER NOT NULL,
                generated_by            TEXT,
                cache_valid_until       INTEGER,
                predictions_considered  INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_source_summaries_valid ON source_summaries(cache_valid_until)")
        return

    cols = _existing_cols(c, "source_summaries")
    if "model" not in cols:
        c.execute("ALTER TABLE source_summaries ADD COLUMN model TEXT")
    # Pre-035 branches named the PK column "handle"; tolerate that too —
    # but don't attempt a rename (SQLite < 3.25 lacks RENAME COLUMN and
    # the pragma flag varies by distro). The module reads via a helper
    # that tries both column names.


def downgrade(c):
    pass  # Additive column left; table drop belongs to migration 029's downgrade
