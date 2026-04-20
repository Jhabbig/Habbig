"""Calibration upgrade (Brier score) + market timing score fields.

Adds timing-related columns to source_credibility and computes market timing
scores for predictions. Calibration scoring already exists (migration 012) —
this migration adds the fields needed for Brier-score computation and
the reliability diagram's overconfident/underconfident detection.

Also adds market_price_at_prediction to the predictions table so we can
compute edge-at-prediction-time for timing scoring.
"""

revision = "020"
down_revision = "019"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # ── source_credibility: timing fields ───────────────────────────────
    cred_cols = _existing_cols(c, "source_credibility")
    if "avg_timing_score" not in cred_cols:
        c.execute("ALTER TABLE source_credibility ADD COLUMN avg_timing_score REAL")
    if "early_predictor_rank" not in cred_cols:
        c.execute("ALTER TABLE source_credibility ADD COLUMN early_predictor_rank TEXT")
    if "calibration_brier" not in cred_cols:
        c.execute(
            "ALTER TABLE source_credibility ADD COLUMN calibration_brier REAL"
        )

    # ── predictions: market context at prediction time ──────────────────
    pred_cols = _existing_cols(c, "predictions")
    if "market_price_at_prediction" not in pred_cols:
        c.execute(
            "ALTER TABLE predictions ADD COLUMN market_price_at_prediction REAL"
        )
    if "timing_score" not in pred_cols:
        c.execute("ALTER TABLE predictions ADD COLUMN timing_score REAL")
    if "edge_at_prediction" not in pred_cols:
        c.execute("ALTER TABLE predictions ADD COLUMN edge_at_prediction REAL")

    # ── source_calibration: Brier + overconfidence fields ───────────────
    cal_cols = _existing_cols(c, "source_calibration")
    if "brier_score" not in cal_cols:
        c.execute("ALTER TABLE source_calibration ADD COLUMN brier_score REAL")
    if "is_overconfident" not in cal_cols:
        c.execute(
            "ALTER TABLE source_calibration ADD COLUMN is_overconfident INTEGER NOT NULL DEFAULT 0"
        )
    if "is_underconfident" not in cal_cols:
        c.execute(
            "ALTER TABLE source_calibration ADD COLUMN is_underconfident INTEGER NOT NULL DEFAULT 0"
        )
    if "is_calibrated" not in cal_cols:
        c.execute(
            "ALTER TABLE source_calibration ADD COLUMN is_calibrated INTEGER NOT NULL DEFAULT 0"
        )


def downgrade(c):
    pass  # additive-only columns; safe to leave in place
