"""Claude-powered prediction extraction: cache + backfill staging.

Replaces the regex/keyword filter path with Claude Haiku extraction. The
extractor is keyed on sha256(post.content) so the same post is only billed
once; cache rows carry a 30-day TTL. Stub rows (Claude failed, no API key,
etc.) are also cached so the extractor does not retry forever.

A separate staging table holds re-extractions of historically-processed
posts during the migration backfill. The admin approves switchover once
the sample has been reviewed — only then do `predictions` rows get
replaced from the staging data.

Schema:
  - prediction_extractions: cached extraction payloads keyed by content hash
  - predictions_reextracted: staging for the backfill job
"""

revision = "027"
down_revision = "022"


def upgrade(c):
    # ── Per-post extraction cache ────────────────────────────────────────
    #
    # post_hash = sha256(content).hexdigest() — keeps identical posts from
    # different scrape windows out of the billing path. The whole Claude
    # payload (all fields from the system prompt) is stored JSON-encoded
    # so future prompt schema changes can be detected via schema_version
    # (bumped by the extractor when the prompt changes).
    c.execute("""
        CREATE TABLE IF NOT EXISTS prediction_extractions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            post_hash             TEXT NOT NULL UNIQUE,
            schema_version        INTEGER NOT NULL DEFAULT 1,
            source_post_id        TEXT,
            source_handle         TEXT,
            generated_at          INTEGER NOT NULL,
            generated_by          TEXT NOT NULL,
            cache_valid_until     INTEGER NOT NULL,
            is_prediction         INTEGER NOT NULL DEFAULT 0,
            claim                 TEXT,
            direction             TEXT,
            explicit_probability  REAL,
            implicit_confidence   TEXT,
            time_frame            TEXT,
            category              TEXT,
            contains_sarcasm      INTEGER NOT NULL DEFAULT 0,
            is_conditional        INTEGER NOT NULL DEFAULT 0,
            raw_payload           TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pred_ext_hash ON prediction_extractions(post_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pred_ext_valid ON prediction_extractions(cache_valid_until)")

    # ── Backfill staging table ───────────────────────────────────────────
    #
    # Mirrors the predictions table plus diff-tracking columns. The admin
    # reviews `matches_original` and `diff_summary` before clicking
    # "approve switchover" to swap rows into predictions.
    c.execute("""
        CREATE TABLE IF NOT EXISTS predictions_reextracted (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            original_prediction_id INTEGER,
            source_handle         TEXT,
            market_id             TEXT,
            category              TEXT,
            direction             TEXT,
            predicted_probability REAL,
            content               TEXT NOT NULL,
            source_url            TEXT,
            extracted_at          INTEGER NOT NULL,
            claim                 TEXT,
            explicit_probability  REAL,
            implicit_confidence   TEXT,
            time_frame            TEXT,
            contains_sarcasm      INTEGER NOT NULL DEFAULT 0,
            is_conditional        INTEGER NOT NULL DEFAULT 0,
            matches_original      INTEGER NOT NULL DEFAULT 0,
            diff_summary          TEXT,
            FOREIGN KEY (original_prediction_id) REFERENCES predictions(id) ON DELETE SET NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pred_reextr_orig ON predictions_reextracted(original_prediction_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pred_reextr_diff ON predictions_reextracted(matches_original)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_pred_reextr_diff")
    c.execute("DROP INDEX IF EXISTS idx_pred_reextr_orig")
    c.execute("DROP TABLE IF EXISTS predictions_reextracted")
    c.execute("DROP INDEX IF EXISTS idx_pred_ext_valid")
    c.execute("DROP INDEX IF EXISTS idx_pred_ext_hash")
    c.execute("DROP TABLE IF EXISTS prediction_extractions")
