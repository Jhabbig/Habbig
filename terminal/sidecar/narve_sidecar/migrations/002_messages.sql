-- narve Terminal v0.5.x — people as sources + text messages.
-- sources.kind: 'model' (seeded sample models) | 'user' (texters; every
-- ingest auto-create is 'user'). messages hold raw texts; a message with
-- BOTH question_id and stance also lands a predictions row at ingest —
-- that is how texters enter the Beta(2,2) credibility loop.

ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'model';

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    question_id TEXT,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    stance_p REAL,
    text_hash TEXT NOT NULL,
    is_sample INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_id, sent_at, text_hash)
);
