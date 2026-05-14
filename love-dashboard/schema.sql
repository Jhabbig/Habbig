-- Love Atlas — macro relationship metrics schema. SQLite.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS love_metrics (
    metric_id   TEXT NOT NULL,
    country     TEXT NOT NULL,
    period      TEXT NOT NULL,
    value       REAL NOT NULL,
    source      TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (metric_id, country, period)
);

CREATE INDEX IF NOT EXISTS idx_lm_metric_period   ON love_metrics (metric_id, period DESC);
CREATE INDEX IF NOT EXISTS idx_lm_country_period  ON love_metrics (country, period DESC);
CREATE INDEX IF NOT EXISTS idx_lm_metric_country  ON love_metrics (metric_id, country, period DESC);
