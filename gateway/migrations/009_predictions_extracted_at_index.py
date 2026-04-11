"""Add an index on predictions.extracted_at for the feed-ordering hot path.

`list_recent_predictions`, `get_predictions_for_market`, and the topic-pull
queries all do `ORDER BY p.extracted_at DESC` over predictions. Without an
index this becomes a full sort once the table has more than a few thousand
rows — measurably slow on the live SQLite DB. The market-id compound index
already exists (`idx_predictions_market`), so we only need the standalone
extracted_at column index here.

Additive only — safe to re-run.
"""

revision = "009"
down_revision = "008"


def upgrade(c):
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_extracted "
        "ON predictions(extracted_at)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_predictions_extracted")
