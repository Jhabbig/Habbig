"""Tests for the ``narve_visitor`` anonymous tracking cookie.

The cookie is minted by ``PWAInjectionMiddleware`` on the first HTML
response a browser receives that ISN'T a pre-auth, admin, or DNT path,
and persists for one year so analytics events can be linked across
sessions without PII. ``auth.cookies.set_visitor_cookie`` owns the
flag set (HttpOnly=False, Secure=PRODUCTION, SameSite=Lax, 1y Max-Age,
22-char URL-safe opaque ID via secrets.token_urlsafe(16)).

Coverage matrix
---------------

1. First HTML hit without cookie -> Set-Cookie present, flag set correct.
2. Second hit WITH the cookie    -> idempotent (no re-set).
3. Cookie value is a non-empty URL-safe string (>=16 chars).
4. /admin/* hits do NOT mint the cookie.
5. /gate hits do NOT mint the cookie.
6. ``DNT: 1`` requests do NOT mint the cookie (Do-Not-Track respected).
7. POST /api/analytics/event WITH the cookie -> row persisted with
   ``visitor_id`` populated (column added by migration 200).
8. POST /api/analytics/event WITHOUT the cookie -> row persisted with
   ``visitor_id IS NULL`` (graceful fallback).

The endpoint half of the suite uses the same TestClient/fixture pattern
as ``tests/test_admin_email_addresses.py`` — shared in-memory DB via
``tests._testdb``, ``USES_TESTDB = True`` marker so the conftest re-pins
``db.conn``.
"""

from __future__ import annotations

import os
import sys
import unittest

# Pre-import env tweaks: drop the gate so HTML routes resolve without a
# cookie, force dev mode so Secure=False (production check honours
# PRODUCTION env), and raise the global per-IP cap so the endpoint
# half of the suite can fire multiple events without hitting the global
# limiter that TestClient shares one host for.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: E402,F401 — shared in-memory DB + migrations
USES_TESTDB = True

import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# Host the SubproductMiddleware accepts in dev; "testclient" (TestClient's
# default) IS in _TRUSTED_PROXY_HOSTS but ``localhost`` is safer because
# ``_DEV_HOSTS`` also includes it. Using one host for every call keeps the
# rate-limit principal stable so per-test isolation works.
_TEST_HOST = "localhost"


def _parse_set_cookie(header_val: str) -> dict:
    """Parse a raw Set-Cookie header into a {flag: value-or-True} dict.

    The httpx response we get from TestClient exposes the raw header so
    we can read the flags (HttpOnly, Secure, SameSite, Max-Age, Path,
    Domain) directly. Returns lowercased flag names and preserves the
    original case for values where it matters (SameSite=Lax).
    """
    parts = [p.strip() for p in header_val.split(";") if p.strip()]
    if not parts:
        return {}
    # First chunk is "name=value".
    name, _, value = parts[0].partition("=")
    out: dict = {"_name": name, "_value": value}
    for chunk in parts[1:]:
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            out[k.strip().lower()] = v.strip()
        else:
            out[chunk.strip().lower()] = True
    return out


def _visitor_set_cookie(response) -> str | None:
    """Return the raw Set-Cookie line for narve_visitor, or None."""
    for hdr in response.headers.get_list("set-cookie"):
        if hdr.startswith("narve_visitor="):
            return hdr
    return None


