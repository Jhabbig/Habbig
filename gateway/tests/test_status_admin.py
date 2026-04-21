"""Admin incident management + email subscription CRUD.

Admin tests do the full login-session dance (create user, mint session
cookie, set gate + CSRF cookies, include _csrf form field on POSTs).
Public subscription tests hit the canonical `/api/v1/status/*` paths so
they bypass the `/api/*` → `/api/v1/*` deprecation redirect.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402
from status_system import db as status_db  # noqa: E402


# Routes are exposed at /api/status/* internally; clients hit /api/v1/status/*
# which the APIVersionMiddleware rewrites before handing off to the handler.
API_SUBSCRIBE = "/api/v1/status/subscribe"
API_UNSUBSCRIBE = "/api/v1/status/unsubscribe"


def _ensure_admin_user(email: str = "statusadmin@narve.ai") -> int:
    """Create (or promote) a user with is_admin=1. Returns the user ID."""
    now = int(time.time())
    existing = db.get_user_by_email(email) if hasattr(db, "get_user_by_email") else None
    if existing:
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (existing["id"],))
        return existing["id"]
    # Raw insert — we don't need a real password hash for test-only admin auth.
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("statusadmin", email, "x" * 64, "salt", now),
        )
        return int(cur.lastrowid)


def _admin_client_with_csrf() -> tuple[TestClient, str]:
    """Return a (TestClient, csrf_token) for an admin user."""
    user_id = _ensure_admin_user()
    session_token = db.create_session(user_id)

    client = TestClient(server.app)
    # Narve's gate cookie — unlocks any non-PUBLIC path.
    client.cookies.set("narve_gate_access", "granted")
    # Session cookie — legacy fallback path in current_user().
    client.cookies.set("pm_gateway_session", session_token)

    # First request seeds the CSRF cookie on a GET.
    probe = client.get("/status")
    csrf_token = probe.cookies.get("_csrf") or client.cookies.get("_csrf") or ""
    if not csrf_token:
        # Fall back to a synthetic token and set the cookie manually.
        csrf_token = secrets.token_urlsafe(32)
        client.cookies.set("_csrf", csrf_token)
    return client, csrf_token


class TestAdminIncidentCrud(unittest.TestCase):
    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM incident_updates")
            c.execute("DELETE FROM incidents")
        self.client, self.csrf = _admin_client_with_csrf()

    def test_admin_create_incident_via_form(self):
        r = self.client.post(
            "/admin/incidents",
            data={
                "title": "Manual test incident",
                "description": "Created from test",
                "severity": "major",
                "status": "investigating",
                "components": ["app", "scraper"],
                "_csrf": self.csrf,
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 303),
                      f"unexpected status {r.status_code}: {r.text[:200]}")
        matching = [i for i in status_db.list_recent_incidents(limit=10)
                    if i["title"] == "Manual test incident"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["severity"], "major")
        self.assertEqual(set(matching[0]["affected_components"]), {"app", "scraper"})

    def test_admin_post_update_mirrors_status_onto_incident(self):
        inc_id = status_db.create_incident(
            title="Test status sync",
            severity="minor",
            affected_components=["app"],
            status="investigating",
        )
        r = self.client.post(
            f"/admin/incidents/{inc_id}/updates",
            data={
                "status": "monitoring",
                "message": "Applied a fix, watching the metrics.",
                "_csrf": self.csrf,
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 303),
                      f"got {r.status_code}: {r.text[:200]}")
        fresh = status_db.get_incident(inc_id)
        self.assertEqual(fresh["status"], "monitoring")
        updates = status_db.list_incident_updates(inc_id)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]["status"], "monitoring")

    def test_admin_resolve_incident(self):
        inc_id = status_db.create_incident(
            title="Resolve test",
            severity="minor",
            affected_components=["app"],
            status="monitoring",
        )
        r = self.client.post(
            f"/admin/incidents/{inc_id}/resolve",
            data={"_csrf": self.csrf},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 303))
        fresh = status_db.get_incident(inc_id)
        self.assertEqual(fresh["status"], "resolved")
        self.assertIsNotNone(fresh["resolved_at"])

    def test_admin_page_renders(self):
        r = self.client.get("/admin/status")
        self.assertEqual(r.status_code, 200, f"body={r.text[:300]}")
        self.assertIn("Status admin", r.text)


class TestAdminAccessControl(unittest.TestCase):
    def test_unauthenticated_admin_page_blocked(self):
        client = TestClient(server.app)
        client.cookies.set("narve_gate_access", "granted")
        r = client.get("/admin/status", follow_redirects=False)
        # Either redirect to /token or 403 — anything except 200 is fine.
        self.assertNotEqual(r.status_code, 200,
                            "admin page should not render without admin auth")

    def test_unauthenticated_admin_create_blocked(self):
        client = TestClient(server.app)
        client.cookies.set("narve_gate_access", "granted")
        r = client.post(
            "/admin/incidents",
            data={"title": "should not land", "severity": "minor", "status": "investigating"},
            follow_redirects=False,
        )
        # CSRF failure (403) or auth failure (redirect/403) — both acceptable.
        self.assertNotEqual(r.status_code, 200)
        self.assertNotEqual(r.status_code, 303)
        matches = [i for i in status_db.list_recent_incidents(100)
                   if i["title"] == "should not land"]
        self.assertEqual(len(matches), 0)


class TestSubscriptionFlow(unittest.TestCase):
    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM status_subscriptions")
        self.client = TestClient(server.app)
        self.client.cookies.set("narve_gate_access", "granted")

    def test_subscribe_with_valid_email(self):
        r = self.client.post(
            API_SUBSCRIBE,
            json={"email": "sub@narve.ai", "components": "all"},
        )
        self.assertEqual(r.status_code, 200, f"body={r.text[:300]}")
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["email"], "sub@narve.ai")
        self.assertIn("unsubscribe_url", body)

    def test_subscribe_rejects_invalid_email(self):
        r = self.client.post(
            API_SUBSCRIBE,
            json={"email": "not-an-email", "components": "all"},
        )
        self.assertEqual(r.status_code, 400)

    def test_subscribe_is_idempotent(self):
        r1 = self.client.post(
            API_SUBSCRIBE,
            json={"email": "idempotent@narve.ai", "components": "all"},
        )
        self.assertEqual(r1.status_code, 200, f"body={r1.text[:200]}")
        self.assertEqual(r1.json()["status"], "new")

        r2 = self.client.post(
            API_SUBSCRIBE,
            json={"email": "idempotent@narve.ai", "components": "all"},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "existing")

    def test_one_click_unsubscribe_via_get(self):
        sub = status_db.create_subscription("onclick@narve.ai", "all")
        r = self.client.get(f"/status/unsubscribe?token={sub['token']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("unsubscribed", r.text.lower())
        self.assertIsNone(status_db.get_subscription_by_token(sub["token"]))

    def test_api_unsubscribe_post(self):
        sub = status_db.create_subscription("apiuns@narve.ai", "all")
        r = self.client.post(API_UNSUBSCRIBE, json={"token": sub["token"]})
        self.assertEqual(r.status_code, 200, f"body={r.text[:300]}")
        self.assertTrue(r.json()["ok"])
        self.assertIsNone(status_db.get_subscription_by_token(sub["token"]))

    def test_api_unsubscribe_rejects_unknown_token(self):
        r = self.client.post(API_UNSUBSCRIBE, json={"token": "no-such-token-here"})
        self.assertEqual(r.status_code, 404)


class TestSubscribersListForComponents(unittest.TestCase):
    """status_db.list_subscribers_for_components filters correctly."""

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM status_subscriptions")

    def test_all_subscriber_matches_any_component(self):
        status_db.create_subscription("all@narve.ai", "all")
        status_db.create_subscription("app-only@narve.ai", ["app"])
        status_db.create_subscription("db-only@narve.ai", ["database"])

        app_subs = status_db.list_subscribers_for_components(["app"])
        emails = {s["email"] for s in app_subs}
        self.assertIn("all@narve.ai", emails)
        self.assertIn("app-only@narve.ai", emails)
        self.assertNotIn("db-only@narve.ai", emails)


if __name__ == "__main__":
    unittest.main()
