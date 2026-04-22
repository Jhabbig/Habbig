"""Populate source_summaries_fts + wire triggers to keep it in sync.

Existing FTS tables (markets_fts, sources_fts, predictions_fts) are
managed by db.init_db — they get rebuilt at boot if empty and have
INSERT/UPDATE/DELETE triggers already installed. This migration only
adds the triggers + bulk-load for source_summaries_fts (new in 115).
"""

from __future__ import annotations


revision = "116"
down_revision = "115"


def upgrade(cur) -> None:
    # Bulk-populate from every existing summary row.
    cur.execute("""
        INSERT INTO source_summaries_fts (rowid, source_handle, summary)
        SELECT id, source_handle, COALESCE(summary, '') FROM source_summaries
    """)

    # INSERT trigger — a new summary lands, index it.
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS source_summaries_ai
        AFTER INSERT ON source_summaries BEGIN
            INSERT INTO source_summaries_fts (rowid, source_handle, summary)
            VALUES (new.id, new.source_handle, COALESCE(new.summary, ''));
        END
    """)
    # UPDATE — contentless FTS doesn't support rowid updates directly; we
    # delete+reinsert by rowid so the index stays coherent.
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS source_summaries_au
        AFTER UPDATE ON source_summaries BEGIN
            DELETE FROM source_summaries_fts WHERE rowid = new.id;
            INSERT INTO source_summaries_fts (rowid, source_handle, summary)
            VALUES (new.id, new.source_handle, COALESCE(new.summary, ''));
        END
    """)
    # DELETE — drop the matching FTS row.
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS source_summaries_ad
        AFTER DELETE ON source_summaries BEGIN
            DELETE FROM source_summaries_fts WHERE rowid = old.id;
        END
    """)


def downgrade(cur) -> None:
    for t in ("source_summaries_ai", "source_summaries_au", "source_summaries_ad"):
        cur.execute(f"DROP TRIGGER IF EXISTS {t}")
    cur.execute("DELETE FROM source_summaries_fts")
