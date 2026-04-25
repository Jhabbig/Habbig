"""Tests for user-authored predictions (migration 031).

Covers:
  - input validation in create_user_prediction
  - one-active-prediction-per-(user,market) invariant
  - 24-hour edit window + direction-locked editability
  - delete-only-while-unresolved
  - resolution: Brier + timing scoring, correctness mapping
  - per-user stats recompute (accuracy, streaks, category breakdown)
  - mirror into user_accuracy for the leaderboard
  - public/private visibility filtering
"""

from __future__ import annotations

import os
import sys
import time
import unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


# Feature gate: skip if the full user-predictions surface isn't on this
# branch yet. The actual function name is `resolve_user_predictions_for_market`
# (an earlier marker checked for `resolve_user_predictions` — wrong name —
# which made the whole module skip on every run regardless of state).
_USER_PREDICTIONS_FULL_API = all(
    hasattr(db, fn) for fn in (
        "create_user_prediction",
        "update_user_prediction",
        "delete_user_prediction",
        "resolve_user_predictions_for_market",
    )
)

pytestmark = pytest.mark.skipif(
    not _USER_PREDICTIONS_FULL_API,
    reason=(
        "user_predictions full CRUD+resolve surface not present on this "
        "branch — tests re-enable once delete_user_prediction and "
        "resolve_user_predictions_for_market land in db."
    ),
)


def _mk_user(name: str, *, leaderboard: bool = False, leaderboard_handle: str = "") -> int:
    """Insert a user row and return its id."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, "
            "                   created_at, leaderboard_participation, leaderboard_handle) "
            "VALUES (?, ?, 'h', 's', ?, ?, ?)",
            (
                name, f"{name}@test.local", int(time.time()),
                1 if leaderboard else 0,
                leaderboard_handle or None,
            ),
        )
        return c.execute(
            "SELECT id FROM users WHERE username = ?", (name,)
        ).fetchone()["id"]


def _backdate_prediction(pid: int, age_seconds: int) -> None:
    """Move a prediction's created_at into the past for edit-window tests."""
    with db.conn() as c:
        c.execute(
            "UPDATE user_predictions SET created_at = ? WHERE id = ?",
            (int(time.time()) - age_seconds, pid),
        )


class TestCreateUserPrediction(unittest.TestCase):
    def test_validates_outcome(self):
        uid = _mk_user("upred_outcome")
        with self.assertRaises(ValueError):
            db.create_user_prediction(uid, "poly:m1", "MAYBE", 0.5)

    def test_validates_probability_range(self):
        uid = _mk_user("upred_prob")
        with self.assertRaises(ValueError):
            db.create_user_prediction(uid, "poly:m2", "YES", 1.5)
        with self.assertRaises(ValueError):
            db.create_user_prediction(uid, "poly:m2b", "YES", -0.1)

    def test_requires_market_slug(self):
        uid = _mk_user("upred_slug")
        with self.assertRaises(ValueError):
            db.create_user_prediction(uid, "  ", "YES", 0.7)

    def test_caps_reasoning_length(self):
        uid = _mk_user("upred_reasoning")
        with self.assertRaises(ValueError):
            db.create_user_prediction(
                uid, "poly:m3", "YES", 0.7, reasoning="x" * 2001
            )

    def test_one_active_per_market(self):
        """A second active prediction on the same market must be rejected."""
        uid = _mk_user("upred_dup")
        db.create_user_prediction(uid, "poly:dup-mkt", "YES", 0.7)
        with self.assertRaises(ValueError):
            db.create_user_prediction(uid, "poly:dup-mkt", "NO", 0.4)

    def test_re_predict_after_resolution(self):
        """Once a prediction resolves, the user can predict again."""
        uid = _mk_user("upred_repredict")
        db.create_user_prediction(uid, "poly:re-mkt", "YES", 0.6)
        db.resolve_user_predictions_for_market("poly:re-mkt", outcome_yes=True)
        # Second prediction should succeed now.
        new_pid = db.create_user_prediction(uid, "poly:re-mkt", "NO", 0.5)
        self.assertGreater(new_pid, 0)

    def test_increments_total_predictions(self):
        uid = _mk_user("upred_total_inc")
        db.create_user_prediction(uid, "poly:t1", "YES", 0.6)
        db.create_user_prediction(uid, "poly:t2", "NO", 0.4)
        stats = db.get_user_prediction_stats(uid)
        self.assertEqual(stats["total_predictions"], 2)

    def test_records_market_price_and_edge(self):
        """When a snapshot exists, market_price + edge_at_prediction are filled."""
        uid = _mk_user("upred_edge")
        with db.conn() as c:
            c.execute(
                "INSERT INTO market_snapshots (market_slug, market_question, "
                "category, yes_price, no_price, snapshotted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("poly:edge-mkt", "Will X happen?", "politics", 0.40, 0.60,
                 int(time.time())),
            )
        pid = db.create_user_prediction(uid, "poly:edge-mkt", "YES", 0.70)
        row = db.get_user_prediction(pid)
        self.assertAlmostEqual(row["market_price_at_prediction"], 0.40, places=4)
        # YES edge = user_yes - market_yes = 0.70 - 0.40 = +0.30
        self.assertAlmostEqual(row["edge_at_prediction"], 0.30, places=4)
        # Snapshot supplied the question/category if the caller didn't.
        self.assertEqual(row["market_question"], "Will X happen?")
        self.assertEqual(row["category"], "politics")


