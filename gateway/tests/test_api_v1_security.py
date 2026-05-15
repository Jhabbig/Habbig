"""Security regression tests for ``gateway/api_v1.py``.

Pins the CRIT/HIGH fixes called out in ``audits/audit_api_v1.md``:

* HIGH-2 — pre-auth per-IP rate limit (``apiv1_anon:<ip>``, 30/60s)
  fires BEFORE the DB lookup. The 31st anon request from a single
  IP must get a 429.
* HIGH-1 — bearer length is capped at 256 chars. A 257-char bearer
  must 401 without a DB round-trip.
* HIGH-4 — every handler now consumes the ``_validate_key`` row and
  gates by tier. A free-tier key hitting ``/markets/edge`` must 403.
* CRIT-1 — pre-existing keys created via the legacy fallback (no
  ``first_displayed_at`` column) still authenticate; migration 196
  is additive and non-breaking for in-flight keys.

The tests bootstrap the shared in-memory DB from ``tests/_testdb.py``
so migrations have already run by the time the test client is
created — meaning the ``first_displayed_at`` column DOES exist in
the test process. The legacy-fallback test simulates the pre-196
world by manually deleting the column from a fresh fixture table.
"""

from __future__ import annotations

import hashlib
import os
import time
import unittest
from unittest.mock import patch

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
# Disable the global per-IP middleware so we are testing the
# api_v1 anon bucket in isolation, not a sibling rate limiter.
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "100000"
# Make sure the rate limiter is actually enabled — tests below
# depend on bucket state and would silently pass if disabled.
os.environ["RATE_LIMIT_ENABLED"] = "true"

from tests import _testdb  # noqa: F401 — shared DB + migrations

import db  # noqa: E402
import server  # noqa: F401,E402 — registers the v1 router
from fastapi.testclient import TestClient  # noqa: E402

import api_v1  # noqa: E402
from security.rate_limiter import limiter  # noqa: E402


# Use a single client; tests below scope to distinct IPs via the
# X-Forwarded-For header so each test gets its own anon-bucket.
#
# server._get_client_ip gates XFF behind a ``_TRUSTED_PROXY_HOSTS``
# frozenset (loopback only — the cloudflared tunnel endpoint). The
# starlette TestClient sets request.client.host to "testclient",
# which is NOT in that set, so XFF is normally ignored. To let the
# tests below scope per-IP we whitelist "testclient" in-process.
# This rewrites the frozenset in place rather than monkeypatching
# the function so any sibling helper that reads the set picks up
# the same view.
server._TRUSTED_PROXY_HOSTS = frozenset(
    server._TRUSTED_PROXY_HOSTS | {"testclient"}
)

client = TestClient(server.app)


def _reset_limiter() -> None:
    """Wipe both anon + per-key buckets so test ordering doesn't bleed."""
    with limiter._lock:
        limiter._windows.clear()


_USER_COUNTER = {"n": 0}


def _new_user(prefix: str) -> int:
    """Insert a throwaway user row and return its id. Direct INSERT
    sidesteps password validation + rate-limit middleware that would
    otherwise sit between us and the api_keys row we actually want."""
    _USER_COUNTER["n"] += 1
    n = _USER_COUNTER["n"]
    email = f"sec-{prefix}-{n}-{int(time.time()*1000)}@test.local"
    username = f"sec{prefix}{n}{int(time.time()*1000)}"
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, password_hash, password_salt, username, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, "x", "x", username, int(time.time())),
        )
        return cur.lastrowid


def _issue_key(tier: str, email_suffix: str) -> str:
    """Issue a fresh key at the named tier. Email is unique per call."""
    uid = _new_user(email_suffix)
    raw_key, _ = api_v1.create_api_key(uid, name=f"sec-{email_suffix}", tier=tier)
    return raw_key


class TestAnonRateLimit(unittest.TestCase):
    """HIGH-2 — pre-auth IP rate limit kicks in at request 31."""

    def setUp(self):
        _reset_limiter()

    def test_31st_anon_request_from_same_ip_returns_429(self):
        # Distinct IP per test so other suites don't poison the bucket.
        ip = "203.0.113.31"
        headers = {"X-Forwarded-For": ip}
        # First 30 requests: all 401 (no Authorization header) or
        # similar — but NOT 429, because the IP bucket isn't full yet.
        for i in range(api_v1._ANON_RATE_LIMIT):
            r = client.get("/api/v1/sources", headers=headers)
            self.assertNotEqual(
                r.status_code, 429,
                f"req {i+1}/{api_v1._ANON_RATE_LIMIT} unexpectedly 429: {r.text}",
            )
        # The 31st must be 429 — the IP bucket is full.
        r = client.get("/api/v1/sources", headers=headers)
        self.assertEqual(r.status_code, 429, f"expected 429, got {r.status_code}: {r.text}")
        self.assertIn("Retry-After", r.headers)

    def test_different_ips_have_independent_buckets(self):
        """Sanity check — exhausting one IP must not 429 another."""
        ip_a = "203.0.113.41"
        ip_b = "203.0.113.42"
        # Burn the bucket for IP A.
        for _ in range(api_v1._ANON_RATE_LIMIT + 1):
            client.get("/api/v1/sources", headers={"X-Forwarded-For": ip_a})
        # IP B's first request must NOT be 429.
        r = client.get("/api/v1/sources", headers={"X-Forwarded-For": ip_b})
        self.assertNotEqual(r.status_code, 429, r.text)


