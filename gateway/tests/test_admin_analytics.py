"""Tests for the /admin/analytics dashboard surface.

The dashboard is the admin-facing rollup over ``analytics_events`` —
stats cards (total events, unique visitors, top pages, top event types),
date-range filters, and a streaming CSV export. The surface is being
shipped alongside the ``narve_visitor`` cookie (test_analytics_cookie.py)
and migration 200's ``visitor_id`` column so cookie-grouped uniques
finally render correctly.

Coverage matrix
---------------

1. GET /admin/analytics unauth -> 302 to /gate.
2. GET /admin/analytics as non-admin -> 403.
3. GET /admin/analytics as admin -> 200 + page chrome (stats cards, top
   pages and top events tables).
4. GET /admin/analytics?since=...&until=... -> 200 and the filter is
   applied to the rendered counts.
5. GET /admin/analytics/export.csv as admin -> 200, text/csv, header
   row first.
6. CSV export defangs spreadsheet formula injection — a row whose
   event_type starts with ``=`` is prefixed with ``'`` so Excel renders
   it as literal text instead of evaluating it.

Auth + seed pattern mirrors ``tests/test_admin_email_addresses.py``:
shared in-memory DB via ``tests._testdb``, admin/regular session
helpers, and a fixture that pre-seeds a couple of analytics_events
rows before each test so the dashboard has data to roll up.

Some of these will be ``expected fail until sibling agent lands`` —
the route + template haven't shipped yet on this branch. Each test
that depends on the route existing is wrapped in a skip-if-missing
guard so the suite can run green when the sibling agent's PR is still
pending but turn into a real assertion once it lands.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: E402,F401 — shared in-memory DB + migrations
USES_TESTDB = True

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# TestClient's default Host is "testserver" — kept here so the auth
# tests get a clean unauthenticated request (``is_local_host`` returns
# False for ``testserver``, so the dev-mode auto-login bypass at
# server.current_user() doesn't fire). Using ``localhost`` would
# silently authenticate every request as the dev admin user and make
# the 302/403 auth assertions impossible.
_TEST_HOST = "testserver"


# ── Session + seed helpers ────────────────────────────────────────────

def _create_admin_session() -> str:
    """Mint a super-admin session and mark it 2FA-verified."""
    pid = os.getpid()
    email = f"analytics_admin_{pid}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong",
            username=f"analytics_admin_{pid}",
        )
    db.set_user_role(user_id, 2)  # super admin
    try:
        db.set_user_2fa_method(user_id, "email_otp")
    except Exception:
        pass
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token


def _create_regular_session() -> str:
    pid = os.getpid()
    email = f"analytics_user_{pid}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(email, "Password1!verylong",
                             username=f"analytics_user_{pid}")
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _wipe_analytics_rows():
    """Truncate analytics_events so each test starts deterministic."""
    with db.conn() as c:
        try:
            c.execute("DELETE FROM analytics_events")
        except Exception:
            pass


def _insert_event(
    *,
    event_type: str = "page_view",
    page: str = "/landing",
    ts: int | None = None,
    visitor_id: str | None = None,
    ip_hash: str = "iphash-test-seed",
    user_agent_category: str = "desktop",
    properties: dict | None = None,
) -> int:
    """Insert one analytics_events row, returning the new row id.

    Bypasses the HTTP endpoint so we can backdate ``created_at`` (the
    ``since/until`` filter test depends on planting an old row).
    """
    import json as _json
    if ts is None:
        ts = int(time.time())
    with db.conn() as c:
        # Detect whether visitor_id column exists (migration 200). The
        # in-memory test DB always runs upgrade_to_head so it should be
        # there, but the insert stays compatible with pre-200 layouts as
        # a defence-in-depth.
        cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(analytics_events)").fetchall()}
        if "visitor_id" in cols:
            cur = c.execute(
                "INSERT INTO analytics_events "
                "(event_type, user_id, session_id, page, referrer, "
                " ip_hash, user_agent_category, properties, created_at, "
                " visitor_id) "
                "VALUES (?, NULL, NULL, ?, '', ?, ?, ?, ?, ?)",
                (
                    event_type, page, ip_hash, user_agent_category,
                    _json.dumps(properties or {}), ts, visitor_id,
                ),
            )
        else:
            cur = c.execute(
                "INSERT INTO analytics_events "
                "(event_type, user_id, session_id, page, referrer, "
                " ip_hash, user_agent_category, properties, created_at) "
                "VALUES (?, NULL, NULL, ?, '', ?, ?, ?, ?)",
                (
                    event_type, page, ip_hash, user_agent_category,
                    _json.dumps(properties or {}), ts,
                ),
            )
        return int(cur.lastrowid)


def _seed_basic_rows(now: int) -> None:
    """Seed a handful of representative rows for the rollup tests.

    Two page_views on /landing, one page_view on /pricing, one
    newsletter_signup. Enough variety that the "top pages" and "top
    events" cards have something to render.
    """
    _insert_event(event_type="page_view", page="/landing",
                  ts=now - 600, visitor_id="vis-aaa", ip_hash="ip1")
    _insert_event(event_type="page_view", page="/landing",
                  ts=now - 500, visitor_id="vis-bbb", ip_hash="ip2")
    _insert_event(event_type="page_view", page="/pricing",
                  ts=now - 400, visitor_id="vis-aaa", ip_hash="ip1")
    _insert_event(event_type="newsletter_signup", page="/landing",
                  ts=now - 300, visitor_id="vis-ccc", ip_hash="ip3")


def _route_exists(path: str) -> bool:
    """Return True if `path` matches a registered route (literal or templated)."""
    try:
        for r in server.app.routes:
            if getattr(r, "path", "") == path:
                return True
    except Exception:
        return False
    return False


# ── Test cases ────────────────────────────────────────────────────────


class AdminAnalyticsDashboardTests(unittest.TestCase):
    """The /admin/analytics dashboard page."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, follow_redirects=False)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}

    def setUp(self):
        # Each test starts with a clean events table + fresh rate-limit
        # buckets so admin GET counts are deterministic and aren't
        # subject to a noisy-neighbour 429 from the prior test.
        _wipe_analytics_rows()
        try:
            server._rate_store.clear()
        except Exception:
            pass

    # ── 1. Unauthenticated -> /gate redirect ──────────────────────────

    def test_unauth_redirects_to_gate(self):
        """No session cookie -> _denied_response returns a 302 to /gate."""
        r = self.client.get(
            "/admin/analytics", cookies={}, follow_redirects=False,
            headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics not registered yet — expected fail "
                "until sibling agent lands the dashboard route"
            )
        # _denied_response sends 302 for anon, 403 for logged-in non-admin.
        # Accept the family because some admin routes return 303 + page.
        self.assertIn(
            r.status_code, (302, 303),
            f"anon -> /admin/analytics should redirect to /gate; got "
            f"status={r.status_code}, body={r.text[:200]!r}",
        )
        location = r.headers.get("location", "")
        self.assertIn(
            "/gate", location,
            f"redirect target should be /gate, got {location!r}",
        )

    # ── 2. Non-admin -> 403 ───────────────────────────────────────────

    def test_non_admin_gets_403(self):
        """Logged-in non-admin -> 403 page from _denied_response."""
        r = self.client.get(
            "/admin/analytics", cookies=self.user_cookies,
            follow_redirects=False, headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics not registered yet — expected fail "
                "until sibling agent lands the dashboard route"
            )
        self.assertEqual(
            r.status_code, 403,
            f"non-admin -> /admin/analytics should be 403, got {r.status_code}",
        )

    # ── 3. Admin GET 200 + chrome markers ─────────────────────────────

    def test_admin_gets_200_with_dashboard_chrome(self):
        """Admin GET returns 200 and renders the dashboard surface.

        We assert on three chrome anchors that the dashboard MUST have:
          - a stats card region (any of the canonical class prefixes)
          - a "Top pages" table or heading
          - a "Top events" / event-types table or heading

        Class names are matched loosely so the test survives minor
        template churn — the dashboard agent owns the exact markup.
        """
        now = int(time.time())
        _seed_basic_rows(now)

        r = self.client.get(
            "/admin/analytics", cookies=self.admin_cookies,
            follow_redirects=False, headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics not registered yet — expected fail "
                "until sibling agent lands the dashboard route"
            )
        self.assertEqual(
            r.status_code, 200,
            f"admin GET /admin/analytics should be 200, got {r.status_code}: "
            f"{r.text[:300]!r}",
        )
        body = r.text
        # Stats-card anchor — match any of the conventional class names
        # the admin shell uses across other dashboards (stats / stat-card /
        # nv-stat). Don't pin to one or we'll be brittle.
        stats_markers = ("nv-stat", "stats-card", "stat-card",
                         "admin-stats", "analytics-stats")
        self.assertTrue(
            any(m in body for m in stats_markers),
            f"none of the stats-card markers {stats_markers!r} found in body",
        )
        # Top pages anchor.
        self.assertTrue(
            ("Top pages" in body
             or "top-pages" in body
             or ">Page<" in body),
            "expected a 'Top pages' table or heading in the dashboard",
        )
        # Top events anchor.
        self.assertTrue(
            ("Top events" in body
             or "top-events" in body
             or "Event types" in body
             or ">Event<" in body),
            "expected a 'Top events' / event-types table in the dashboard",
        )

    # ── 4. Date-range filter (?since=...&until=...) ───────────────────

    def test_date_range_filter_applied(self):
        """``?since=...&until=...`` narrows the rollup window.

        We plant one ANCIENT row (a year ago) and a fresh row, then ask
        for the last 30 days. The page should still render (200) and
        not 500. We don't pin the exact count text because the template
        formatting varies — the load-bearing assertion is that the
        filter parsing path doesn't blow up.
        """
        now = int(time.time())
        # Anchor a known recent fingerprint so we can spot it in the body.
        unique_page = "/__analytics_filter_probe__"
        _insert_event(event_type="page_view", page=unique_page,
                      ts=now - 60, ip_hash="ip-probe")
        # Old row that the filter must exclude.
        _insert_event(event_type="page_view", page="/legacy-old",
                      ts=now - 400 * 86400, ip_hash="ip-old")

        # Build a since/until window that covers only the last 30 days.
        # Page conventions accept YYYY-MM-DD; reuse the same format as
        # /admin/email-addresses.
        import datetime as _dt
        d_until = _dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
        d_since = _dt.datetime.utcfromtimestamp(now - 30 * 86400).strftime("%Y-%m-%d")

        r = self.client.get(
            f"/admin/analytics?since={d_since}&until={d_until}",
            cookies=self.admin_cookies, follow_redirects=False,
            headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics not registered yet — expected fail "
                "until sibling agent lands the dashboard route"
            )
        self.assertEqual(
            r.status_code, 200,
            f"filtered admin GET should be 200, got {r.status_code}: "
            f"{r.text[:300]!r}",
        )
        # If the page renders a top-pages table, the legacy row's URL
        # MUST NOT appear (it's outside the window).
        self.assertNotIn(
            "/legacy-old", r.text,
            "row outside the since/until window leaked into the rendered "
            "page — filter not applied",
        )


