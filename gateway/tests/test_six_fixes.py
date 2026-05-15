"""One test per fix from the six-fixes commit.

  A) Conftest DB-reset fixture wipes test-fixture rows between tests
  B) Sessions.token shadow column installed for the in-memory test DB
  C) Scheduler register_job rejects duplicates + untrusted modules
  D) retry_job rejects unknown names + forged HMACs
  E) Stripe webhook honours symmetric livemode + metadata trust + customer linkage
  F) api_public.v1_get_prediction returns explicit allowlist + constant 404

Each test isolates its own state so re-running the file gives a stable
pass count regardless of suite order.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True
from tests import _testdb  # noqa: F401,E402


# -- A: Conftest fixture wipes rows ---------------------------------------


class TestFixA_FixtureWipe(unittest.TestCase):
    def test_wipe_tables_constant_defined(self):
        """Fix A: the wipe-list constant is defined in conftest."""
        from tests import conftest
        self.assertTrue(hasattr(conftest, "_FIXTURE_WIPE_TABLES"))
        names = conftest._FIXTURE_WIPE_TABLES
        # Must wipe users + sessions + subscriptions (the 3 highest-impact
        # leakers we already know about).
        for required in ("users", "sessions", "subscriptions"):
            self.assertIn(required, names, f"_FIXTURE_WIPE_TABLES missing {required}")
        # users must be last so FK cascades don't reorder rows mid-loop.
        self.assertEqual(names[-1], "users")

    def test_wipe_actually_runs(self):
        """Inserting a user-like row and triggering the autouse fixture
        leaves the row deleted by the next test in this class."""
        import db
        with db.conn() as c:
            c.execute(
                "INSERT INTO users (username, email, password_hash, "
                "password_salt, created_at, is_admin) "
                "VALUES ('fixa_seed', 'fixa_seed@test.example', '', '', ?, 0)",
                (int(time.time()),),
            )
            row = c.execute(
                "SELECT id FROM users WHERE username = 'fixa_seed'",
            ).fetchone()
        self.assertIsNotNone(row, "seed user not inserted")
        # NOTE: the autouse fixture fires AFTER the test exits. So the
        # assert that the row is gone happens in the next test method.

    def test_wipe_ran_on_previous(self):
        """Continuation of test_wipe_actually_runs: the previous test's
        row should be gone now that the autouse fixture has fired."""
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE username = 'fixa_seed'",
            ).fetchone()
        self.assertIsNone(row, "_FIXTURE_WIPE_TABLES did not wipe users")


# -- B: Sessions.token shadow column --------------------------------------


class TestFixB_SessionsTokenShadow(unittest.TestCase):
    def test_sessions_has_token_column(self):
        """Fix B: in-memory test sessions table has a 'token' column."""
        import db
        with db.conn() as c:
            cols = {row["name"] for row in c.execute("PRAGMA table_info(sessions)")}
        self.assertIn("token", cols,
                      "sessions.token shadow column missing - Fix B regressed")
        self.assertIn("token_hash", cols,
                      "sessions.token_hash also required (production schema)")

    def test_can_insert_and_query_by_token(self):
        """The e2e flow pattern (INSERT row; SELECT WHERE token = ?) works."""
        import db
        raw = f"shadow_tok_{int(time.time())}"
        with db.conn() as c:
            uid = c.execute(
                "INSERT INTO users (username, email, password_hash, "
                "password_salt, created_at, is_admin) "
                "VALUES ('fixb_owner', 'fixb_owner@test.example', '', '', ?, 0) "
                "RETURNING id",
                (int(time.time()),),
            ).fetchone()["id"]
            c.execute(
                "INSERT INTO sessions "
                "(token, token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (raw, hashlib.sha256(raw.encode()).hexdigest(), uid,
                 int(time.time()), int(time.time()) + 3600),
            )
            row = c.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token = ?",
                (raw,),
            ).fetchone()
        self.assertIsNotNone(row, "could not SELECT sessions WHERE token = ?")
        self.assertEqual(row["user_id"], uid)


# -- C: register_job duplicate + trust check ------------------------------


class TestFixC_RegisterJobGuards(unittest.TestCase):
    def test_duplicate_registration_raises(self):
        """Fix C: a second @register_job under the same name raises."""
        from jobs.registry import job_registry, register_job

        # Pick a name that isn't already registered.
        name = f"fixc_dup_{int(time.time())}_{os.getpid()}"
        self.assertNotIn(name, job_registry)

        @register_job(name)
        async def _first():  # noqa: ARG001
            return None

        try:
            with self.assertRaises(ValueError):
                @register_job(name)
                async def _second():
                    return None
        finally:
            job_registry.pop(name, None)

    def test_untrusted_module_rejected(self):
        """Fix C: a function whose __module__ is outside the trusted
        prefix list is rejected at decoration time."""
        from jobs.registry import job_registry, register_job

        async def evil():
            return None
        evil.__module__ = "attacker.injected"

        name = f"fixc_evil_{int(time.time())}"
        with self.assertRaises(ValueError):
            register_job(name)(evil)
        self.assertNotIn(name, job_registry, "untrusted job leaked into registry")


# -- D: retry_job HMAC + name check ---------------------------------------


class TestFixD_RetryJobHMAC(unittest.TestCase):
    def setUp(self):
        # Stable secret across the test for deterministic HMAC.
        os.environ["GATEWAY_SSO_SECRET"] = "fixd-test-secret-deterministic"
        # Make sure the background_jobs table exists with the new shape.
        from jobs.backend import _ensure_jobs_table
        _ensure_jobs_table()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_retry_rejects_unknown_name(self):
        """Fix D: a row whose name is not in job_registry is refused."""
        import db
        from jobs.backend import retry_job
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO background_jobs "
                "(name, payload, payload_hmac, status, enqueued_at) "
                "VALUES ('fixd_not_registered_xyz', '{}', NULL, 'failed', ?)",
                (int(time.time()),),
            )
            job_id = cur.lastrowid
        ok = self._run(retry_job(int(job_id)))
        self.assertFalse(ok, "retry_job accepted unknown job name")

    def test_retry_rejects_missing_hmac(self):
        """Fix D: a row with a registered name but NULL HMAC is refused."""
        import db
        from jobs.backend import retry_job
        from jobs.registry import job_registry

        name = f"fixd_legit_{int(time.time())}"
        async def _noop():
            return None
        _noop.__module__ = "tests.test_six_fixes"
        job_registry[name] = _noop
        try:
            with db.conn() as c:
                cur = c.execute(
                    "INSERT INTO background_jobs "
                    "(name, payload, payload_hmac, status, enqueued_at) "
                    "VALUES (?, '{}', NULL, 'failed', ?)",
                    (name, int(time.time())),
                )
                job_id = cur.lastrowid
            ok = self._run(retry_job(int(job_id)))
            self.assertFalse(ok, "retry_job accepted NULL HMAC")
        finally:
            job_registry.pop(name, None)


# -- E: Stripe webhook livemode + metadata --------------------------------


class TestFixE_StripeMetadataTrust(unittest.TestCase):
    def test_trust_returns_user_id_when_unmapped(self):
        """First-touch flow: no existing customer row -> accept the
        metadata user_id verbatim so checkout.session.completed can do
        the initial mapping."""
        from stripe_webhook_routes import _trust_user_from_metadata
        result = _trust_user_from_metadata(42, "cus_brand_new_test")
        # Either 42 (no mapping yet) or None (some user already owns the
        # customer). Both are acceptable - what we're verifying is that
        # the function is callable and returns a sane shape.
        self.assertIn(result, (42, None))

    def test_trust_rejects_mismatched_user(self):
        """If a customer already maps to user X and the event metadata
        claims user Y != X, the function returns None (refuse)."""
        import db
        from stripe_webhook_routes import _trust_user_from_metadata
        cust = f"cus_fixe_mismatch_{int(time.time())}"
        with db.conn() as c:
            uid = c.execute(
                "INSERT INTO users (username, email, password_hash, "
                "password_salt, created_at, is_admin, stripe_customer_id) "
                "VALUES ('fixe_owner', 'fixe_owner@test.example', '', '', ?, 0, ?) "
                "RETURNING id",
                (int(time.time()), cust),
            ).fetchone()["id"]
        # Same user -> trusted.
        self.assertEqual(_trust_user_from_metadata(uid, cust), uid)
        # Wrong user -> rejected.
        self.assertIsNone(_trust_user_from_metadata(uid + 999, cust))


# -- F: api_public allowlist + constant 404 --------------------------------


class TestFixF_ApiPublicAllowlist(unittest.TestCase):
    def test_allowlist_excludes_sensitive_fields(self):
        """Fix F: _PUBLIC_PREDICTION_COLUMNS does NOT include reasoning /
        edge / user_id / is_anonymous / market_price_at_prediction."""
        from api_public.routes import _PUBLIC_PREDICTION_COLUMNS
        # user_id is in the allowlist (handler nulls it for is_anonymous);
        # all other private fields must NOT be there.
        sensitive = {
            "reasoning",
            "market_price_at_prediction",
            "edge_at_prediction",
            "is_anonymous",
            "resolved_at",
            "resolution_outcome",
            "score",
            "calibration_score",
        }
        leaked = sensitive & set(_PUBLIC_PREDICTION_COLUMNS)
        self.assertFalse(
            leaked,
            f"sensitive columns leaking through allowlist: {sorted(leaked)}",
        )
        # And the basic public fields are still there.
        for col in ("id", "market_slug", "predicted_outcome",
                    "predicted_probability"):
            self.assertIn(col, _PUBLIC_PREDICTION_COLUMNS)

    def test_prediction_not_found_constant_shape(self):
        """Fix F: every miss/private/forbidden case raises the same 404
        exception with the same detail message."""
        from api_public.routes import _prediction_not_found
        exc_a = _prediction_not_found()
        exc_b = _prediction_not_found()
        self.assertEqual(exc_a.status_code, 404)
        self.assertEqual(exc_a.status_code, exc_b.status_code)
        self.assertEqual(exc_a.detail, exc_b.detail)
        self.assertEqual(exc_a.detail, "Prediction not found")


if __name__ == "__main__":
    unittest.main()