class TestUpdateUserPrediction(unittest.TestCase):
    def test_owner_can_edit_within_window(self):
        uid = _mk_user("upred_edit_ok")
        pid = db.create_user_prediction(uid, "poly:edit-mkt", "YES", 0.6, reasoning="old")
        ok, err = db.update_user_prediction(
            pid, uid, predicted_probability=0.85, reasoning="new",
        )
        self.assertTrue(ok)
        self.assertIsNone(err)
        row = db.get_user_prediction(pid)
        self.assertEqual(row["predicted_probability"], 0.85)
        self.assertEqual(row["reasoning"], "new")

    def test_other_user_blocked(self):
        owner = _mk_user("upred_owner")
        other = _mk_user("upred_other")
        pid = db.create_user_prediction(owner, "poly:fb-mkt", "YES", 0.6)
        ok, err = db.update_user_prediction(pid, other, predicted_probability=0.9)
        self.assertFalse(ok)
        self.assertEqual(err, "forbidden")

    def test_edit_blocked_after_24h(self):
        uid = _mk_user("upred_locked")
        pid = db.create_user_prediction(uid, "poly:lock-mkt", "YES", 0.6)
        _backdate_prediction(pid, 25 * 3600)
        ok, err = db.update_user_prediction(pid, uid, predicted_probability=0.9)
        self.assertFalse(ok)
        self.assertEqual(err, "locked")

    def test_visibility_toggle_after_window(self):
        """is_public flip is allowed even after the 24h window expires."""
        uid = _mk_user("upred_pub_toggle")
        pid = db.create_user_prediction(uid, "poly:vis-mkt", "YES", 0.6)
        _backdate_prediction(pid, 25 * 3600)
        ok, err = db.update_user_prediction(pid, uid, is_public=True)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_edit_blocked_after_resolution(self):
        uid = _mk_user("upred_resolved_edit")
        pid = db.create_user_prediction(uid, "poly:re-edit", "YES", 0.6)
        db.resolve_user_predictions_for_market("poly:re-edit", outcome_yes=True)
        ok, err = db.update_user_prediction(pid, uid, predicted_probability=0.9)
        self.assertFalse(ok)
        self.assertEqual(err, "resolved")


class TestDeleteUserPrediction(unittest.TestCase):
    def test_owner_can_delete_unresolved(self):
        uid = _mk_user("upred_del_ok")
        pid = db.create_user_prediction(uid, "poly:del-mkt", "YES", 0.6)
        ok, err = db.delete_user_prediction(pid, uid)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNone(db.get_user_prediction(pid))

    def test_cannot_delete_resolved(self):
        uid = _mk_user("upred_del_resolved")
        pid = db.create_user_prediction(uid, "poly:del2", "YES", 0.6)
        db.resolve_user_predictions_for_market("poly:del2", outcome_yes=True)
        ok, err = db.delete_user_prediction(pid, uid)
        self.assertFalse(ok)
        self.assertEqual(err, "resolved")

    def test_other_user_cannot_delete(self):
        owner = _mk_user("upred_del_owner")
        other = _mk_user("upred_del_other")
        pid = db.create_user_prediction(owner, "poly:del3", "YES", 0.6)
        ok, err = db.delete_user_prediction(pid, other)
        self.assertFalse(ok)
        self.assertEqual(err, "forbidden")


