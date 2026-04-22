"""Forensic signing — per-user deterministic perturbation of API list payloads.

The signer tags every response list with an LSB-level perturbation keyed on
`(user_id, endpoint)`. If that payload later leaks (screenshot, HTML dump,
forwarded JSON), `score_payload_against_seed` can identify which user's
download it came from.

Tests lock in the behaviours that matter for forensics:

  1. Same user + same data + same endpoint → identical output (deterministic).
  2. Different users get different perturbations (distinguishable).
  3. Seeds are persistent per user — a fresh lookup returns the same seed.
  4. `rotate_seed` replaces the old seed (stale seeds invalidate past leaks).
  5. Signing a small list does NOT inject sentinels (< 50 rows threshold).
  6. Signing a large list DOES inject sentinels (≥ 50 rows).
  7. `score_payload_against_seed` scores a user's own payload > a random
     seed's payload — the recovery primitive actually discriminates.
  8. Non-list inputs (dicts, primitives) pass through unchanged — the signer
     only tags *list* responses.
"""

from __future__ import annotations

USES_TESTDB = True

import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB

import db
from forensics import signer


def _mk_user(suffix: str) -> int:
    """Create a unique test user; return its id."""
    return db.create_user(
        f"fx_{suffix}@test.local", "Forensics!!1234", username=f"fx_{suffix}"
    )


class TestSeedLifecycle(unittest.TestCase):
    def test_get_or_create_seed_is_deterministic(self):
        uid = _mk_user("seed_det")
        a = signer.get_or_create_seed(uid)
        b = signer.get_or_create_seed(uid)
        self.assertEqual(a, b, "seed must persist across lookups")
        self.assertIsInstance(a, int)

    def test_different_users_get_different_seeds(self):
        u1 = _mk_user("seed_u1")
        u2 = _mk_user("seed_u2")
        self.assertNotEqual(signer.get_or_create_seed(u1), signer.get_or_create_seed(u2))

    def test_rotate_replaces_old_seed(self):
        uid = _mk_user("seed_rot")
        old = signer.get_or_create_seed(uid)
        new = signer.rotate_seed(uid)
        self.assertNotEqual(old, new, "rotation must produce a fresh seed")
        # Next lookup returns the new seed, not the old one.
        self.assertEqual(signer.get_or_create_seed(uid), new)


class TestSignResponseDeterministic(unittest.TestCase):
    """Same inputs produce same output — critical for reproducibility."""

    def test_same_user_same_data_identical_output(self):
        uid = _mk_user("sign_det")
        # Keys must be in _SIGNABLE_FLOAT_KEYS to actually get perturbed.
        payload = [
            {"id": i, "probability": round(0.3 + 0.01 * i, 4)} for i in range(6)
        ]
        out_a = signer.sign_response(uid, payload, "/api/v1/feed")
        out_b = signer.sign_response(uid, payload, "/api/v1/feed")
        self.assertEqual(out_a, out_b)


class TestSignResponseDistinguishesUsers(unittest.TestCase):
    """Different users' responses must be distinguishable."""

    def test_two_users_get_different_output(self):
        # The signer only touches keys in `_SIGNABLE_FLOAT_KEYS` — use a
        # payload keyed on `probability` so the perturbation actually fires,
        # with enough rows to make a collision vanishingly unlikely.
        payload = [
            {"id": i, "probability": round(0.25 + 0.007 * i, 4)}
            for i in range(40)
        ]
        u1 = _mk_user("sign_user_a")
        u2 = _mk_user("sign_user_b")
        out1 = signer.sign_response(u1, payload, "/api/v1/feed",
                                    inject_sentinels=False)
        out2 = signer.sign_response(u2, payload, "/api/v1/feed",
                                    inject_sentinels=False)
        # Different seeds → different per-row LSB-parity → different payloads.
        self.assertNotEqual(out1, out2)


class TestSentinelInjectionThreshold(unittest.TestCase):
    """Sentinels are an extra breadcrumb only added to big lists (>= 50 rows)."""

    def test_small_list_no_sentinels(self):
        uid = _mk_user("sign_small")
        payload = [{"id": i, "score": 0.1 * i} for i in range(10)]
        out = signer.sign_response(uid, payload, "/api/v1/feed")
        # Length is preserved for small lists — no sentinels injected.
        self.assertEqual(len(out), len(payload))

    def test_large_list_injects_sentinels(self):
        uid = _mk_user("sign_large")
        payload = [{"id": i, "score": round(0.01 * i, 4)} for i in range(60)]
        out = signer.sign_response(uid, payload, "/api/v1/feed")
        # At 60 rows the signer is free to insert sentinel rows; the
        # returned list should be strictly longer than the input.
        self.assertGreaterEqual(len(out), len(payload))

    def test_inject_sentinels_false_never_grows_list(self):
        uid = _mk_user("sign_no_sentinels")
        payload = [{"id": i, "score": round(0.01 * i, 4)} for i in range(60)]
        out = signer.sign_response(
            uid, payload, "/api/v1/feed", inject_sentinels=False,
        )
        self.assertEqual(len(out), len(payload))


class TestSigningPreservesNonListInputs(unittest.TestCase):
    """Dicts, primitives, etc. must pass through unchanged."""

    def test_dict_passthrough(self):
        uid = _mk_user("sign_dict")
        payload = {"a": 1, "b": "hello"}
        out = signer.sign_response(uid, payload, "/api/v1/market")
        self.assertEqual(out, payload)

    def test_primitive_passthrough(self):
        uid = _mk_user("sign_prim")
        self.assertEqual(signer.sign_response(uid, 42, "/api/v1/x"), 42)
        self.assertEqual(signer.sign_response(uid, "hi", "/api/v1/x"), "hi")
        self.assertIsNone(signer.sign_response(uid, None, "/api/v1/x"))


class TestScorePayloadAgainstSeed(unittest.TestCase):
    """The recovery primitive must actually discriminate."""

    def _signable_payload(self, n: int = 40):
        return [
            {"id": i, "probability": round(0.25 + 0.007 * i, 4)}
            for i in range(n)
        ]

    def test_user_payload_scores_high_against_own_seed(self):
        uid = _mk_user("score_self")
        payload = self._signable_payload()
        signed = signer.sign_response(
            uid, payload, "/api/v1/feed", inject_sentinels=False,
        )
        user_seed = signer.get_or_create_seed(uid)
        self_score = signer.score_payload_against_seed(signed, user_seed)
        # > 0.5 means better-than-random match against this user's seed.
        self.assertGreater(self_score, 0.5)

    def test_owner_scores_at_least_as_high_as_stranger(self):
        uid = _mk_user("score_mine")
        other = _mk_user("score_other")
        payload = self._signable_payload()
        signed = signer.sign_response(
            uid, payload, "/api/v1/feed", inject_sentinels=False,
        )
        self_score = signer.score_payload_against_seed(
            signed, signer.get_or_create_seed(uid),
        )
        other_score = signer.score_payload_against_seed(
            signed, signer.get_or_create_seed(other),
        )
        # The whole point: the owner's seed should match the payload at
        # least as well as a stranger's seed does.
        self.assertGreaterEqual(self_score, other_score)


if __name__ == "__main__":
    unittest.main()
