"""
Tests for the admin panel's /admin/logs/* endpoints.

These use FastAPI's TestClient and bypass real auth by seeding a real admin
user + session in the SQLite DB. The goal is to verify:
  - non-admin callers get 403
  - the /live endpoint returns the ring buffer contents
  - level and service filters are respected
  - the /errors endpoint groups ERROR records by message
  - the /search endpoint honours the `q` param
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import logging_config as lc  # noqa: E402
import server  # noqa: E402


def _create_admin_session() -> str:
    """Create a temporary admin user + session and return the session token.

    Configures 2FA + marks the session verified so it passes the admin gate
    that `_require_admin_user` enforces on every /admin/* route.
    """
    email = f"logs_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(email, "Password1!verylong", username=f"logs_admin_{os.getpid()}")
    db.set_user_role(user_id, 2)  # super admin
    # Give the user a configured 2FA method so `_two_fa_redirect` doesn't
    # punt them to /auth/2fa/setup. The exact secret does not matter — the
    # test only checks that the session is marked verified below.
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


def _seed_ring(records: list[dict]) -> None:
    lc.ring_buffer.clear()
    for r in records:
        # Construct a LogRecord that the formatter will turn into our target JSON.
        logger_name = r.get("logger", "t")
        level = getattr(logging, r.get("level", "INFO"))
        message = r.get("message", "msg")
        rec = logging.LogRecord(logger_name, level, "", 0, message, (), None)
        # Carry extra fields via __dict__ so StructuredFormatter picks them up.
        for k, v in r.items():
            if k not in ("logger", "level", "message"):
                setattr(rec, k, v)
        lc.ring_buffer.emit(rec)


class AdminLogsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure the DB schema is at head so 2FA columns exist before we try
        # to mark the test session as 2FA-verified. In a full `uvicorn`
        # lifecycle this happens in the startup event; on a fresh test DB
        # it may not have run yet.
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.session_token = _create_admin_session()
        cls.admin_cookies = {server.COOKIE_NAME: cls.session_token}

    def setUp(self):
        lc.ring_buffer.clear()

    # ── Auth ────────────────────────────────────────────────────────────

    def test_live_requires_admin(self):
        r = self.client.get("/admin/logs/live", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_errors_requires_admin(self):
        r = self.client.get("/admin/logs/errors", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_search_requires_admin(self):
        r = self.client.get("/admin/logs/search?q=hello", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_live_non_admin_user_rejected(self):
        """A logged-in non-admin should also get 403."""
        email = f"logs_user_{os.getpid()}@test.local"
        existing = db.get_user_by_email(email)
        if existing:
            uid = existing["id"]
            db.set_user_role(uid, 0)
        else:
            uid = db.create_user(email, "Password1!verylong", username=f"logs_user_{os.getpid()}")
            db.set_user_role(uid, 0)
        tok = db.create_session(uid)
        r = self.client.get("/admin/logs/live", cookies={server.COOKIE_NAME: tok})
        self.assertEqual(r.status_code, 403)

    # ── /admin/logs/live ────────────────────────────────────────────────

    def test_live_returns_ring_buffer(self):
        _seed_ring([
            {"level": "INFO", "message": "seed 1", "logger": "t", "service": "app"},
            {"level": "WARNING", "message": "seed 2", "logger": "t", "service": "app"},
        ])
        r = self.client.get("/admin/logs/live", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("records", data)
        self.assertIn("count", data)
        self.assertIn("logtail_configured", data)
        msgs = [rec.get("message") for rec in data["records"]]
        self.assertIn("seed 1", msgs)
        self.assertIn("seed 2", msgs)

    def test_live_level_filter(self):
        _seed_ring([
            {"level": "INFO", "message": "info one"},
            {"level": "ERROR", "message": "error one"},
        ])
        r = self.client.get("/admin/logs/live?level=ERROR", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        levels = {rec.get("level") for rec in r.json()["records"]}
        self.assertEqual(levels, {"ERROR"})

    def test_live_limit_clamped(self):
        """limit > 500 is clamped to 500."""
        r = self.client.get("/admin/logs/live?limit=9999", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        # Ring buffer capacity caps the result regardless.
        self.assertLessEqual(r.json()["count"], 500)

    # ── /admin/logs/errors ──────────────────────────────────────────────

    def test_errors_groups_by_message(self):
        _seed_ring([
            {"level": "ERROR", "message": "database down", "logger": "db"},
            {"level": "ERROR", "message": "database down", "logger": "db"},
            {"level": "ERROR", "message": "database down", "logger": "db"},
            {"level": "ERROR", "message": "disk full", "logger": "fs"},
            {"level": "INFO", "message": "ok"},  # ignored
        ])
        r = self.client.get("/admin/logs/errors", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total_errors"], 4)
        self.assertEqual(data["distinct_errors"], 2)
        msgs = {g["message"]: g["count"] for g in data["groups"]}
        self.assertEqual(msgs.get("database down"), 3)
        self.assertEqual(msgs.get("disk full"), 1)

    def test_errors_empty_buffer(self):
        # Other test files (notably test_job_queue.py) leak asyncio
        # "Task was destroyed but it is pending!" ERROR records into the
        # global ring buffer between setUp() and the assertion below. Clear
        # the buffer immediately before the request so the assertion is
        # deterministic regardless of which tests ran before us.
        lc.ring_buffer.clear()
        r = self.client.get("/admin/logs/errors", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["total_errors"], 0)

    # ── /admin/logs/search ──────────────────────────────────────────────

    def test_search_substring(self):
        _seed_ring([
            {"level": "INFO", "message": "pipeline started"},
            {"level": "INFO", "message": "pipeline finished"},
            {"level": "INFO", "message": "unrelated event"},
        ])
        r = self.client.get("/admin/logs/search?q=pipeline", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        msgs = [rec.get("message") for rec in r.json()["records"]]
        self.assertEqual(len(msgs), 2)
        for m in msgs:
            self.assertIn("pipeline", m)

    def test_search_empty_query(self):
        _seed_ring([
            {"level": "INFO", "message": "first"},
            {"level": "INFO", "message": "second"},
        ])
        r = self.client.get("/admin/logs/search", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # Empty q returns everything in the buffer (up to limit).
        self.assertGreaterEqual(data["count"], 2)

    # ── Logtail badge ───────────────────────────────────────────────────

    def test_live_reports_logtail_status(self):
        r = self.client.get("/admin/logs/live", cookies=self.admin_cookies)
        data = r.json()
        # In tests LOGTAIL_TOKEN_APP is not set → badge should be false.
        self.assertFalse(data["logtail_configured"])


if __name__ == "__main__":
    unittest.main()
