"""The credibility engine — the product core.

Beta posterior with priors alpha=2.0, beta=2.0; credibility = alpha/(alpha+beta)
(new source => 0.5). Resolving a question scores every source's LATEST
prediction on it: hit iff (p >= 0.5) == (outcome == yes); hit => alpha += 1,
miss => beta += 1, with a credibility_events row per source. Consensus is the
credibility-weighted blend of latest predictions. Brier is display-only.
"""

from __future__ import annotations

import sqlite3

from .db import utcnow


class QuestionNotFound(Exception):
    pass


class AlreadyResolved(Exception):
    pass


class InvalidOutcome(Exception):
    pass


def credibility(alpha: float, beta: float) -> float:
    return alpha / (alpha + beta)


def latest_predictions(conn: sqlite3.Connection, question_id: str) -> list[sqlite3.Row]:
    """One row per source: its latest prediction on the question
    (by stated_at, ties broken by insertion id)."""
    return conn.execute(
        """
        SELECT * FROM (
            SELECT p.*, ROW_NUMBER() OVER (
                PARTITION BY source_id ORDER BY stated_at DESC, id DESC
            ) AS rn
            FROM predictions p WHERE question_id = ?
        ) WHERE rn = 1
        """,
        (question_id,),
    ).fetchall()


def consensus(conn: sqlite3.Connection, question_id: str) -> float | None:
    """combined_p = sum(p_i * cred_i) / sum(cred_i) over latest predictions.
    None when no source has predicted."""
    num = den = 0.0
    for pred in latest_predictions(conn, question_id):
        src = conn.execute(
            "SELECT alpha, beta FROM sources WHERE id = ?", (pred["source_id"],)
        ).fetchone()
        if src is None:
            continue
        c = credibility(src["alpha"], src["beta"])
        num += pred["p"] * c
        den += c
    return num / den if den > 0 else None


def brier(conn: sqlite3.Connection, source_id: str) -> float | None:
    """Display-only: mean of (p - outcome)^2 over resolved questions,
    using the source's latest prediction per question. None if nothing resolved."""
    rows = conn.execute(
        """
        SELECT lp.p, q.resolved_outcome FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY question_id ORDER BY stated_at DESC, id DESC
            ) AS rn
            FROM predictions WHERE source_id = ?
        ) lp
        JOIN questions q ON q.id = lp.question_id
        WHERE lp.rn = 1 AND q.status = 'resolved' AND q.resolved_outcome IS NOT NULL
        """,
        (source_id,),
    ).fetchall()
    if not rows:
        return None
    return sum((r["p"] - r["resolved_outcome"]) ** 2 for r in rows) / len(rows)


def resolve_question(
    conn: sqlite3.Connection,
    question_id: str,
    outcome: str,
    resolved_at: str | None = None,
) -> list[dict]:
    """The resolution loop. Returns moves: [{source_id, old_cred, new_cred, hit}].

    'void' sets status='void' with no score changes (empty moves). Re-resolving
    any non-live question raises AlreadyResolved (the API maps it to 409).
    """
    if outcome not in ("yes", "no", "void"):
        raise InvalidOutcome(f"outcome must be one of yes|no|void, got {outcome!r}")
    q = conn.execute(
        "SELECT * FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if q is None:
        raise QuestionNotFound(question_id)
    if q["status"] != "live":
        raise AlreadyResolved(question_id)
    at = resolved_at or utcnow()

    if outcome == "void":
        conn.execute(
            "UPDATE questions SET status = 'void', resolved_at = ? WHERE id = ?",
            (at, question_id),
        )
        conn.commit()
        return []

    outcome_yes = outcome == "yes"
    moves: list[dict] = []
    for pred in latest_predictions(conn, question_id):
        src = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (pred["source_id"],)
        ).fetchone()
        if src is None:  # orphan prediction; ingest always creates the source
            continue
        old_a, old_b = src["alpha"], src["beta"]
        hit = (pred["p"] >= 0.5) == outcome_yes
        new_a = old_a + 1.0 if hit else old_a
        new_b = old_b if hit else old_b + 1.0
        conn.execute(
            "UPDATE sources SET alpha = ?, beta = ? WHERE id = ?",
            (new_a, new_b, src["id"]),
        )
        conn.execute(
            "INSERT INTO credibility_events"
            "(source_id, question_id, old_alpha, old_beta, new_alpha, new_beta, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (src["id"], question_id, old_a, old_b, new_a, new_b, at),
        )
        moves.append({
            "source_id": src["id"],
            "old_cred": round(credibility(old_a, old_b), 4),
            "new_cred": round(credibility(new_a, new_b), 4),
            "hit": hit,
        })
    conn.execute(
        "UPDATE questions SET status = 'resolved', resolved_outcome = ?,"
        " resolved_at = ? WHERE id = ?",
        (1 if outcome_yes else 0, at, question_id),
    )
    conn.commit()
    return moves
