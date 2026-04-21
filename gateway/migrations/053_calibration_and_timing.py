"""Calibration + timing columns for credibility scoring.

Calibration (Brier-score-based) rewards sources whose stated probabilities
match resolved outcomes. Timing rewards early + contrarian predictions —
sources who call it before the market reprices. Both contribute to the
global credibility score (see credibility/engine.py weights).

Columns added:

  source_prediction_records
    - predicted_probability_stated  REAL     what the source themselves said
    - calibration_contribution      REAL     per-record Brier delta
    - timing_score                  REAL     0.0–1.0 per compute_timing_score
    - edge_at_prediction            REAL     |source_prob − market_prob| at t

  sources
    - calibration_score             REAL     aggregated Brier, nullable
    - calibration_sample_size       INTEGER  count of scored records
    - calibration_unlocked          INTEGER  1 when sample_size ≥ 10
    - avg_timing_score              REAL     mean timing across resolved preds
    - early_predictor_rank          TEXT     'early' | 'average' | 'late'

Adapts gracefully when the target tables don't exist yet (pre-credibility-
pipeline branches): column adds are skipped and the migration still
records schema_version so the tree stays linear.

Additive only. Safe to re-run.
"""

revision = "053"
down_revision = "052"


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _add_if_missing(c, table: str, column: str, ddl: str) -> None:
    if table not in _table_exists._cached:  # type: ignore[attr-defined]
        _table_exists._cached[table] = _table_exists(c, table)  # type: ignore[attr-defined]
    if not _table_exists._cached[table]:
        return
    if column not in _existing_cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Simple memoisation — the upgrade function opens and closes hundreds of
# cursors otherwise. Attribute carries the cache for one migration run.
_table_exists._cached = {}  # type: ignore[attr-defined]


def upgrade(c):
    _table_exists._cached = {}  # type: ignore[attr-defined]
    _add_if_missing(c, "source_prediction_records", "predicted_probability_stated", "REAL")
    _add_if_missing(c, "source_prediction_records", "calibration_contribution", "REAL")
    _add_if_missing(c, "source_prediction_records", "timing_score", "REAL")
    _add_if_missing(c, "source_prediction_records", "edge_at_prediction", "REAL")

    _add_if_missing(c, "sources", "calibration_score", "REAL")
    _add_if_missing(c, "sources", "calibration_sample_size", "INTEGER NOT NULL DEFAULT 0")
    _add_if_missing(c, "sources", "calibration_unlocked", "INTEGER NOT NULL DEFAULT 0")
    _add_if_missing(c, "sources", "avg_timing_score", "REAL")
    _add_if_missing(c, "sources", "early_predictor_rank", "TEXT")


def downgrade(c):
    # Additive columns — no DROP COLUMN on older SQLite. Intentionally left.
    pass
