"""Sample dataset — derived from gateway/lab_pages/markets.py::_fallback_rows().

12 live midterm questions, 5 sources, one latest prediction per row at narve_p,
one polymarket snapshot per row at market_price. Plus 2 PRE-RESOLVED 2025
governor questions (a hit for race_model, a miss for poll_aggregator) run
through the real resolution loop so the SOURCES screen shows credibility
movement out of the box. v2 adds one HUMAN source (sample:capitol_staffer,
kind='user', fresh 2/2 prior) with 3 text messages around the House question —
the one carrying question+stance also lands a prediction, entering the texter
into the credibility loop. v3 adds ACCOUNT CONTEXT (bias/region/affiliation/
topics via targeted UPDATEs, so upgraded DBs pick it up too) and one DJT
sample question (trump-2026-rally-tour) with two predictions and a snapshot,
so the source_context + DJT skew logic is demoable out of the box.
Everything is_sample=1; source ids prefixed 'sample:'; questions keep their
slug ids. Loading is idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import credibility
from .db import utcnow
from .ingest import text_hash

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Fixed anchor for every sample timestamp (age_minutes counts back from here).
# Deliberately NOT wall-clock: the natural keys (stated_at / captured_at /
# sent_at) must be identical on every load, or INSERT OR IGNORE can't dedupe
# and each /sample/load would re-insert the whole set with fresh keys.
ANCHOR = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

# (title, question_id, source_id, listed_cred, narve_p, market_price, age_minutes)
EVENTS = [
    ("Republicans win the U.S. House majority (2026)", "midterm-house-gop-2026", "race_model", 0.81, 0.62, 0.55, 60),
    ("Democrats hold the U.S. Senate majority (2026)", "midterm-senate-dem-2026", "poll_aggregator", 0.76, 0.48, 0.41, 120),
    ("Texas U.S. Senate — Republican wins (2026)", "midterm-tx-sen-gop-2026", "race_model", 0.82, 0.80, 0.74, 60),
    ("Ohio U.S. Senate — Republican wins (2026)", "midterm-oh-sen-gop-2026", "race_model", 0.81, 0.71, 0.64, 180),
    ("Michigan U.S. Senate — Democrat wins (2026)", "midterm-mi-sen-dem-2026", "state_polls", 0.79, 0.63, 0.57, 120),
    ("Arizona U.S. Senate — Democrat wins (2026)", "midterm-az-sen-dem-2026", "state_polls", 0.69, 0.54, 0.60, 300),
    ("Georgia U.S. Senate — Republican wins (2026)", "midterm-ga-sen-gop-2026", "state_polls", 0.68, 0.46, 0.52, 360),
    ("Nevada U.S. Senate — Republican wins (2026)", "midterm-nv-sen-gop-2026", "state_polls", 0.66, 0.49, 0.44, 420),
    ("Balance of power — GOP wins House & Senate (2026)", "midterm-trifecta-gop-2026", "macro_model", 0.77, 0.34, 0.39, 300),
    ("Pennsylvania Governor — Democrat wins (2026)", "midterm-pa-gov-dem-2026", "state_polls", 0.70, 0.58, 0.62, 240),
    ("Wisconsin U.S. Senate — Democrat wins (2026)", "midterm-wi-sen-dem-2026", "state_polls", 0.71, 0.52, 0.55, 180),
    ("National House popular vote — Democrats win (2026)", "midterm-house-popvote-dem-2026", "generic_ballot", 0.74, 0.51, 0.49, 30),
]

# Seed integer alpha/beta chosen so that AFTER the two pre-resolved questions
# below are scored (race_model +1 alpha, poll_aggregator +1 beta) the final
# credibility lands within +/-0.005 of each source's listed cred (mean where a
# source is listed with several values), with alpha+beta in [8, 30]:
#   race_model      12/3 -> hit  -> 13/16 = 0.8125  (listed mean 0.8133)
#   poll_aggregator 19/5 -> miss -> 19/25 = 0.76    (listed 0.76)
#   state_polls     12/5         -> 12/17 = 0.7059  (listed mean 0.705)
#   macro_model     10/3         -> 10/13 = 0.7692  (listed 0.77)
#   generic_ballot  20/7         -> 20/27 = 0.7407  (listed 0.74)
SOURCES = {
    "race_model": ("Race Model", 12.0, 3.0),
    "poll_aggregator": ("Poll Aggregator", 19.0, 5.0),
    "state_polls": ("State Polls", 12.0, 5.0),
    "macro_model": ("Macro Model", 10.0, 3.0),
    "generic_ballot": ("Generic Ballot", 20.0, 7.0),
}

# One human texter, kind='user', fresh Beta(2,2) prior — scored only when a
# question it predicted on resolves.
USER_SOURCES = {
    "capitol_staffer": ("Capitol Staffer", 2.0, 2.0),
}

# (age_minutes, question_id | None, stance_p | None, text). The second message
# carries BOTH question_id and stance, so it also lands a predictions row —
# mirroring the messages-ingest rule. The first has a stance but no question
# link; the third is context-only (no stance).
MESSAGES = [
    (90, None, 0.70,
     "Leadership whip count has the House majority holding — floor mood is confident."),
    (55, "midterm-house-gop-2026", 0.66,
     "Latest internal count on the House: still see it around 2-in-3 for the GOP."),
    (25, "midterm-house-gop-2026", None,
     "FWIW two members told me the NRCC is moving money into toss-up seats this week."),
]

# Account context, applied as targeted UPDATEs on every load (idempotent —
# same values every time — and it upgrades v1/v2 DBs whose sample sources
# already exist so INSERT OR IGNORE alone would leave them context-less).
MODEL_TOPICS = "midterms polls senate house"
CONTEXT = {  # sid -> (bias, region, affiliation, topics)
    "race_model": ("center", "US", "model", MODEL_TOPICS),
    "poll_aggregator": ("center", "US", "model", MODEL_TOPICS),
    "state_polls": ("center", "US", "model", MODEL_TOPICS),
    "macro_model": ("center", "US", "model", MODEL_TOPICS),
    "generic_ballot": ("center", "US", "model", MODEL_TOPICS),
    "capitol_staffer": ("lean-right", "US-DC", "staffer", "midterms house djt"),
}

# The DJT sample question: live, two predictions, one snapshot — enough for
# the QUESTION drill to show source_context (djt_related, bias spread) and a
# non-empty WORTH HEARING FROM list (the four non-predicting models match on
# the "midterms" topic token).
DJT_QUESTION = ("trump-2026-rally-tour",
                "Trump holds 20+ rallies before the 2026 midterms")
DJT_PREDICTIONS = [  # (source_id, p, age_minutes)
    ("capitol_staffer", 0.7, 45),
    ("race_model", 0.55, 50),
]
DJT_SNAPSHOT = (0.6, 30000.0, 40)  # (yes_price, liquidity, age_minutes)

# (title, question_id, source_id, p, outcome, stated_at, resolved_at)
RESOLVED = [
    ("Virginia Governor — Democrat wins (2025)", "va-gov-dem-2025",
     "race_model", 0.72, "yes", "2025-11-01T12:00:00Z", "2025-11-04T04:00:00Z"),
    ("New Jersey Governor — Republican wins (2025)", "nj-gov-gop-2025",
     "poll_aggregator", 0.58, "no", "2025-11-01T12:00:00Z", "2025-11-04T04:00:00Z"),
]


def _counts(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "sources": one("SELECT COUNT(*) FROM sources WHERE is_sample = 1"),
        "questions": one("SELECT COUNT(*) FROM questions WHERE is_sample = 1"),
        "predictions": one(
            "SELECT COUNT(*) FROM predictions WHERE source_id LIKE 'sample:%'"),
        "snapshots": one(
            "SELECT COUNT(*) FROM market_snapshots WHERE question_id IN"
            " (SELECT id FROM questions WHERE is_sample = 1)"),
    }


def load_sample(conn: sqlite3.Connection) -> dict:
    """Idempotent at ROW level (INSERT OR IGNORE on every natural key), so a
    re-load is a no-op AND an upgraded sample set tops up an existing DB with
    only its new rows — a table-level "already loaded" guard would strand old
    DBs on the old sample forever."""
    now = ANCHOR
    created = utcnow()
    for sid, (name, alpha, beta) in SOURCES.items():
        conn.execute(
            "INSERT OR IGNORE INTO sources(id, name, alpha, beta, kind, is_sample,"
            " created_at) VALUES (?, ?, ?, ?, 'model', 1, ?)",
            (f"sample:{sid}", name, alpha, beta, created),
        )
    for sid, (name, alpha, beta) in USER_SOURCES.items():
        conn.execute(
            "INSERT OR IGNORE INTO sources(id, name, alpha, beta, kind, is_sample,"
            " created_at) VALUES (?, ?, ?, ?, 'user', 1, ?)",
            (f"sample:{sid}", name, alpha, beta, created),
        )
    for sid, (bias, region, affiliation, topics) in CONTEXT.items():
        conn.execute(
            "UPDATE sources SET bias = ?, region = ?, affiliation = ?,"
            " topics = ? WHERE id = ?",
            (bias, region, affiliation, topics, f"sample:{sid}"),
        )
    for i, (title, qid, sid, _cred, narve_p, mkt, age_min) in enumerate(EVENTS):
        at = (now - timedelta(minutes=age_min)).strftime(TS_FMT)
        conn.execute(
            "INSERT OR IGNORE INTO questions(id, title, status, is_sample, created_at)"
            " VALUES (?, ?, 'live', 1, ?)", (qid, title, created))
        conn.execute(
            "INSERT OR IGNORE INTO predictions(source_id, question_id, p, stated_at, note)"
            " VALUES (?, ?, ?, ?, NULL)", (f"sample:{sid}", qid, narve_p, at))
        conn.execute(
            "INSERT OR IGNORE INTO market_snapshots"
            "(venue, market_id, question_id, yes_price, liquidity, captured_at)"
            " VALUES ('polymarket', ?, ?, ?, ?, ?)",
            (qid, qid, mkt, 50000.0 * (12 - i), at))
    for age_min, qid, stance, text in MESSAGES:
        at = (now - timedelta(minutes=age_min)).strftime(TS_FMT)
        conn.execute(
            "INSERT OR IGNORE INTO messages(source_id, question_id, text, sent_at,"
            " stance_p, text_hash, is_sample) VALUES (?, ?, ?, ?, ?, ?, 1)",
            ("sample:capitol_staffer", qid, text, at, stance, text_hash(text)),
        )
        if qid and stance is not None:  # question+stance -> credibility loop
            conn.execute(
                "INSERT OR IGNORE INTO predictions(source_id, question_id, p, stated_at,"
                " note) VALUES (?, ?, ?, ?, ?)",
                ("sample:capitol_staffer", qid, stance, at, text[:100]),
            )
    djt_qid, djt_title = DJT_QUESTION
    conn.execute(
        "INSERT OR IGNORE INTO questions(id, title, status, is_sample, created_at)"
        " VALUES (?, ?, 'live', 1, ?)", (djt_qid, djt_title, created))
    for sid, p, age_min in DJT_PREDICTIONS:
        at = (now - timedelta(minutes=age_min)).strftime(TS_FMT)
        conn.execute(
            "INSERT OR IGNORE INTO predictions(source_id, question_id, p, stated_at,"
            " note) VALUES (?, ?, ?, ?, NULL)", (f"sample:{sid}", djt_qid, p, at))
    price, liq, age_min = DJT_SNAPSHOT
    at = (now - timedelta(minutes=age_min)).strftime(TS_FMT)
    conn.execute(
        "INSERT OR IGNORE INTO market_snapshots"
        "(venue, market_id, question_id, yes_price, liquidity, captured_at)"
        " VALUES ('polymarket', ?, ?, ?, ?, ?)", (djt_qid, djt_qid, price, liq, at))
    for title, qid, sid, p, outcome, stated_at, resolved_at in RESOLVED:
        conn.execute(
            "INSERT OR IGNORE INTO questions(id, title, status, is_sample, created_at)"
            " VALUES (?, ?, 'live', 1, ?)", (qid, title, created))
        conn.execute(
            "INSERT OR IGNORE INTO predictions(source_id, question_id, p, stated_at, note)"
            " VALUES (?, ?, ?, ?, NULL)", (f"sample:{sid}", qid, p, stated_at))
        # Only score once: on re-load (or an upgraded DB) the question is
        # already resolved and re-scoring would double-count credibility.
        status = conn.execute(
            "SELECT status FROM questions WHERE id = ?", (qid,)).fetchone()
        if status is not None and status[0] == "live":
            credibility.resolve_question(conn, qid, outcome, resolved_at)
    conn.commit()
    return _counts(conn)
