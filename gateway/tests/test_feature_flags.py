"""Tests for features.is_feature_enabled — covers every evaluation branch.

Uses the shared in-memory DB from tests._testdb so we talk to real sqlite
through db.create_feature_flag / db.update_feature_flag without mocking.
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401  (imports the shared in-memory DB)

import db
import features


def _fresh_flag(**overrides):
    """Create a flag with a unique key per call, return the key."""
    import secrets
    key = "flag_" + secrets.token_hex(4)
    kwargs = {
        "key": key,
        "name": key,
        "description": "",
        "enabled_globally": False,
    }
    kwargs.update(overrides)
    db.create_feature_flag(**kwargs)
    return key


class TestFeatureFlagEvaluation(unittest.TestCase):
    def test_missing_flag_defaults_to_false(self):
        self.assertFalse(features.is_feature_enabled("nope_does_not_exist", {"user_id": 1}))

    def test_globally_disabled_wins(self):
        key = _fresh_flag(enabled_globally=False, enabled_for_user_ids=[1])
        self.assertFalse(features.is_feature_enabled(key, {"user_id": 1}))

    def test_globally_enabled_with_no_scopes_still_false(self):
        # Master switch on but no users/tiers/rollout means no one matches.
        key = _fresh_flag(enabled_globally=True)
        self.assertFalse(features.is_feature_enabled(key, {"user_id": 1}))

    def test_enabled_for_user_ids_matches(self):
        key = _fresh_flag(enabled_globally=True, enabled_for_user_ids=[7, 8])
        self.assertTrue(features.is_feature_enabled(key, {"user_id": 7}))
        self.assertFalse(features.is_feature_enabled(key, {"user_id": 9}))

    def test_disabled_for_user_ids_beats_enabled(self):
        key = _fresh_flag(
            enabled_globally=True,
            enabled_for_user_ids=[10],
            disabled_for_user_ids=[10],
        )
        self.assertFalse(features.is_feature_enabled(key, {"user_id": 10}))

    def test_rollout_is_deterministic(self):
        # Same user must always get the same answer for the same flag+%
        key = _fresh_flag(enabled_globally=True, rollout_percentage=50)
        first = features.is_feature_enabled(key, {"user_id": 42})
        for _ in range(10):
            self.assertEqual(first, features.is_feature_enabled(key, {"user_id": 42}))

    def test_rollout_zero_disables_everyone(self):
        key = _fresh_flag(enabled_globally=True, rollout_percentage=0)
        for uid in range(1, 200):
            self.assertFalse(features.is_feature_enabled(key, {"user_id": uid}))

    def test_rollout_hundred_enables_everyone(self):
        key = _fresh_flag(enabled_globally=True, rollout_percentage=100)
        for uid in range(1, 200):
            self.assertTrue(features.is_feature_enabled(key, {"user_id": uid}))

    def test_anonymous_never_matches_rollout(self):
        key = _fresh_flag(enabled_globally=True, rollout_percentage=100)
        self.assertFalse(features.is_feature_enabled(key, None))

    def test_update_flag_takes_effect(self):
        key = _fresh_flag(enabled_globally=False)
        user = {"user_id": 1}
        self.assertFalse(features.is_feature_enabled(key, user))
        db.update_feature_flag(key, enabled_globally=True, enabled_for_user_ids=[1])
        self.assertTrue(features.is_feature_enabled(key, user))

    def test_delete_flag_reverts_to_false(self):
        key = _fresh_flag(enabled_globally=True, enabled_for_user_ids=[1])
        user = {"user_id": 1}
        self.assertTrue(features.is_feature_enabled(key, user))
        db.delete_feature_flag(key)
        self.assertFalse(features.is_feature_enabled(key, user))

    def test_record_event_does_not_break_eval(self):
        key = _fresh_flag(enabled_globally=True, enabled_for_user_ids=[1])
        # record_event=True exercises the audit path; no exceptions bubble up.
        self.assertTrue(features.is_feature_enabled(key, {"user_id": 1}, record_event=True))


if __name__ == "__main__":
    unittest.main()