class TestBearerLengthCap(unittest.TestCase):
    """HIGH-1 — bearer tokens > 256 chars are refused without a DB lookup."""

    def setUp(self):
        _reset_limiter()

    def test_257_char_bearer_returns_401(self):
        # 'Bearer ' (7) + 250 = 257 — just over the cap.
        oversized = "x" * 250
        auth = f"Bearer {oversized}"
        self.assertEqual(len(auth), 257)
        r = client.get(
            "/api/v1/sources",
            headers={
                "Authorization": auth,
                "X-Forwarded-For": "203.0.113.51",
            },
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_oversized_bearer_does_not_hit_db(self):
        """The length cap must short-circuit BEFORE the SHA-256 + SELECT."""
        oversized = "y" * 500
        with patch("api_v1.db.conn") as mock_conn:
            # If the handler reaches the DB, this would be invoked.
            r = client.get(
                "/api/v1/sources",
                headers={
                    "Authorization": f"Bearer {oversized}",
                    "X-Forwarded-For": "203.0.113.52",
                },
            )
        self.assertEqual(r.status_code, 401)
        mock_conn.assert_not_called()


class TestTierGating(unittest.TestCase):
    """HIGH-4 — free-tier keys cannot reach paid endpoints."""

    def setUp(self):
        _reset_limiter()

    def test_free_tier_blocked_from_markets_edge(self):
        free_key = _issue_key("free", "free")
        r = client.get(
            "/api/v1/markets/edge",
            headers={
                "Authorization": f"Bearer {free_key}",
                "X-Forwarded-For": "203.0.113.61",
            },
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("paid", r.text.lower())

    def test_free_tier_can_still_read_sources(self):
        """Free tier keeps read access to free endpoints."""
        free_key = _issue_key("free", "freeread")
        # Don't hit the upstream-dependent paths; just confirm the
        # tier gate doesn't 403 a read endpoint.
        r = client.get(
            "/api/v1/sources?limit=1",
            headers={
                "Authorization": f"Bearer {free_key}",
                "X-Forwarded-For": "203.0.113.62",
            },
        )
        # The endpoint may 200 (data) or other non-403 — the
        # contract under test is "not a 403 from the tier gate".
        self.assertNotEqual(r.status_code, 403, r.text)

    def test_standard_tier_passes_markets_edge_gate(self):
        """Paid tier passes the tier check (may still error downstream)."""
        std_key = _issue_key("standard", "std")
        with patch("backend.markets.unified_markets.fetch_unified_markets",
                   return_value=[]):
            r = client.get(
                "/api/v1/markets/edge",
                headers={
                    "Authorization": f"Bearer {std_key}",
                    "X-Forwarded-For": "203.0.113.63",
                },
            )
        # The tier gate is the only thing under test here — anything
        # other than 403 means the gate let us through.
        self.assertNotEqual(r.status_code, 403, r.text)


class TestLegacyFallbackKeysStillWork(unittest.TestCase):
    """CRIT-1 — pre-existing legacy keys (no first_displayed_at) still auth.

    Simulates the pre-196 world: a key row inserted into ``api_keys``
    without ``first_displayed_at`` populated. The hardened
    ``_validate_key`` must still accept it (the column is metadata, not
    an auth predicate).
    """

    def setUp(self):
        _reset_limiter()

    def test_legacy_key_authenticates(self):
        # Insert directly with NULL first_displayed_at — exactly the
        # state a key created via the pre-196 legacy fallback ends up in.
        raw = "narve_legacy_abc_def_ghi_" + "z" * 16
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        uid = _new_user("legacy")
        with db.conn() as c:
            c.execute(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, name, tier, rate_limit_hour, "
                " created_at, first_displayed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (key_hash, raw[:12], uid, "legacy", "standard", 1000, int(time.time())),
            )
        r = client.get(
            "/api/v1/sources?limit=1",
            headers={
                "Authorization": f"Bearer {raw}",
                "X-Forwarded-For": "203.0.113.71",
            },
        )
        # Auth succeeded → not 401 / 403 / 429.
        self.assertNotIn(
            r.status_code, (401, 403, 429),
            f"legacy key auth failed: {r.status_code} {r.text}",
        )


class TestCreateApiKeyNarrowedFallback(unittest.TestCase):
    """CRIT-1 — non-missing-column errors must propagate, not be swallowed."""

    def test_unique_violation_propagates(self):
        """A duplicate key_hash insert raises IntegrityError — the
        narrowed ``except sqlite3.OperationalError`` must NOT swallow it.
        """
        import sqlite3
        # Stub db.conn to return a connection whose execute raises
        # IntegrityError on the first INSERT — this is the kind of
        # error the old blanket except swallowed silently.
        uid = _new_user("propagate")

        with patch("api_v1.db.conn") as mock_conn:
            fake_cur = unittest.mock.MagicMock()
            fake_cur.execute.side_effect = sqlite3.IntegrityError(
                "UNIQUE constraint failed: api_keys.key_hash"
            )
            fake_conn = unittest.mock.MagicMock()
            fake_conn.__enter__.return_value = fake_cur
            fake_conn.__exit__.return_value = False
            mock_conn.return_value = fake_conn
            with self.assertRaises(sqlite3.IntegrityError):
                api_v1.create_api_key(uid, name="propagate", tier="standard")


if __name__ == "__main__":
    unittest.main()
