-- narve Terminal v0.5.x — account context: who a source is, not just its
-- track record. bias is one of left|lean-left|center|lean-right|right|unknown
-- (enforced at ingest, not by the schema); topics is a space-or-comma-
-- separated list of beats (e.g. "djt midterms fed") used for deterministic
-- token-overlap relevance; followers is nullable (unknown != zero).

ALTER TABLE sources ADD COLUMN bias TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE sources ADD COLUMN region TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN affiliation TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN topics TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN followers INTEGER;
ALTER TABLE sources ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN notes TEXT NOT NULL DEFAULT '';
