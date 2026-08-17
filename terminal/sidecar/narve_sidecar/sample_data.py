"""Sample dataset — derived from gateway/lab_pages/markets.py::_fallback_rows().

12 live midterm questions, 5 sources, one latest prediction per row at narve_p,
one polymarket snapshot per row at market_price. Plus 2 PRE-RESOLVED 2025
governor questions (a hit for race_model, a miss for poll_aggregator) run
through the real resolution loop so the SOURCES screen shows credibility
movement out of the box. Everything is_sample=1; source ids prefixed
'sample:'; questions keep their slug ids. Loading is idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import credibility
from .db import utcnow

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

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
    """Idempotent: a second load is a no-op that returns the current counts."""
    if conn.execute("SELECT 1 FROM sources WHERE is_sample = 1 LIMIT 1").fetchone():
        return _counts(conn)
    now = datetime.now(timezone.utc)
    created = utcnow()
    for sid, (name, alpha, beta) in SOURCES.items():
        conn.execute(
            "INSERT INTO sources(id, name, alpha, beta, is_sample, created_at)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (f"sample:{sid}", name, alpha, beta, created),
        )
    for i, (title, qid, sid, _cred, narve_p, mkt, age_min) in enumerate(EVENTS):
        at = (now - timedelta(minutes=age_min)).strftime(TS_FMT)
        conn.execute(
            "INSERT INTO questions(id, title, status, is_sample, created_at)"
            " VALUES (?, ?, 'live', 1, ?)", (qid, title, created))
        conn.execute(
            "INSERT INTO predictions(source_id, question_id, p, stated_at, note)"
            " VALUES (?, ?, ?, ?, NULL)", (f"sample:{sid}", qid, narve_p, at))
        conn.execute(
            "INSERT INTO market_snapshots"
            "(venue, market_id, question_id, yes_price, liquidity, captured_at)"
            " VALUES ('polymarket', ?, ?, ?, ?, ?)",
            (qid, qid, mkt, 50000.0 * (12 - i), at))
    for title, qid, sid, p, outcome, stated_at, resolved_at in RESOLVED:
        conn.execute(
            "INSERT INTO questions(id, title, status, is_sample, created_at)"
            " VALUES (?, ?, 'live', 1, ?)", (qid, title, created))
        conn.execute(
            "INSERT INTO predictions(source_id, question_id, p, stated_at, note)"
            " VALUES (?, ?, ?, ?, NULL)", (f"sample:{sid}", qid, p, stated_at))
        credibility.resolve_question(conn, qid, outcome, resolved_at)
    conn.commit()
    return _counts(conn)
