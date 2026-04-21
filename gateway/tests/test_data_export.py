"""Tests for the GDPR data export system.

Covers:
  * generator.build_zip — actually produces a valid ZIP with all required
    files, both CSV and JSON, plus Markdown for Intelligence conversations
  * Signed URL: round-trips, expires, rejects tampered tokens
  * db CRUD: create / get / list / latest_for_user / update / expire
  * API: rate limit (1/24h), 403 on other-user download, 404 on unknown
"""

from __future__ import annotations

USES_TESTDB = True

import io
import json
import os
import time
import unittest
import zipfile

from tests import _testdb  # noqa: F401 — shared in-memory DB

import db


# Ensure the secret exists for sign_download_url calls.
os.environ.setdefault("GATEWAY_COOKIE_SECRET", "test-secret-export")
os.environ.setdefault("DATA_EXPORT_DIR", "/tmp/narve-exports-tests")


# ── Signed URL ───────────────────────────────────────────────────────────────


class TestSignedDownloadURL(unittest.TestCase):

    def setUp(self):
        from exports import generator
        self.generator = generator

    def test_round_trip_valid(self):
        expires = int(time.time()) + 3600
        url = self.generator.sign_download_url(123, expires)
        # Token sits in the query string
        token = url.split("token=")[1]
        self.assertTrue(self.generator.verify_download_token(123, expires, token))

    def test_expired_token_rejected(self):
        past = int(time.time()) - 60
        url = self.generator.sign_download_url(7, past)
        token = url.split("token=")[1]
        self.assertFalse(self.generator.verify_download_token(7, past, token))

    def test_tampered_export_id_rejected(self):
        expires = int(time.time()) + 3600
        url = self.generator.sign_download_url(1, expires)
        token = url.split("token=")[1]
        # Same token, different export_id — must reject
        self.assertFalse(self.generator.verify_download_token(2, expires, token))

    def test_tampered_expiry_rejected(self):
        expires = int(time.time()) + 3600
        url = self.generator.sign_download_url(1, expires)
        token = url.split("token=")[1]
        self.assertFalse(self.generator.verify_download_token(1, expires + 60, token))

    def test_empty_token_rejected(self):
        expires = int(time.time()) + 3600
        self.assertFalse(self.generator.verify_download_token(1, expires, ""))


# ── DB CRUD ──────────────────────────────────────────────────────────────────


class TestExportRequestCRUD(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-crud@test.com'"
            ).fetchone()
        if row:
            cls.user_id = row["id"]
        else:
            cls.user_id = db.create_user(
                "export-crud@test.com", "ExpPass1!", "export_crud"
            )

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM data_export_requests WHERE user_id = ?",
                      (self.user_id,))

    def test_create_inserts_pending_row(self):
        eid = db.create_export_request(self.user_id)
        self.assertIsInstance(eid, int)
        row = db.get_export_request(eid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["user_id"], self.user_id)

    def test_get_export_scoped_by_user(self):
        eid = db.create_export_request(self.user_id)
        # Wrong user_id returns None
        row = db.get_export_request(eid, user_id=self.user_id + 9999)
        self.assertIsNone(row)
        # Right user_id returns the row
        row = db.get_export_request(eid, user_id=self.user_id)
        self.assertIsNotNone(row)

    def test_list_user_exports_orders_desc(self):
        e1 = db.create_export_request(self.user_id)
        time.sleep(0.01)
        e2 = db.create_export_request(self.user_id)
        rows = db.list_user_exports(self.user_id)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids[0], e2)
        self.assertIn(e1, ids)

    def test_latest_export_for_user(self):
        e1 = db.create_export_request(self.user_id)
        latest = db.latest_export_for_user(self.user_id)
        self.assertEqual(latest["id"], e1)

    def test_update_status_patches_only_provided_fields(self):
        eid = db.create_export_request(self.user_id)
        ok = db.update_export_status(
            eid,
            status="ready",
            file_size_bytes=4096,
            download_url="https://x/",
            expires_at=int(time.time()) + 3600,
        )
        self.assertTrue(ok)
        row = db.get_export_request(eid)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["file_size_bytes"], 4096)
        self.assertIsNotNone(row["download_url"])
        # error stayed null because we didn't pass it
        self.assertIsNone(row["error"])

    def test_expire_old_exports_flips_ready_past_expiry(self):
        eid = db.create_export_request(self.user_id)
        past = int(time.time()) - 100
        db.update_export_status(eid, status="ready", expires_at=past)
        flipped = db.expire_old_exports()
        flipped_ids = [r["id"] for r in flipped]
        self.assertIn(eid, flipped_ids)
        row = db.get_export_request(eid)
        self.assertEqual(row["status"], "expired")


# ── ZIP build ────────────────────────────────────────────────────────────────


class TestZipBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # One full user with at least one row in every easily-populated table.
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-zip@test.com'"
            ).fetchone()
        if row:
            cls.user_id = row["id"]
        else:
            cls.user_id = db.create_user(
                "export-zip@test.com", "ExpZip1!", "export_zip_user"
            )

        now = int(time.time())
        with db.conn() as c:
            # Subscription
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, "
                "status, started_at, source) "
                "VALUES (?, 'sports', 'monthly', 'active', ?, 'placeholder')",
                (cls.user_id, now),
            )
            # Followed source
            c.execute(
                "INSERT INTO followed_sources (user_id, source_handle, "
                "platform, followed_at) VALUES (?, 'demo_handle', 'twitter', ?)",
                (cls.user_id, now),
            )
            # Topic (Signal Search)
            c.execute(
                "INSERT INTO user_topics (user_id, name, keywords, schedule_minutes, "
                "is_active, created_at) "
                "VALUES (?, 'demo topic', ?, 60, 1, ?)",
                (cls.user_id, json.dumps(["fed", "rates"]), now),
            )
            # Intelligence conversation + 2 messages
            cur = c.execute(
                "INSERT INTO intelligence_conversations (user_id, title, "
                "message_count, created_at, updated_at) "
                "VALUES (?, 'Demo chat', 2, ?, ?)",
                (cls.user_id, now, now),
            )
            conv_id = cur.lastrowid
            c.execute(
                "INSERT INTO intelligence_messages (conversation_id, role, "
                "content, created_at) VALUES (?, 'user', ?, ?)",
                (conv_id, "What is the rate decision?", now),
            )
            c.execute(
                "INSERT INTO intelligence_messages (conversation_id, role, "
                "content, created_at) VALUES (?, 'assistant', ?, ?)",
                (conv_id, "Markets imply 70% no change.", now + 1),
            )

    def setUp(self):
        from exports.generator import EXPORT_DIR
        self.target = EXPORT_DIR / f"test-export-{self.user_id}.zip"
        if self.target.exists():
            self.target.unlink()

    def test_build_zip_produces_valid_archive(self):
        from exports.generator import build_zip

        manifest = build_zip(self.user_id, self.target)

        self.assertTrue(self.target.exists())
        self.assertTrue(zipfile.is_zipfile(self.target))
        self.assertEqual(manifest["user_id"], self.user_id)

    def test_zip_contains_all_expected_top_level_files(self):
        from exports.generator import build_zip

        build_zip(self.user_id, self.target)
        with zipfile.ZipFile(self.target) as zf:
            names = set(zf.namelist())

        # Top-level
        for required in (
            "README.txt",
            "account.json",
            "subscriptions.json",
            "metadata.json",
            "predictions/saved.csv",
            "predictions/saved.json",
            "markets/viewed.csv",
            "markets/viewed.json",
            "sources/followed.csv",
            "sources/followed.json",
            "signal_search/topics.json",
            "intelligence/conversations.json",
            "notifications/history.csv",
            "notifications/history.json",
            "activity/login_history.csv",
        ):
            self.assertIn(required, names, f"missing file: {required}")

    def test_account_json_is_valid_and_scrubs_password(self):
        from exports.generator import build_zip

        build_zip(self.user_id, self.target)
        with zipfile.ZipFile(self.target) as zf:
            payload = json.loads(zf.read("account.json"))

        self.assertEqual(payload["id"], self.user_id)
        self.assertEqual(payload["email"], "export-zip@test.com")
        self.assertNotIn("password_hash", payload)
        self.assertNotIn("password_salt", payload)

    def test_csv_format_has_header_row(self):
        from exports.generator import build_zip

        build_zip(self.user_id, self.target)
        with zipfile.ZipFile(self.target) as zf:
            csv_bytes = zf.read("sources/followed.csv")
        text = csv_bytes.decode("utf-8")
        # Header includes our follow column
        self.assertIn("source_handle", text.split("\n")[0])
        # Body row mentions the seeded handle
        self.assertIn("demo_handle", text)

    def test_intelligence_conversation_exported_as_markdown(self):
        from exports.generator import build_zip

        build_zip(self.user_id, self.target)
        with zipfile.ZipFile(self.target) as zf:
            md_files = [n for n in zf.namelist()
                        if n.startswith("intelligence/conversations/")
                        and n.endswith(".md")]
            self.assertTrue(md_files, "no conversation .md files in ZIP")
            md = zf.read(md_files[0]).decode("utf-8")
        self.assertIn("# Demo chat", md)
        self.assertIn("## User", md)
        self.assertIn("## Assistant", md)
        self.assertIn("rate decision", md)

    def test_metadata_manifest_has_row_counts(self):
        from exports.generator import build_zip

        build_zip(self.user_id, self.target)
        with zipfile.ZipFile(self.target) as zf:
            manifest = json.loads(zf.read("metadata.json"))

        self.assertEqual(manifest["schema"], "narve.gdpr.export.v1")
        # The seeded subscriptions row should be counted
        self.assertGreaterEqual(manifest["row_counts"]["subscriptions"], 1)
        self.assertGreaterEqual(manifest["row_counts"]["topics"], 1)
        self.assertGreaterEqual(manifest["row_counts"]["conversations"], 1)