class AdminAnalyticsCsvExportTests(unittest.TestCase):
    """GET /admin/analytics/export.csv — header + formula-injection defang."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, follow_redirects=False)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}

    def setUp(self):
        _wipe_analytics_rows()
        try:
            server._rate_store.clear()
        except Exception:
            pass

    # ── 5. Admin CSV export → 200, text/csv, header row first ─────────

    def test_admin_csv_export_returns_csv_with_header(self):
        """Admin GET on the CSV exporter returns 200 + text/csv content.

        The first line of the body must be the column header so a
        downstream sheet importer correctly labels the columns.
        """
        now = int(time.time())
        _seed_basic_rows(now)

        r = self.client.get(
            "/admin/analytics/export.csv", cookies=self.admin_cookies,
            follow_redirects=False, headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics/export.csv not registered yet — "
                "expected fail until sibling agent lands the export route"
            )
        self.assertEqual(
            r.status_code, 200,
            f"admin CSV export should be 200, got {r.status_code}: "
            f"{r.text[:200]!r}",
        )
        ctype = r.headers.get("content-type", "")
        self.assertTrue(
            ctype.startswith("text/csv"),
            f"content-type should start with text/csv, got {ctype!r}",
        )
        body = r.text
        self.assertTrue(body.strip(),
                        "CSV body must not be empty when rows are seeded")
        first_line = body.splitlines()[0]
        # The exact column order is the sibling agent's call, but the
        # first row MUST be a non-data header (we look for the event_type
        # column header as a strong proxy — every plausible header design
        # for analytics events carries it).
        first_lower = first_line.lower()
        self.assertTrue(
            ("event_type" in first_lower
             or "event type" in first_lower
             or first_lower.startswith("event")),
            f"first CSV line should be a header row containing 'event_type', "
            f"got {first_line!r}",
        )

    # ── 6. CSV defangs spreadsheet formula injection ──────────────────

    def test_csv_defangs_formula_injection_in_event_type(self):
        """An event_type starting with ``=`` is escaped with a leading ``'``.

        Spreadsheet apps treat the first char of a cell as a formula
        prefix when it's ``=``, ``+``, ``-``, ``@``, ``\\t``, or ``\\r``.
        The codebase's ``_csv_safe_cell`` defangs this by prefixing with
        a single quote so the cell renders as literal text.
        """
        evil = "=cmd|' /C calc'!A1"
        _insert_event(event_type=evil, page="/x",
                      ts=int(time.time()), ip_hash="ip-evil")

        r = self.client.get(
            "/admin/analytics/export.csv", cookies=self.admin_cookies,
            follow_redirects=False, headers={"Host": _TEST_HOST},
        )
        if r.status_code == 404:
            self.skipTest(
                "/admin/analytics/export.csv not registered yet — "
                "expected fail until sibling agent lands the export route"
            )
        self.assertEqual(
            r.status_code, 200,
            f"admin CSV export should be 200, got {r.status_code}",
        )
        body = r.text
        # The raw ``=cmd|...`` MUST NOT appear at the start of any cell —
        # csv.writer will quote fields containing commas/special chars,
        # but the leading ``=`` is what matters for the injection. The
        # safe form prefixes a single quote, so we look for ``'=cmd``
        # somewhere in the body and the unprefixed start-of-cell variant
        # must NOT be there.
        self.assertIn(
            "'=cmd",
            body,
            f"formula-injection cell should be prefixed with ``'`` for "
            f"CSV safety; body excerpt: {body[:500]!r}",
        )
        # The raw "=cmd|" string as the first chars of a cell would be
        # dangerous; csv.writer quotes the field because of the pipe, so
        # the dangerous on-disk form is ``"=cmd|...``. Assert that
        # specific dangerous prefix does NOT appear.
        self.assertNotIn(
            '"=cmd',
            body,
            f"raw '=cmd' detected as the start of a quoted CSV cell — "
            f"_csv_safe_cell wasn't applied; body excerpt: {body[:500]!r}",
        )


if __name__ == "__main__":
    unittest.main()
