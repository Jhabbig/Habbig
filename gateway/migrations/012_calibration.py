"""Calibration scoring table (F9).

Stores per-source calibration data: when a source says 80%, does the event
happen 80% of the time? Calibration is computed alongside accuracy during
the credibility recomputation pipeline.
"""

revision = "012"
down_revision = "011"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS source_calibration (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_handle       TEXT NOT NULL UNIQUE,
            calibration_score   REAL,
            calibration_data    TEXT,
            total_calibrated    INTEGER NOT NULL DEFAULT 0,
            last_computed_at    INTEGER NOT NULL
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_cal_source "
        "ON source_calibration(source_handle)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_cal_source")
    c.execute("DROP TABLE IF EXISTS source_calibration")
