"""Pins the exact credibility math from CONTRACT.md."""

import pytest

from narve_sidecar import credibility as cr
from tests.conftest import add_prediction, add_question, add_source


def test_new_source_credibility_is_half():
    assert cr.credibility(2.0, 2.0) == 0.5


def test_hand_math_hit_two_two_to_three_fifths(conn):
    add_source(conn, "s1")
    add_question(conn, "q1")
    add_prediction(conn, "s1", "q1", 0.7)
    moves = cr.resolve_question(conn, "q1", "yes")
    assert moves == [{"source_id": "s1", "old_cred": 0.5, "new_cred": 0.6,
                      "hit": True}]
    row = conn.execute("SELECT alpha, beta FROM sources WHERE id='s1'").fetchone()
    assert (row["alpha"], row["beta"]) == (3.0, 2.0)
    assert 3.0 / 5.0 == 0.6  # the contract's hand math


def test_miss_increments_beta(conn):
    add_source(conn, "s1")
    add_question(conn, "q1")
    add_prediction(conn, "s1", "q1", 0.7)
    moves = cr.resolve_question(conn, "q1", "no")
    assert moves[0]["hit"] is False
    row = conn.execute("SELECT alpha, beta FROM sources WHERE id='s1'").fetchone()
    assert (row["alpha"], row["beta"]) == (2.0, 3.0)
    assert moves[0]["new_cred"] == 0.4


def test_latest_prediction_wins(conn):
    add_source(conn, "s1")
    add_question(conn, "q1")
    add_prediction(conn, "s1", "q1", 0.2, stated_at="2026-08-01T00:00:00Z")
    add_prediction(conn, "s1", "q1", 0.8, stated_at="2026-08-02T00:00:00Z")
    moves = cr.resolve_question(conn, "q1", "yes")
    assert moves[0]["hit"] is True  # latest p=0.8 scored, not the 0.2


def test_resolution_loop_scores_every_source(conn):
    add_source(conn, "a")
    add_source(conn, "b")
    add_question(conn, "q1")
    add_prediction(conn, "a", "q1", 0.9)
    add_prediction(conn, "b", "q1", 0.1)
    moves = cr.resolve_question(conn, "q1", "yes")
    by_id = {m["source_id"]: m for m in moves}
    assert by_id["a"]["hit"] is True and by_id["b"]["hit"] is False
    events = conn.execute(
        "SELECT source_id, old_alpha, new_alpha, new_beta FROM"
        " credibility_events ORDER BY source_id").fetchall()
    assert len(events) == 2
    assert events[0]["source_id"] == "a" and events[0]["new_alpha"] == 3.0
    assert events[1]["source_id"] == "b" and events[1]["new_beta"] == 3.0
    q = conn.execute("SELECT * FROM questions WHERE id='q1'").fetchone()
    assert q["status"] == "resolved" and q["resolved_outcome"] == 1


def test_void_changes_nothing(conn):
    add_source(conn, "s1")
    add_question(conn, "q1")
    add_prediction(conn, "s1", "q1", 0.7)
    moves = cr.resolve_question(conn, "q1", "void")
    assert moves == []
    row = conn.execute("SELECT alpha, beta FROM sources WHERE id='s1'").fetchone()
    assert (row["alpha"], row["beta"]) == (2.0, 2.0)
    q = conn.execute("SELECT status FROM questions WHERE id='q1'").fetchone()
    assert q["status"] == "void"
    assert conn.execute("SELECT COUNT(*) FROM credibility_events").fetchone()[0] == 0


def test_double_resolve_raises(conn):
    add_question(conn, "q1")
    cr.resolve_question(conn, "q1", "yes")
    with pytest.raises(cr.AlreadyResolved):
        cr.resolve_question(conn, "q1", "no")


def test_resolve_void_then_resolve_raises(conn):
    add_question(conn, "q1")
    cr.resolve_question(conn, "q1", "void")
    with pytest.raises(cr.AlreadyResolved):
        cr.resolve_question(conn, "q1", "yes")


def test_invalid_outcome_raises(conn):
    add_question(conn, "q1")
    with pytest.raises(cr.InvalidOutcome):
        cr.resolve_question(conn, "q1", "maybe")


def test_unknown_question_raises(conn):
    with pytest.raises(cr.QuestionNotFound):
        cr.resolve_question(conn, "nope", "yes")


def test_consensus_blend(conn):
    # cred a = 3/5 = 0.6, cred b = 2/4 = 0.5
    add_source(conn, "a", alpha=3.0, beta=2.0)
    add_source(conn, "b", alpha=2.0, beta=2.0)
    add_question(conn, "q1")
    add_prediction(conn, "a", "q1", 0.8)
    add_prediction(conn, "b", "q1", 0.4)
    expected = (0.8 * 0.6 + 0.4 * 0.5) / (0.6 + 0.5)
    assert abs(cr.consensus(conn, "q1") - expected) < 1e-12


def test_consensus_uses_latest_prediction(conn):
    add_source(conn, "a")
    add_question(conn, "q1")
    add_prediction(conn, "a", "q1", 0.2, stated_at="2026-08-01T00:00:00Z")
    add_prediction(conn, "a", "q1", 0.9, stated_at="2026-08-02T00:00:00Z")
    assert abs(cr.consensus(conn, "q1") - 0.9) < 1e-12


def test_consensus_none_without_predictions(conn):
    add_question(conn, "q1")
    assert cr.consensus(conn, "q1") is None


def test_brier(conn):
    add_source(conn, "a")
    add_question(conn, "q1")
    add_question(conn, "q2")
    add_question(conn, "q3")  # stays live: excluded
    add_prediction(conn, "a", "q1", 0.8)
    add_prediction(conn, "a", "q2", 0.3)
    add_prediction(conn, "a", "q3", 0.5)
    cr.resolve_question(conn, "q1", "yes")   # (0.8-1)^2 = 0.04
    cr.resolve_question(conn, "q2", "yes")   # (0.3-1)^2 = 0.49
    assert abs(cr.brier(conn, "a") - (0.04 + 0.49) / 2) < 1e-12


def test_brier_none_when_nothing_resolved(conn):
    add_source(conn, "a")
    add_question(conn, "q1")
    add_prediction(conn, "a", "q1", 0.5)
    assert cr.brier(conn, "a") is None
