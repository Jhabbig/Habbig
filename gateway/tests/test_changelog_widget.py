"""Tests for the "What's new" widget — parser, /api/changelog,
/api/changelog/seen, and the dashboards.html mount.

Self-contained: parses a fixture-string CHANGELOG so the test isn't
coupled to whatever happens to be in the real repo CHANGELOG.md
today, and uses the standard testdb harness for the seen-state
endpoints.
"""

from __future__ import annotations

USES_TESTDB = True

import json
import time
import unittest

from tests import _testdb  # noqa: F401  — shared in-memory DB

import db
import changelog_routes


SAMPLE_CHANGELOG = """\
# Changelog

All notable changes here.

## [Unreleased]

### Added
- **Brand-new feature** that's about to ship — exciting stuff.
- A second item with `code` and a [link](https://x.example).

### Changed
- Tightened the `/login` rate limit to 5/min per email.

## [2026-04-22]

### Added
- Locale switcher in the sidebar.
- Client-side `t()` for JS-rendered UI.

### Fixed
- Dashboard headline rendering on iOS Safari 17.

## [2026-04-15] - 2026-04-15

### Security
- Upgraded `cryptography` to 44.0.1.
"""


# ── Parser ──────────────────────────────────────────────────────────────────


class TestParseChangelog(unittest.TestCase):
    def test_returns_entries_in_file_order(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["version"], "Unreleased")
        self.assertEqual(entries[1]["version"], "2026-04-22")
        self.assertEqual(entries[2]["version"], "2026-04-15")

    def test_entry_keys_are_stable_and_unique(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        keys = [e["key"] for e in entries]
        self.assertEqual(len(keys), len(set(keys)), "duplicate entry keys")
        # Re-parse and confirm keys are stable
        again = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        self.assertEqual([e["key"] for e in again], keys)

    def test_title_strips_markdown(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        # Bullet was: "**Brand-new feature** that's about to ship — exciting stuff."
        self.assertNotIn("**", entries[0]["title"])
        self.assertIn("Brand-new feature", entries[0]["title"])

    def test_summary_combines_remaining_bullets(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        summary = entries[0]["summary"]
        # Second bullet had `code` and [link](url) — both should be stripped.
        self.assertNotIn("`", summary)
        self.assertNotIn("](http", summary)

    def test_falls_back_to_no_notes_for_empty(self):
        text = "# Changelog\n\n## [0.0.0]\n"
        entries = changelog_routes.parse_changelog(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["summary"], "(no notes)")

    def test_parses_security_only_section(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        sec = next(e for e in entries if e["version"] == "2026-04-15")
        self.assertIn("cryptography", sec["title"])

    def test_handles_missing_changelog_file_gracefully(self):
        # When the file doesn't exist, parse_changelog with default
        # (None) reads CHANGELOG_PATH; if that's missing we should get
        # an empty list, not an exception.
        from pathlib import Path
        original = changelog_routes.CHANGELOG_PATH
        try:
            changelog_routes.CHANGELOG_PATH = Path("/nope/does/not/exist.md")
            self.assertEqual(changelog_routes.parse_changelog(), [])
        finally:
            changelog_routes.CHANGELOG_PATH = original


# ── DB helpers ──────────────────────────────────────────────────────────────


class TestSeenHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure migration 170's table exists even if the migration
        # runner didn't fire it yet under the testdb (idempotent CREATE).
        with db.conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS changelog_seen (
                    user_id INTEGER NOT NULL,
                    entry_key TEXT NOT NULL,
                    seen_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                    PRIMARY KEY (user_id, entry_key)
                )
                """
            )

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM changelog_seen")
        self.user_id = db.create_user(
            f"seen_{int(time.time()*1000)%999999}@e2e.test",
            "TestPass123!",
            f"seen_{int(time.time()*1000)%999999}",
        )

    def test_get_seen_keys_empty_for_new_user(self):
        self.assertEqual(changelog_routes.get_seen_keys(self.user_id), set())

    def test_mark_seen_inserts_new_keys(self):
        n = changelog_routes.mark_seen(self.user_id, ["abc123", "def456"])
        self.assertEqual(n, 2)
        self.assertEqual(
            changelog_routes.get_seen_keys(self.user_id),
            {"abc123", "def456"},
        )

    def test_mark_seen_idempotent(self):
        changelog_routes.mark_seen(self.user_id, ["abc"])
        n = changelog_routes.mark_seen(self.user_id, ["abc"])
        self.assertEqual(n, 0)
        self.assertEqual(
            changelog_routes.get_seen_keys(self.user_id), {"abc"},
        )

    def test_mark_seen_skips_blank_keys(self):
        n = changelog_routes.mark_seen(self.user_id, ["", "  ", "real"])
        self.assertEqual(n, 1)
        self.assertEqual(
            changelog_routes.get_seen_keys(self.user_id), {"real"},
        )


# ── HTTP routes ─────────────────────────────────────────────────────────────


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  — also runs register() chains
        from starlette.testclient import TestClient

        cls.server = server
        cls.client = TestClient(server.app)

        # Ensure changelog_seen table exists.
        with db.conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS changelog_seen (
                    user_id INTEGER NOT NULL,
                    entry_key TEXT NOT NULL,
                    seen_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                    PRIMARY KEY (user_id, entry_key)
                )
                """
            )

        cls.user_id = db.create_user(
            "changelog_user@e2e.test", "TestPass123!", "changelog_user",
        )
        cls.session_token = db.create_session(cls.user_id)

    def setUp(self):
        with db.conn() as c:
            c.execute(
                "DELETE FROM changelog_seen WHERE user_id = ?",
                (self.user_id,),
            )

    def _auth(self):
        return {
            "Cookie": f"pm_gateway_session={self.session_token}; _csrf=t",
            "x-csrf-token": "t",
        }

    def test_get_changelog_unauth_returns_entries(self):
        r = self.client.get("/api/changelog?limit=2")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("entries", body)
        self.assertIsInstance(body["entries"], list)
        self.assertLessEqual(len(body["entries"]), 2)
        # Anonymous: nothing is "seen", so unseen_count == len(entries).
        self.assertEqual(body["unseen_count"], len(body["entries"]))

    def test_get_changelog_clamps_limit(self):
        r = self.client.get("/api/changelog?limit=99999")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.json()["entries"]), 20)

    def test_get_changelog_includes_cache_header(self):
        r = self.client.get("/api/changelog?limit=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age", r.headers.get("cache-control", ""))

    def test_seen_post_anonymous_returns_persisted_false(self):
        r = self.client.post(
            "/api/changelog/seen",
            json={"keys": ["abc"]},
            headers={"x-csrf-token": "t", "Cookie": "_csrf=t"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["persisted"])

    def test_seen_post_authed_persists(self):
        r = self.client.post(
            "/api/changelog/seen",
            json={"keys": ["v1abc", "v2def"]},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["persisted"])
        self.assertEqual(body["marked"], 2)
        self.assertEqual(
            changelog_routes.get_seen_keys(self.user_id),
            {"v1abc", "v2def"},
        )

    def test_seen_post_idempotent(self):
        h = self._auth()
        self.client.post("/api/changelog/seen", json={"keys": ["x"]}, headers=h)
        r2 = self.client.post(
            "/api/changelog/seen", json={"keys": ["x"]}, headers=h,
        )
        self.assertEqual(r2.json()["marked"], 0)

    def test_seen_post_rejects_non_array(self):
        r = self.client.post(
            "/api/changelog/seen",
            json={"keys": "not-an-array"},
            headers=self._auth(),
        )
        self.assertEqual(r.status_code, 400)

    def test_authed_get_marks_seen_in_response(self):
        # Seed a seen row using whatever the parser produced for the
        # most-recent entry.
        entries = changelog_routes._parsed_entries()
        if not entries:
            self.skipTest("real CHANGELOG.md is missing in this checkout")
        first_key = entries[0]["key"]
        changelog_routes.mark_seen(self.user_id, [first_key])

        r = self.client.get("/api/changelog?limit=1", headers=self._auth())
        body = r.json()
        self.assertTrue(body["entries"][0]["seen"])
        self.assertEqual(body["unseen_count"], 0)


# ── Asset wiring ────────────────────────────────────────────────────────────


class TestRenderPageInjection(unittest.TestCase):
    def test_dashboards_template_carries_widget_mount(self):
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent
            / "static" / "dashboards.html"
        )
        page = path.read_text(encoding="utf-8")
        self.assertIn("data-changelog", page,
                      "dashboards.html should carry the widget mount")
        self.assertIn("data-changelog-list", page)
        self.assertIn("/changelog", page,
                      "footer link to /changelog missing")


if __name__ == "__main__":
    unittest.main()
