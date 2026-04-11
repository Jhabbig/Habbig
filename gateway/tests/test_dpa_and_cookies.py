"""Tests for the DPA page and expanded cookies notice on /privacy.

Exercises:
  - GET /dpa returns 200 and contains the key sub-processor list
  - GET /privacy contains the new cookie inventory (narve_gate_access, _csrf, etc.)
  - /dpa is registered as a public path (survives the gate middleware)
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


class TestDPAPage(unittest.TestCase):
    def test_dpa_returns_200(self):
        r = client.get("/dpa")
        self.assertEqual(r.status_code, 200)

    def test_dpa_contains_title(self):
        r = client.get("/dpa")
        self.assertIn("Data Processing Agreement", r.text)

    def test_dpa_lists_real_sub_processors(self):
        r = client.get("/dpa")
        self.assertIn("Cloudflare", r.text)
        self.assertIn("Sentry", r.text)
        self.assertIn("Stripe", r.text)
        self.assertIn("MailChannels", r.text)

    def test_dpa_does_not_list_supabase(self):
        r = client.get("/dpa")
        self.assertNotIn("Supabase", r.text)

    def test_dpa_is_public_path(self):
        self.assertIn("/dpa", server._PUBLIC_PATHS)


class TestPrivacyCookiesNotice(unittest.TestCase):
    def test_privacy_lists_real_cookies(self):
        r = client.get("/privacy")
        self.assertEqual(r.status_code, 200)
        # Cookie names from the real codebase
        self.assertIn("pm_gateway_session", r.text)
        self.assertIn("_csrf", r.text)
        self.assertIn("narve_gate_access", r.text)

    def test_privacy_mentions_essential_only(self):
        r = client.get("/privacy")
        self.assertIn("essential cookies", r.text.lower())

    def test_privacy_links_to_dpa(self):
        r = client.get("/privacy")
        self.assertIn("/dpa", r.text)


if __name__ == "__main__":
    unittest.main()
