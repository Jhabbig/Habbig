"""Add source_summaries_fts for rich-text source search.

Existing FTS tables that db.init_db already provisions:

  markets_fts        — content='market_snapshots', columns:
                         (market_slug, market_question, category) — rich text
  sources_fts        — content='source_credibility', columns:
                         (source_handle) — identifier only, no prose
  predictions_fts    — content='predictions', columns:
                         (content, source_handle, category) — rich text

The existing sources_fts is handle-only, which means searching "fed policy
analyst" returns nothing useful from a source's Claude-generated summary
page. We add a separate FTS table over source_summaries so long-form
source prose is actually searchable.

We do NOT touch markets_fts / sources_fts / predictions_fts here — db.py
already owns those tables and their triggers. Duplicating would create
double-insert bugs.
"""

from __future__ import annotations


revision = "115"
down_revision = "114"


def upgrade(cur) -> None:
    # Contentless FTS linked to source_summaries by rowid.
    # summary is the indexed column; source_handle is UNINDEXED so we can
    # SELECT it back without it counting toward the term match.
    cur.execute("DROP TABLE IF EXISTS source_summaries_fts")
    cur.execute("""
        CREATE VIRTUAL TABLE source_summaries_fts USING fts5(
            source_handle UNINDEXED,
            summary,
            tokenize='unicode61 remove_diacritics 2'
        )
    """)


def downgrade(cur) -> None:
    cur.execute("DROP TABLE IF EXISTS source_summaries_fts")