class TestVisitorCookieMint(unittest.TestCase):
    """Cookie-mint half of the suite — focuses on PWAInjectionMiddleware."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.client = TestClient(server.app, follow_redirects=False)

    def setUp(self):
        # Each test starts with a clean cookie jar so the second-hit
        # "idempotent" test isn't polluted by the first-hit test's cookie.
        try:
            self.client.cookies.clear()
        except Exception:
            pass
        # Drop rate-limit counters between tests so the global per-IP cap
        # doesn't accumulate across the suite.
        try:
            self.server._rate_store.clear()
        except Exception:
            pass

    def _get_html(self, path: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Host", _TEST_HOST)
        return self.client.get(path, headers=headers, **kwargs)

    # ── 1. First hit sets the cookie with the right flags ─────────────

    def test_first_hit_sets_cookie_with_expected_flags(self):
        """First HTML hit without the cookie mints it on the response.

        We pick the public landing (/) because it returns HTML and is
        gated only by the SITE_ACCESS_TOKEN check, which the test env
        clears. The cookie flags must match what the analytics tracker
        depends on: HttpOnly=False (JS reads it), SameSite=Lax (survives
        top-level navs but not cross-site POSTs), Max-Age ~= 1y, Path=/.
        """
        r = self._get_html("/")
        # Status varies on this build (200 prerelease, 302 to subdomain),
        # but as long as the body went through PWAInjectionMiddleware the
        # cookie is set. The cookie line is the load-bearing assertion.
        raw = _visitor_set_cookie(r)
        self.assertIsNotNone(
            raw,
            f"narve_visitor cookie not set on first HTML hit; status="
            f"{r.status_code!r}, set-cookie headers="
            f"{r.headers.get_list('set-cookie')!r}",
        )
        parsed = _parse_set_cookie(raw)
        # Value is a non-empty URL-safe string — the strict length check
        # lives in its own test so this one stays focused on flags.
        self.assertTrue(parsed["_value"], "cookie value must be non-empty")
        # HttpOnly must NOT be set (JS reads the value on every ping).
        self.assertNotIn("httponly", parsed,
                         "narve_visitor must be readable from JS (HttpOnly=False)")
        # SameSite=Lax for top-level nav survival without cross-site leak.
        self.assertEqual(parsed.get("samesite", "").lower(), "lax",
                         f"SameSite must be Lax, got {parsed.get('samesite')!r}")
        # Path=/ so the cookie travels with every request, not just /.
        self.assertEqual(parsed.get("path"), "/",
                         f"Path must be /, got {parsed.get('path')!r}")
        # Max-Age ~= 1y. The constant is 365*86400 = 31536000 — assert the
        # value is in a tight band so a future bump is caught.
        max_age = parsed.get("max-age")
        self.assertIsNotNone(max_age, "Max-Age must be set (1-year persistence)")
        self.assertEqual(int(max_age), 365 * 86400,
                         f"Max-Age must be 1 year (31536000s), got {max_age!r}")
        # Secure flag must follow PRODUCTION. Tests run with PRODUCTION
        # cleared so the cookie is NOT Secure here; in prod the same
        # code path sets Secure=True (set_visitor_cookie uses
        # _is_production()).
        self.assertNotIn(
            "secure", parsed,
            "Secure flag should be omitted when PRODUCTION env is empty (dev mode)",
        )

    # ── 2. Second hit is idempotent ───────────────────────────────────

    def test_second_hit_with_cookie_does_not_reset(self):
        """If the cookie is already present, the middleware skips Set-Cookie.

        Re-setting the cookie would (a) reset Max-Age every visit, which
        is OK but wasteful, and (b) change the value if we ever mint a
        fresh ID, which would break the cross-session grouping the
        cookie is FOR. The current code does ``read_visitor_cookie()``
        first and bails if anything's already there.
        """
        first = self._get_html("/")
        first_cookie = _visitor_set_cookie(first)
        self.assertIsNotNone(first_cookie,
                             "precondition: first hit must mint cookie")
        existing_value = _parse_set_cookie(first_cookie)["_value"]

        # Second hit WITH the cookie attached. We send the raw Cookie
        # header rather than letting the httpx jar persist it so the test
        # is explicit about what state we're sending.
        second = self._get_html(
            "/",
            headers={"Cookie": f"narve_visitor={existing_value}"},
        )
        self.assertIsNone(
            _visitor_set_cookie(second),
            "second hit with existing narve_visitor cookie must NOT re-set it",
        )

    # ── 3. Value is a non-empty URL-safe ID (>=16 chars) ──────────────

    def test_cookie_value_is_url_safe_and_long_enough(self):
        """secrets.token_urlsafe(16) yields a 22-char URL-safe base64 string.

        We assert >=16 chars (the lower bound the cookie helper promises)
        and that every char is URL-safe. The exact charset is the base64
        URL-safe alphabet plus the no-padding rule, so [A-Za-z0-9_-].
        """
        r = self._get_html("/")
        raw = _visitor_set_cookie(r)
        self.assertIsNotNone(raw, "no Set-Cookie line emitted")
        value = _parse_set_cookie(raw)["_value"]
        self.assertGreaterEqual(
            len(value), 16,
            f"cookie value must be >=16 chars, got {len(value)}: {value!r}",
        )
        # URL-safe base64 charset.
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "abcdefghijklmnopqrstuvwxyz"
                      "0123456789_-")
        bad = set(value) - allowed
        self.assertFalse(
            bad,
            f"cookie value contains non-URL-safe chars {bad!r}: {value!r}",
        )

    # ── 4. /admin/* is excluded ───────────────────────────────────────

    def test_admin_paths_do_not_set_cookie(self):
        """Admin surface MUST never auto-track. _should_inject_analytics
        bails on any path under /admin so neither the script nor the
        cookie get attached.
        """
        # Anonymous /admin hits get a 302 -> /gate; that's still served
        # via the gate redirect and is HTML, but the gate skip ALSO fires
        # so the cookie still must not be set. Use a path the middleware
        # actually sees as /admin (not the redirected target).
        r = self._get_html("/admin", follow_redirects=False)
        self.assertIsNone(
            _visitor_set_cookie(r),
            f"narve_visitor cookie was minted on /admin (status={r.status_code}); "
            f"admin surface must be excluded from auto-tracking",
        )

    # ── 5. /gate is excluded ──────────────────────────────────────────

    def test_gate_path_does_not_set_cookie(self):
        """/gate is the pre-auth landing — no tracking before the visitor
        even has site access. _should_inject_analytics short-circuits on
        path == '/gate' and 'gate/*'.
        """
        r = self._get_html("/gate")
        self.assertIsNone(
            _visitor_set_cookie(r),
            f"narve_visitor cookie was minted on /gate (status={r.status_code}); "
            f"pre-auth surface must be excluded",
        )

    # ── 6. DNT: 1 is respected ────────────────────────────────────────

    def test_dnt_request_does_not_set_cookie(self):
        """Visitors who send ``DNT: 1`` are honoured site-wide.

        The middleware checks the request header before deciding to mint
        the cookie. A DNT visit to / would normally trigger the mint;
        the header must suppress it (and the analytics <script> tag too,
        which is tested in the injector unit tests).
        """
        r = self._get_html("/", headers={"DNT": "1"})
        self.assertIsNone(
            _visitor_set_cookie(r),
            f"narve_visitor cookie was minted despite DNT: 1 (status={r.status_code}); "
            f"Do-Not-Track must be honoured",
        )


class TestAnalyticsEventPersistsVisitorId(unittest.TestCase):
    """POST /api/analytics/event — verify the visitor_id column wiring.

    Migration 200 added ``visitor_id`` to ``analytics_events``. The
    endpoint reads ``narve_visitor`` off the request cookie and passes
    it through to ``record_analytics_event``. We hit the endpoint with
    and without the cookie and read the row back via SQL to confirm the
    column got populated (or left NULL on the no-cookie path).
    """

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.client = TestClient(server.app, follow_redirects=False)

    def setUp(self):
        # Per-test rate-limit reset so /api/analytics/event doesn't 429
        # after the previous test cranked through events. Each test posts
        # one or two events, so the global cap isn't a concern.
        try:
            self.server._rate_store.clear()
        except Exception:
            pass
        # Clean any rows we wrote in the previous test so the "newest row"
        # assertions don't pick up the prior test's event.
        try:
            with db.conn() as c:
                c.execute(
                    "DELETE FROM analytics_events WHERE event_type = ?",
                    ("page_view",),
                )
        except Exception:
            pass

    def _post_event(self, *, visitor_cookie: str | None = None):
        headers = {"Host": _TEST_HOST}
        if visitor_cookie is not None:
            headers["Cookie"] = f"narve_visitor={visitor_cookie}"
        return self.client.post(
            "/api/analytics/event",
            json={
                "event_type": "page_view",
                "page": "/landing",
                "user_agent_category": "desktop",
            },
            headers=headers,
        )

    def _latest_visitor_id(self) -> str | None:
        """Return the visitor_id of the most-recently-inserted page_view row."""
        with db.conn() as c:
            row = c.execute(
                "SELECT visitor_id FROM analytics_events "
                "WHERE event_type = ? ORDER BY id DESC LIMIT 1",
                ("page_view",),
            ).fetchone()
        if row is None:
            return None
        # sqlite3.Row supports both index and key access.
        try:
            return row["visitor_id"]
        except (IndexError, KeyError):
            return row[0]

    # ── 7. With cookie -> visitor_id populated ────────────────────────

    def test_event_with_visitor_cookie_persists_visitor_id(self):
        """Cookie value flows through to the analytics_events row."""
        opaque = "test_visitor_abc123_xyz"
        r = self._post_event(visitor_cookie=opaque)
        self.assertEqual(
            r.status_code, 204,
            f"POST /api/analytics/event should accept (204), got {r.status_code}: {r.text!r}",
        )
        # Row must exist; visitor_id must equal the cookie value we sent.
        stored = self._latest_visitor_id()
        self.assertEqual(
            stored, opaque,
            f"visitor_id column should hold the cookie value, got {stored!r}",
        )

    # ── 8. Without cookie -> visitor_id NULL ──────────────────────────

    def test_event_without_visitor_cookie_has_null_visitor_id(self):
        """No cookie -> row inserts with visitor_id NULL (graceful)."""
        r = self._post_event(visitor_cookie=None)
        self.assertEqual(
            r.status_code, 204,
            f"endpoint should accept anonymous events (204), got {r.status_code}",
        )
        stored = self._latest_visitor_id()
        self.assertIsNone(
            stored,
            f"visitor_id should be NULL when no cookie sent, got {stored!r}",
        )


if __name__ == "__main__":
    unittest.main()
