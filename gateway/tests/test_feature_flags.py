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


class TestPerSubproductFeatureFlags(unittest.TestCase):
    """Migration 183 dimension: per-subproduct flag overrides.

    Lookup precedence is:
      (key, subproduct_key=<slug>)  ->  evaluate that row
      (key, subproduct_key=NULL)    ->  evaluate the global row
    If only the global row exists, every subproduct shares its value;
    that's the back-compat path for every flag that existed before 183.
    """

    def test_subproduct_override_wins_over_global(self):
        # Global row is OFF; per-subproduct row is ON for "voters".
        # Caller passing subproduct_key="voters" should see True.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key, enabled_globally=False,
        )
        db.create_feature_flag(
            key=key, name=key,
            enabled_globally=True, enabled_for_user_ids=[1],
            subproduct_key="voters",
        )
        # Subproduct context => override row used => True.
        self.assertTrue(features.is_feature_enabled(
            key, {"user_id": 1}, subproduct_key="voters"
        ))
        # No subproduct context => global row used => False.
        self.assertFalse(features.is_feature_enabled(
            key, {"user_id": 1}
        ))

    def test_subproduct_override_can_kill_switch_global(self):
        # Mirror image: global is ON for everyone (rollout 100), but the
        # per-subproduct row is OFF. Callers on that subproduct see False
        # even though the global default says everyone is enabled.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key,
            enabled_globally=True, rollout_percentage=100,
        )
        db.create_feature_flag(
            key=key, name=key,
            enabled_globally=False,
            subproduct_key="crypto",
        )
        self.assertTrue(features.is_feature_enabled(
            key, {"user_id": 42}
        ))
        self.assertFalse(features.is_feature_enabled(
            key, {"user_id": 42}, subproduct_key="crypto"
        ))

    def test_missing_subproduct_row_falls_back_to_global(self):
        # Only a global row exists. A caller passing subproduct_key for a
        # subproduct without its own override should fall back to global.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key,
            enabled_globally=True, enabled_for_user_ids=[7],
        )
        self.assertTrue(features.is_feature_enabled(
            key, {"user_id": 7}, subproduct_key="sports"
        ))
        self.assertFalse(features.is_feature_enabled(
            key, {"user_id": 8}, subproduct_key="sports"
        ))

    def test_backfill_existing_flags_have_null_subproduct(self):
        # Flag created without a subproduct_key kwarg lands as global
        # (subproduct_key IS NULL). It remains evaluable via the
        # zero-subproduct lookup, mirroring the pre-183 behaviour for every
        # existing row that the migration backfilled.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key,
            enabled_globally=True, enabled_for_user_ids=[1],
        )
        row = db.get_feature_flag(key)
        self.assertIsNotNone(row)
        # The Row sqlite3 binding returns None for NULL columns.
        self.assertIsNone(row["subproduct_key"])
        # And the flag still resolves correctly without any subproduct ctx.
        self.assertTrue(features.is_feature_enabled(key, {"user_id": 1}))

    def test_same_key_can_coexist_across_subproducts(self):
        # The uniqueness constraint moved to (key, subproduct_key) in
        # migration 183 so the same key can live as a global row plus one
        # row per subproduct without colliding.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key, enabled_globally=True,
        )
        db.create_feature_flag(
            key=key, name=key, enabled_globally=True,
            subproduct_key="voters",
        )
        db.create_feature_flag(
            key=key, name=key, enabled_globally=False,
            subproduct_key="crypto",
        )
        # All three rows are independently fetchable.
        self.assertIsNotNone(db.get_feature_flag(key))
        self.assertIsNotNone(db.get_feature_flag(key, subproduct_key="voters"))
        self.assertIsNotNone(db.get_feature_flag(key, subproduct_key="crypto"))

    def test_update_per_subproduct_does_not_touch_global(self):
        # Updating a subproduct row leaves the global row untouched —
        # important so admins can experiment with an override without
        # accidentally flipping the global default.
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key, enabled_globally=False,
        )
        db.create_feature_flag(
            key=key, name=key, enabled_globally=False,
            subproduct_key="voters",
        )
        db.update_feature_flag(
            key, subproduct_key="voters",
            enabled_globally=True,
            enabled_for_user_ids=[5],
        )
        self.assertFalse(db.get_feature_flag(key)["enabled_globally"])
        self.assertTrue(db.get_feature_flag(key, subproduct_key="voters")["enabled_globally"])

    def test_delete_per_subproduct_does_not_touch_global(self):
        import secrets
        key = "flag_" + secrets.token_hex(4)
        db.create_feature_flag(
            key=key, name=key, enabled_globally=True, enabled_for_user_ids=[1],
        )
        db.create_feature_flag(
            key=key, name=key, enabled_globally=False,
            subproduct_key="crypto",
        )
        deleted = db.delete_feature_flag(key, subproduct_key="crypto")
        self.assertTrue(deleted)
        self.assertIsNone(db.get_feature_flag(key, subproduct_key="crypto"))
        # Global row survives and still resolves True for the enabled user.
        self.assertTrue(features.is_feature_enabled(key, {"user_id": 1}))


if __name__ == "__main__":
    unittest.main()
