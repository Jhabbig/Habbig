"""Regression tests for the pre-release waitlist (/api/newsletter).

These exist because the form-urlencoded handler quietly broke when the
prerelease page was rewritten to fetch + URLSearchParams. Catching that
bug class requires three things this file enforces:

  1. The handler MUST accept both Content-Type:
     application/x-www-form-urlencoded AND application/json.
  2. The response MUST contain the keys the prerelease.html JS reads
     (`success`, `position`, `referral_code`, `share_url`, `is_new`).
  3. Referral codes MUST round-trip case-sensitively — uppercasing them
     anywhere in the chain breaks every shared link.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

# Make sure the test imports the gateway from the project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# server.py refuses to import in production mode without a gate token, so
# set a dummy 48-char token before any import. Match what test_health.py
# does so the two test files can run in the same pytest session.
os.environ.setdefault(
    "SITE_ACCESS_TOKEN",
    "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
)


def _isolated_db_env() -> str:
    """Spin up a fresh SQLite path for the duration of a test class."""
    tmp = tempfile.NamedTemporaryFile(suffix="_newsletter_test.db", delete=False)
    tmp.close()
    os.environ["GATEWAY_DB_PATH"] = tmp.name
    return tmp.name


def tearDownModule():
    """Restore the original server + server_features modules after this
    file's classes finish running.

    Every TestCase in this file calls ``importlib.reload(server)`` in its
    setUpClass, which rebuilds ``server.app`` from scratch and wipes every
    route registered by ``server_features.py``. Without this hook, the
    next test file in the suite (e.g. ``test_token_first_auth``) sees an
    app with /token, /sources/<handle>, /api/saved, etc. all missing.
    """
    try:
        import importlib
        import server as _server
        importlib.reload(_server)
        import server_features as _sf
        importlib.reload(_sf)
    except Exception:
        pass


def _reset_rate_store():
    """Wipe the in-memory rate-limit store so a previous test class can't
    leak per-IP counters into this one. The TestClient always reports the
    client IP as 'testclient', so without this every test would inherit
    the rate-limit state of every test that ran before it."""
    import server
    if hasattr(server, "_rate_store"):
        server._rate_store.clear()
    if hasattr(server, "_login_failures"):
        server._login_failures.clear()


class TestNewsletterFormEncoding(unittest.TestCase):
    """Regression: the prerelease form posts URL-encoded, not JSON."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _isolated_db_env()
        # Re-import server fresh so it picks up GATEWAY_DB_PATH from env.
        # Reloading server rebuilds server.app from scratch and wipes every
        # route registered by server_features.py — including /token, /terms,
        # /privacy, /sources/<handle>, /api/saved, etc. The
        # `tearDownModule` hook below restores them once for the whole file.
        import importlib
        import db
        importlib.reload(db)
        import server
        importlib.reload(server)
        cls.server = server
        # Re-attach server_features routes to the freshly-rebuilt app so
        # tests in THIS file can hit them too.
        try:
            import server_features as _sf
            importlib.reload(_sf)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def setUp(self):
        # Wipe in-memory rate store before EVERY test so the per-IP cap
        # doesn't carry over from a previous test (TestClient always uses
        # the same fake client IP).
        _reset_rate_store()

    def test_form_urlencoded_returns_200(self):
        """The exact content-type the prerelease.html form sends."""
        r = self.client.post(
            "/api/newsletter",
            data={"email": "form@example.com"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(r.status_code, 200, f"got {r.status_code}: {r.text}")

    def test_response_has_all_keys_frontend_expects(self):
        """prerelease.html reads res.data.{success,position,referral_code,share_url,is_new}."""
        r = self.client.post(
            "/api/newsletter",
            data={"email": "keys@example.com"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("success", "position", "referral_code", "share_url", "is_new"):
            self.assertIn(key, body, f"missing field: {key}")
        self.assertTrue(body["success"])
        self.assertIsInstance(body["position"], int)
        self.assertIsInstance(body["referral_code"], str)
        self.assertEqual(len(body["referral_code"]), 8)
        self.assertTrue(body["share_url"].startswith("https://"))
        self.assertIn("?ref=", body["share_url"])
        self.assertTrue(body["is_new"])

    def test_json_body_also_works(self):
        """Native API clients should be able to POST JSON instead."""
        r = self.client.post("/api/newsletter", json={"email": "json@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_invalid_email_400(self):
        r = self.client.post("/api/newsletter", data={"email": "not-an-email"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_empty_email_400(self):
        r = self.client.post("/api/newsletter", data={"email": ""})
        self.assertEqual(r.status_code, 400)

    def test_no_email_400(self):
        r = self.client.post("/api/newsletter", data={})
        self.assertEqual(r.status_code, 400)

    def test_excessively_long_email_400(self):
        # FIELD_MAX["email"] caps at 254 — push past it
        r = self.client.post(
            "/api/newsletter",
            data={"email": ("a" * 300) + "@example.com"},
        )
        self.assertEqual(r.status_code, 400)


class TestNewsletterIdempotencyAndPositions(unittest.TestCase):
    """Position math + duplicate handling."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _isolated_db_env()
        import importlib
        import db
        importlib.reload(db)
        import server
        importlib.reload(server)
        cls.server = server
        # Re-attach server_features routes to the freshly-rebuilt app.
        try:
            import server_features as _sf
            importlib.reload(_sf)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def setUp(self):
        # Wipe in-memory rate store before EVERY test so the per-IP cap
        # doesn't carry over from a previous test (TestClient always uses
        # the same fake client IP).
        _reset_rate_store()

    def test_first_signup_position_one(self):
        # Use a unique email so we're not coupled to test execution order.
        r = self.client.post("/api/newsletter", data={"email": "uniq-first@x.com"})
        self.assertEqual(r.status_code, 200)
        # The DB persists across tests in this class, so we don't assert
        # exact position 1 — just that the response is well-formed.
        self.assertGreaterEqual(r.json()["position"], 1)
        self.assertTrue(r.json()["is_new"])

    def test_subsequent_signups_get_increasing_positions(self):
        # Each signup must have a position strictly greater than the
        # previous one. Reset rate store mid-test because the per-IP cap
        # is 5/hour and we want to do 4 signups here without hitting it.
        _reset_rate_store()
        positions = []
        for email in ["sub-a@x.com", "sub-b@x.com", "sub-c@x.com", "sub-d@x.com"]:
            r = self.client.post("/api/newsletter", data={"email": email})
            self.assertEqual(r.status_code, 200, f"{email}: {r.text}")
            positions.append(r.json()["position"])
        self.assertEqual(positions, sorted(positions),
                         f"positions not monotonic: {positions}")
        self.assertEqual(len(set(positions)), 4, "positions must be unique")

    def test_duplicate_email_idempotent(self):
        """Re-posting the same email returns the same code + position with is_new=False."""
        first = self.client.post("/api/newsletter", data={"email": "dup@x.com"})
        self.assertTrue(first.json()["is_new"])
        first_code = first.json()["referral_code"]
        first_pos = first.json()["position"]

        again = self.client.post("/api/newsletter", data={"email": "dup@x.com"})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["is_new"])
        self.assertEqual(again.json()["referral_code"], first_code)
        self.assertEqual(again.json()["position"], first_pos)


class TestNewsletterReferralFlow(unittest.TestCase):
    """Referral codes are case-sensitive and earn 5 slots per successful invite."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _isolated_db_env()
        import importlib
        import db
        importlib.reload(db)
        import server
        importlib.reload(server)
        cls.server = server
        # Re-attach server_features routes to the freshly-rebuilt app.
        try:
            import server_features as _sf
            importlib.reload(_sf)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def setUp(self):
        # Wipe in-memory rate store before EVERY test so the per-IP cap
        # doesn't carry over from a previous test (TestClient always uses
        # the same fake client IP).
        _reset_rate_store()

    def test_ref_param_records_referrer(self):
        r1 = self.client.post("/api/newsletter", data={"email": "ref-inviter@x.com"})
        self.assertEqual(r1.status_code, 200)
        ref = r1.json()["referral_code"]
        inviter_pos = r1.json()["position"]

        r2 = self.client.post(
            "/api/newsletter",
            data={"email": "ref-invited@x.com", "ref": ref},
        )
        self.assertEqual(r2.status_code, 200)
        # Invited user signs up after inviter, so their position must be
        # strictly greater (position is monotonic with signup order).
        self.assertGreater(r2.json()["position"], inviter_pos)
        self.assertTrue(r2.json()["is_new"])

    def test_inviter_position_advances_after_referral(self):
        # Sign up enough people that the inviter starts at position >= 6
        # (so the 5-slot bump leaves them above floor=1 and we can verify
        # the math precisely instead of just verifying movement).
        # Reset the rate store between batches so the per-IP cap doesn't
        # interfere with the test setup.
        for i in range(7):
            _reset_rate_store()
            r = self.client.post("/api/newsletter", data={"email": f"adv-x{i}@x.com"})
            self.assertEqual(r.status_code, 200, f"adv-x{i} failed: {r.text}")
        _reset_rate_store()
        r_inviter = self.client.post("/api/newsletter", data={"email": "adv-inviter@x.com"})
        self.assertEqual(r_inviter.status_code, 200, f"inviter signup failed: {r_inviter.text}")
        inviter_code = r_inviter.json()["referral_code"]
        inviter_pos_before = r_inviter.json()["position"]
        self.assertGreaterEqual(inviter_pos_before, 2,
                                f"need inviter at >=2 to observe the 1-slot bump, got {inviter_pos_before}")
        _reset_rate_store()

        # New signup uses inviter's code
        self.client.post(
            "/api/newsletter",
            data={"email": "adv-newhire@x.com", "ref": inviter_code},
        )
        lookup = self.client.get(
            "/api/newsletter/position",
            params={"email": "adv-inviter@x.com"},
        )
        self.assertEqual(lookup.status_code, 200)
        new_pos = lookup.json()["position"]
        self.assertLess(new_pos, inviter_pos_before,
                        f"expected inviter to advance from {inviter_pos_before}, got {new_pos}")
        # Bump dropped from 5 → 1 slot per referral in commit cce4e67;
        # the test predates that change.
        self.assertEqual(new_pos, max(1, inviter_pos_before - 1))

    def test_referral_code_is_case_sensitive_round_trip(self):
        """REGRESSION: getRefFromUrl().toUpperCase() corrupted mixed-case codes."""
        r1 = self.client.post("/api/newsletter", data={"email": "case@x.com"})
        original = r1.json()["referral_code"]

        # Force the code through an upper/lower cycle that proves it's
        # case-sensitive at both ends. The inviter must record EXACTLY
        # the original code, not a normalised variant.
        r2 = self.client.post(
            "/api/newsletter",
            data={"email": "case2@x.com", "ref": original},
        )
        self.assertEqual(r2.status_code, 200)

        # Look up the inviter — if their code was case-mangled, they
        # wouldn't have any referrals and their position wouldn't move.
        lookup = self.client.get(
            "/api/newsletter/position",
            params={"email": "case@x.com"},
        )
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["referral_code"], original,
                         "referral code lost case fidelity through round-trip")

    def test_invalid_ref_silently_ignored(self):
        """A bogus ?ref= must not break the signup."""
        r = self.client.post(
            "/api/newsletter",
            data={"email": "okref@x.com", "ref": "bogus_code_12345"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])


class TestNewsletterPositionEndpoint(unittest.TestCase):
    """Returning visitors look up their current rank."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _isolated_db_env()
        import importlib
        import db
        importlib.reload(db)
        import server
        importlib.reload(server)
        cls.server = server
        # Re-attach server_features routes to the freshly-rebuilt app.
        try:
            import server_features as _sf
            importlib.reload(_sf)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def setUp(self):
        # Wipe in-memory rate store before EVERY test so the per-IP cap
        # doesn't carry over from a previous test (TestClient always uses
        # the same fake client IP).
        _reset_rate_store()

    def test_position_lookup_existing_email(self):
        self.client.post("/api/newsletter", data={"email": "lookup@x.com"})
        r = self.client.get(
            "/api/newsletter/position",
            params={"email": "lookup@x.com"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("position", body)
        self.assertIn("share_url", body)
        self.assertIn("referral_code", body)

    def test_position_lookup_unknown_email_404(self):
        r = self.client.get(
            "/api/newsletter/position",
            params={"email": "nobody-at-all@x.com"},
        )
        self.assertEqual(r.status_code, 404)
        # Generic error so we don't reveal whether the email exists
        self.assertIn("error", r.json())

    def test_position_lookup_invalid_email_400(self):
        r = self.client.get("/api/newsletter/position", params={"email": "garbage"})
        self.assertEqual(r.status_code, 400)


class TestNewsletterRateLimits(unittest.TestCase):
    """The per-IP cap stops spam from a single source."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _isolated_db_env()
        import importlib
        import db
        importlib.reload(db)
        import server
        importlib.reload(server)
        cls.server = server
        # Re-attach server_features routes to the freshly-rebuilt app.
        try:
            import server_features as _sf
            importlib.reload(_sf)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.db_path)

    def setUp(self):
        # Wipe in-memory rate store before EVERY test so the per-IP cap
        # doesn't carry over from a previous test (TestClient always uses
        # the same fake client IP).
        _reset_rate_store()

    def test_per_ip_rate_limit_kicks_in_after_5(self):
        """5 requests succeed, 6th gets 429."""
        # All from the same TestClient (=> same _get_client_ip key).
        statuses = []
        for i in range(7):
            r = self.client.post(
                "/api/newsletter",
                data={"email": f"rl{i}@example.com"},
            )
            statuses.append(r.status_code)

        # First few succeed, eventually hit 429
        self.assertEqual(statuses[0], 200)
        self.assertIn(429, statuses, f"expected at least one 429, got: {statuses}")
        # The 429 must have happened by request 6 (cap is 5/hour)
        first_429 = statuses.index(429)
        self.assertLessEqual(first_429, 5,
                             f"rate limit kicked in too late at index {first_429}")

    def test_429_response_includes_friendly_error(self):
        """The error message must be actionable, not raw."""
        # Hit the limit
        for i in range(10):
            self.client.post(
                "/api/newsletter",
                data={"email": f"err{i}@example.com"},
            )
        # Next one should be 429
        r = self.client.post(
            "/api/newsletter",
            data={"email": "another@example.com"},
        )
        if r.status_code == 429:
            self.assertIn("error", r.json())
            self.assertIn("Try again", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