# ── API routes ───────────────────────────────────────────────────────────────


class TestExportAPIRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server
        import server_features  # noqa: F401
        from starlette.testclient import TestClient

        cls.app = server.app
        cls.client = TestClient(cls.app)

        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-api@test.com'"
            ).fetchone()
        if row:
            cls.user_id = row["id"]
        else:
            cls.user_id = db.create_user(
                "export-api@test.com", "ExpApi1!", "export_api_user"
            )

        token = db.create_session(cls.user_id)
        # Bind the same CSRF token to the session row so the CSRF middleware
        # accepts our header. Without this, the session-based validator
        # ignores the cookie and refuses any "test_csrf" header.
        db.set_session_csrf(token, "test_csrf")
        cls.cookies = {server.COOKIE_NAME: token, "_csrf": "test_csrf"}
        cls.csrf = {"x-csrf-token": "test_csrf"}

        # Second user, for cross-user 403 test
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'export-other@test.com'"
            ).fetchone()
        if row:
            cls.other_id = row["id"]
        else:
            cls.other_id = db.create_user(
                "export-other@test.com", "ExpApi1!", "export_other_user"
            )
        other_token = db.create_session(cls.other_id)
        db.set_session_csrf(other_token, "test_csrf")
        cls.other_cookies = {
            server.COOKIE_NAME: other_token, "_csrf": "test_csrf"
        }

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM data_export_requests WHERE user_id IN (?, ?)",
                      (self.user_id, self.other_id))

    def test_create_requires_auth(self):
        # No session, no CSRF — both layers can reject. CSRF fires first
        # for POST so we accept either 401 or 403; both signal blocked.
        r = self.client.post("/api/v1/account/export", json={})
        self.assertIn(r.status_code, (401, 403))

    def test_create_returns_202_and_export_id(self):
        r = self.client.post(
            "/api/v1/account/export",
            json={},
            cookies=self.cookies,
            headers=self.csrf,
        )
        self.assertEqual(r.status_code, 202, msg=f"body={r.text!r} headers={dict(r.headers)!r}")
        data = r.json()
        self.assertIn("export_id", data)
        self.assertEqual(data["status"], "pending")

    def test_create_rate_limited_to_1_per_24h(self):
        # First request succeeds
        r1 = self.client.post(
            "/api/v1/account/export",
            json={},
            cookies=self.cookies,
            headers=self.csrf,
        )
        self.assertEqual(
            r1.status_code, 202,
            msg=f"first POST body={r1.text!r}",
        )
        # Second request immediately after must 429
        r2 = self.client.post(
            "/api/v1/account/export",
            json={},
            cookies=self.cookies,
            headers=self.csrf,
        )
        self.assertEqual(r2.status_code, 429)
        self.assertIn("Retry-After", r2.headers)

    def test_list_returns_user_exports_only(self):
        eid_mine = db.create_export_request(self.user_id)
        eid_other = db.create_export_request(self.other_id)

        r = self.client.get("/api/v1/account/export", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        ids = [e["id"] for e in r.json()["exports"]]
        self.assertIn(eid_mine, ids)
        self.assertNotIn(eid_other, ids)

    def test_status_404_on_other_users_export(self):
        eid_other = db.create_export_request(self.other_id)
        r = self.client.get(
            f"/api/v1/account/export/{eid_other}",
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 404)

    def test_download_404_when_not_ready(self):
        eid = db.create_export_request(self.user_id)
        r = self.client.get(
            f"/api/v1/account/export/{eid}/download",
            cookies=self.cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 404)

    def test_download_403_for_other_user_session(self):
        # Create + mark ready (simulating a finished export)
        eid = db.create_export_request(self.user_id)
        # Write a tiny placeholder file
        from exports.generator import EXPORT_DIR
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / f"test-{eid}.zip"
        path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty zip EOCD
        db.update_export_status(
            eid,
            status="ready",
            expires_at=int(time.time()) + 3600,
            file_path=str(path),
        )
        r = self.client.get(
            f"/api/v1/account/export/{eid}/download",
            cookies=self.other_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_download_with_signed_token_works_without_session(self):
        from exports.generator import EXPORT_DIR, sign_download_url

        eid = db.create_export_request(self.user_id)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / f"test-signed-{eid}.zip"
        path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        expires = int(time.time()) + 3600
        url = sign_download_url(eid, expires)
        token = url.split("token=")[1]
        db.update_export_status(
            eid, status="ready", expires_at=expires, file_path=str(path),
            download_url=url,
        )
        # No cookies — but with a valid signed token it works
        r = self.client.get(
            f"/api/v1/account/export/{eid}/download"
            f"?expires={expires}&token={token}",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("content-type"), "application/zip")


if __name__ == "__main__":
    unittest.main()