class TestResolveUserPredictions(unittest.TestCase):
    def test_marks_correct_and_incorrect(self):
        uid_a = _mk_user("upred_res_a")
        uid_b = _mk_user("upred_res_b")
        pid_yes = db.create_user_prediction(uid_a, "poly:res-mkt", "YES", 0.8)
        pid_no = db.create_user_prediction(uid_b, "poly:res-mkt", "NO", 0.7)

        count = db.resolve_user_predictions_for_market(
            "poly:res-mkt", outcome_yes=True, final_market_price=0.95,
        )
        self.assertEqual(count, 2)

        row_yes = db.get_user_prediction(pid_yes)
        self.assertEqual(row_yes["resolved"], 1)
        self.assertEqual(row_yes["resolved_correct"], 1)
        self.assertEqual(row_yes["final_market_price"], 0.95)

        row_no = db.get_user_prediction(pid_no)
        self.assertEqual(row_no["resolved"], 1)
        self.assertEqual(row_no["resolved_correct"], 0)

    def test_brier_score_yes_correct(self):
        """YES (0.7) prediction on YES outcome → Brier = (0.7 - 1)^2 = 0.09."""
        uid = _mk_user("upred_brier_yes")
        pid = db.create_user_prediction(uid, "poly:brier-yes", "YES", 0.7)
        db.resolve_user_predictions_for_market("poly:brier-yes", outcome_yes=True)
        row = db.get_user_prediction(pid)
        self.assertAlmostEqual(row["brier_score"], 0.09, places=4)

    def test_brier_score_no_correct(self):
        """NO (0.7) prediction on NO outcome → p_yes=0.3, actual=0 → 0.09."""
        uid = _mk_user("upred_brier_no")
        pid = db.create_user_prediction(uid, "poly:brier-no", "NO", 0.7)
        db.resolve_user_predictions_for_market("poly:brier-no", outcome_yes=False)
        row = db.get_user_prediction(pid)
        self.assertAlmostEqual(row["brier_score"], 0.09, places=4)

    def test_timing_score_uses_market_first_seen(self):
        """Predict early in market lifetime → timing close to 1.0."""
        uid = _mk_user("upred_timing")
        # Snapshot landed 100 days ago.
        first_seen = int(time.time()) - 100 * 86400
        with db.conn() as c:
            c.execute(
                "INSERT INTO market_snapshots (market_slug, market_question, "
                "category, yes_price, no_price, snapshotted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("poly:timing-mkt", "Q?", "other", 0.5, 0.5, first_seen),
            )
        pid = db.create_user_prediction(uid, "poly:timing-mkt", "YES", 0.6)
        # Backdate prediction to ~2 days after first snapshot — very early.
        with db.conn() as c:
            c.execute(
                "UPDATE user_predictions SET created_at = ? WHERE id = ?",
                (first_seen + 2 * 86400, pid),
            )
        db.resolve_user_predictions_for_market("poly:timing-mkt", outcome_yes=True)
        row = db.get_user_prediction(pid)
        # Predicted at 2/100 = 2% elapsed → 98% remaining.
        self.assertGreater(row["timing_score"], 0.9)

    def test_timing_score_neutral_without_snapshot(self):
        """No snapshot → timing falls back to 0.5."""
        uid = _mk_user("upred_timing_nosnap")
        pid = db.create_user_prediction(uid, "poly:no-snap-mkt", "YES", 0.6)
        db.resolve_user_predictions_for_market("poly:no-snap-mkt", outcome_yes=True)
        row = db.get_user_prediction(pid)
        self.assertEqual(row["timing_score"], 0.5)


