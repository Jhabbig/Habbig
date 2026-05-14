-- Voters Dashboard — user-thoughts layer schema.
-- SQLite. Created on first server boot if absent.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── thoughts ───────────────────────────────────────────────────────
-- A "thought" is any user contribution: a comment, a reaction, or
-- (in slice 4) a counter-chain. Slice 1 only uses kinds 'comment'
-- and 'reaction'.
CREATE TABLE IF NOT EXISTS thoughts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,            -- gateway user id
    user_email      TEXT NOT NULL,               -- denormalised for display
    target_type     TEXT NOT NULL,               -- 'country' | 'poll' | 'election' | 'chain' (later) | 'step' (later)
    target_id       TEXT NOT NULL,               -- e.g. ISO3 for country, election id, etc.
    kind            TEXT NOT NULL,               -- 'comment' | 'reaction'
    body            TEXT,                        -- markdown for comments; emoji shortcode for reactions
    parent_id       INTEGER,                     -- for threaded replies; NULL for top-level
    created_at      INTEGER NOT NULL,            -- unix seconds
    edited_at       INTEGER,
    hidden_at       INTEGER,                     -- soft-delete timestamp
    hidden_by       INTEGER,                     -- reviewer user_id, NULL if auto-hidden by flag count
    hidden_reason   TEXT,
    upvotes         INTEGER NOT NULL DEFAULT 0,
    downvotes       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES thoughts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_thoughts_target ON thoughts (target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_thoughts_user   ON thoughts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_thoughts_parent ON thoughts (parent_id) WHERE parent_id IS NOT NULL;

-- ── flags ──────────────────────────────────────────────────────────
-- One flag per (thought, user). 3 distinct flags auto-hide the thought.
CREATE TABLE IF NOT EXISTS thought_flags (
    thought_id      INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    reason          TEXT,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (thought_id, user_id),
    FOREIGN KEY (thought_id) REFERENCES thoughts(id) ON DELETE CASCADE
);

-- ── votes ──────────────────────────────────────────────────────────
-- Track who upvoted/downvoted what so we can toggle and prevent dupes.
CREATE TABLE IF NOT EXISTS thought_votes (
    thought_id      INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    vote            INTEGER NOT NULL,           -- +1 / -1
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (thought_id, user_id),
    FOREIGN KEY (thought_id) REFERENCES thoughts(id) ON DELETE CASCADE
);

-- ── audit log ──────────────────────────────────────────────────────
-- Every moderation action gets a row. Append-only. Reviewers can
-- always see the full history of a thought.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id        INTEGER NOT NULL,
    actor_email     TEXT NOT NULL,
    action          TEXT NOT NULL,              -- 'create' | 'edit' | 'flag' | 'auto_hide' | 'hide' | 'unhide' | 'vote'
    target_type     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    meta_json       TEXT,                       -- arbitrary JSON payload
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log (actor_id, created_at DESC);

-- ── rate limiting ──────────────────────────────────────────────────
-- Simple sliding-window counter; cheaper than touching a Redis from a
-- single-process dashboard.
CREATE TABLE IF NOT EXISTS rate_limit_log (
    user_id         INTEGER NOT NULL,
    action          TEXT NOT NULL,              -- 'comment' | 'flag' | 'chain_create'
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ratelimit ON rate_limit_log (user_id, action, created_at);

-- ── impact_chains (slice 3) ───────────────────────────────────────
-- A chain is a typed sequence of steps connecting voter concern → candidate
-- position → policy → market impact. Each chain belongs to one country and
-- has a curation lifecycle: draft → under_review → approved | rejected.
-- Approved chains show up in the public country drawer; drafts are visible
-- only to the author and reviewers.
CREATE TABLE IF NOT EXISTS impact_chains (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    iso             TEXT NOT NULL,              -- country ISO3 the chain belongs to
    title           TEXT NOT NULL,              -- short human title
    summary         TEXT,                       -- one-paragraph plain-language summary
    author_id       INTEGER NOT NULL,
    author_email    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft|under_review|approved|rejected
    source_kind     TEXT NOT NULL DEFAULT 'user',   -- user|seed (curated YAML)
    parent_chain_id INTEGER,                    -- if this is a counter-chain (slice 4)
    counter_kind    TEXT,                       -- 'refute'|'fork'|'extend' for counter-chains
    created_at      INTEGER NOT NULL,
    submitted_at    INTEGER,                    -- when moved draft -> under_review
    decided_at      INTEGER,                    -- when moved into approved|rejected
    decided_by      INTEGER,                    -- reviewer user_id
    review_notes    TEXT,                       -- last reviewer's comment
    upvotes         INTEGER NOT NULL DEFAULT 0,
    downvotes       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_chain_id) REFERENCES impact_chains(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_chains_iso     ON impact_chains (iso, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chains_status  ON impact_chains (status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_chains_author  ON impact_chains (author_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chains_parent  ON impact_chains (parent_chain_id) WHERE parent_chain_id IS NOT NULL;

-- ── impact_chain_steps ────────────────────────────────────────────
-- Ordered nodes in a chain. Step kinds form the canonical chain grammar:
--   concern   — what voters care about (e.g. "Inflation 38% top issue")
--   actor     — politician/party/movement (e.g. "Milei")
--   policy    — concrete action (e.g. "Lift FX controls")
--   market    — observable outcome (e.g. "ARS/USD breaks 1500")
--   evidence  — supporting source/citation (optional inline)
CREATE TABLE IF NOT EXISTS impact_chain_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id        INTEGER NOT NULL,
    step_idx        INTEGER NOT NULL,           -- 0-based order in the chain
    kind            TEXT NOT NULL,              -- concern|actor|policy|market|evidence
    text            TEXT NOT NULL,              -- short human label
    detail          TEXT,                       -- optional 1-2 sentence elaboration
    ref_url         TEXT,                       -- optional link (poll, market, news)
    ref_provider    TEXT,                       -- 'polymarket'|'kalshi'|'pew'|...
    ref_id          TEXT,                       -- provider-specific id (market id, etc.)
    confidence      INTEGER,                    -- 1..5 author-stated confidence
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES impact_chains(id) ON DELETE CASCADE,
    UNIQUE (chain_id, step_idx)
);

CREATE INDEX IF NOT EXISTS idx_chain_steps ON impact_chain_steps (chain_id, step_idx);

-- ── impact_chain_reviews ──────────────────────────────────────────
-- Append-only review trail. A chain can have multiple reviewers; the
-- "decided_by" on impact_chains records the latest decisive vote, but
-- the full history lives here.
CREATE TABLE IF NOT EXISTS impact_chain_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id        INTEGER NOT NULL,
    reviewer_id     INTEGER NOT NULL,
    reviewer_email  TEXT NOT NULL,
    decision        TEXT NOT NULL,              -- approve|reject|request_changes|comment
    notes           TEXT,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (chain_id) REFERENCES impact_chains(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chain_reviews ON impact_chain_reviews (chain_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chain_reviews_by ON impact_chain_reviews (reviewer_id, created_at DESC);

-- ── impact_chain_votes ────────────────────────────────────────────
-- Per-user up/down on a chain (mirrors thought_votes).
CREATE TABLE IF NOT EXISTS impact_chain_votes (
    chain_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    vote            INTEGER NOT NULL,           -- +1 / -1
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (chain_id, user_id),
    FOREIGN KEY (chain_id) REFERENCES impact_chains(id) ON DELETE CASCADE
);