class TestStatsRecompute(unittest.TestCase):
    def test_accuracy_and_streaks(self):
        uid = _mk_user("upred_stats")
        # Create resolved predictions in chronological order:
        #   correct, correct, incorrect, correct, correct, correct
        outcomes = [True, True, False, True, True, True]
        slugs = [f"poly:stats-{i}" for i in range(len(outcomes))]
        now = int(time.time())
        for i, (slug, was_yes) in enumerate(zip(slugs, outcomes)):
            pid = db.create_user_prediction(uid, slug, "YES", 0.7)
            with db.conn() as c:
                c.execute(
                    "UPDATE user_predictions SET created_at = ? WHERE id = ?",
                    (now - (10 - i) * 3600, pid),  # ascending created_at
                )
            db.resolve_user_predictions_for_market(slug, outcome_yes=was_yes)
        stats = db.recompute_user_prediction_stats(uid)
        self.assertEqual(stats["resolved_predictions"], 6)
        self.assertEqual(stats["correct_predictions"], 5)
        self.assertAlmostEqual(stats["accuracy"], 5 / 6, places=4)
        # Streaks: walking c,c,x,c,c,c → best=3, current=3 (trailing run).
        self.assertEqual(stats["best_streak"], 3)
        self.assertEqual(stats["current_streak"], 3)

    def test_category_breakdown(self):
        import json as _json
        uid = _mk_user("upred_cats")
        for i in range(3):
            pid = db.create_user_prediction(
                uid, f"poly:pol-{i}", "YES", 0.7, category="politics",
            )
            db.resolve_user_predictions_for_market(f"poly:pol-{i}", outcome_yes=True)
        for i in range(2):
            pid = db.create_user_prediction(
                uid, f"poly:cry-{i}", "YES", 0.7, category="crypto",
            )
            # Only 1 of 2 crypto predictions correct.
            db.resolve_user_predictions_for_market(f"poly:cry-{i}", outcome_yes=(i == 0))
        stats = db.recompute_user_prediction_stats(uid)
        cats = _json.loads(stats["category_stats"])
        self.assertEqual(cats["politics"]["total"], 3)
        self.assertEqual(cats["politics"]["correct"], 3)
        self.assertEqual(cats["crypto"]["total"], 2)
        self.assertEqual(cats["crypto"]["correct"], 1)

    def test_mirrors_into_user_accuracy(self):
        """Stats recompute should populate `user_accuracy` so the
        leaderboard (migration 023) sees the user."""
        uid = _mk_user("upred_mirror", leaderboard=True, leaderboard_handle="mirroruser")
        for i in range(4):
            db.create_user_prediction(uid, f"poly:mir-{i}", "YES", 0.7)
            db.resolve_user_predictions_for_market(f"poly:mir-{i}", outcome_yes=(i % 2 == 0))
        db.recompute_user_prediction_stats(uid)
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM user_accuracy WHERE user_id = ?", (uid,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["total_predictions"], 4)
        self.assertEqual(row["correct_predictions"], 2)
        self.assertAlmostEqual(row["accuracy_score"], 0.5, places=4)


class TestVisibility(unittest.TestCase):
    def test_public_predictions_listed(self):
        owner = _mk_user("upred_pub_owner", leaderboard=True, leaderboard_handle="pubuser")
        db.create_user_prediction(owner, "poly:pub-1", "YES", 0.7, is_public=True)
        db.create_user_prediction(owner, "poly:pub-2", "NO", 0.6, is_public=False)
        public_rows = db.list_public_user_predictions(owner)
        self.assertEqual(len(public_rows), 1)
        self.assertEqual(public_rows[0]["market_slug"], "poly:pub-1")

    def test_other_user_isolation(self):
        a = _mk_user("upred_iso_a")
        b = _mk_user("upred_iso_b")
        db.create_user_prediction(a, "poly:iso-1", "YES", 0.7)
        db.create_user_prediction(b, "poly:iso-2", "NO", 0.6)
        a_rows = db.list_user_predictions(a)
        b_rows = db.list_user_predictions(b)
        self.assertEqual(len(a_rows), 1)
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(a_rows[0]["market_slug"], "poly:iso-1")
        self.assertEqual(b_rows[0]["market_slug"], "poly:iso-2")


class TestSubscriptionGate(unittest.TestCase):
    def test_admin_passes_gate(self):
        """Admins always have an 'active subscription' for gating purposes."""
        with db.conn() as c:
            c.execute(
                "INSERT INTO users (username, email, password_hash, password_salt, "
                "                   created_at, is_admin) "
                "VALUES (?, ?, 'h', 's', ?, 1)",
                ("upred_admin", "upred_admin@test", int(time.time())),
            )
            uid = c.execute(
                "SELECT id FROM users WHERE username = ?", ("upred_admin",)
            ).fetchone()["id"]
        self.assertTrue(db.has_any_active_subscription(uid))

    def test_no_sub_no_pass(self):
        uid = _mk_user("upred_nosub")
        self.assertFalse(db.has_any_active_subscription(uid))

    def test_active_sub_passes(self):
        uid = _mk_user("upred_withsub")
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, "
                "                            started_at, expires_at, source) "
                "VALUES (?, 'sports', 'monthly', 'active', ?, ?, 'placeholder')",
                (uid, int(time.time()), int(time.time()) + 30 * 86400),
            )
        self.assertTrue(db.has_any_active_subscription(uid))


if __name__ == "__main__":
    unittest.main()
