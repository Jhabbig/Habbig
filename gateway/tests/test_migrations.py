"""Tests for Feature 5: versioned migrations."""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401 — sets up in-memory DB + migrations
import db  # noqa: E402
import migrations  # noqa: E402


class TestMigrationDiscovery(unittest.TestCase):
    def test_discover_finds_all_migrations(self):
        mods = migrations._discover_migrations()
        revisions = {m.revision for m in mods}
        self.assertIn("001", revisions)
        self.assertIn("002", revisions)
        self.assertIn("003", revisions)
        self.assertIn("004", revisions)
        self.assertIn("005", revisions)

    def test_migrations_are_sorted(self):
        mods = migrations._discover_migrations()
        revs = [m.revision for m in mods]
        self.assertEqual(revs, sorted(revs))


class TestUpgradeToHead(unittest.TestCase):
    def test_idempotent(self):
        first = migrations.upgrade_to_head()
        # Second call should apply 0 migrations.
        second = migrations.upgrade_to_head()
        self.assertEqual(second["applied"], 0)

    def test_schema_version_table_populated(self):
        migrations.upgrade_to_head()
        with db.conn() as c:
            rows = c.execute("SELECT revision FROM schema_version ORDER BY revision").fetchall()
        revisions = {r["revision"] for r in rows}
        self.assertIn("002", revisions)
        self.assertIn("003", revisions)
        self.assertIn("004", revisions)
        self.assertIn("005", revisions)

    def test_migration_002_creates_unsubscribe_table(self):
        migrations.upgrade_to_head()
        with db.conn() as c:
            c.execute("INSERT INTO email_unsubscribes (email, unsubscribed_from, token, created_at) VALUES (?, ?, ?, ?)",
                      ("a@b.com", "marketing", "tok", 1))
            row = c.execute("SELECT * FROM email_unsubscribes WHERE token = 'tok'").fetchone()
        self.assertIsNotNone(row)

    def test_migration_004_adds_waitlist_columns(self):
        migrations.upgrade_to_head()
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(newsletter_subscribers)")}
        self.assertIn("position", cols)
        self.assertIn("display_position", cols)
        self.assertIn("referral_code", cols)
        self.assertIn("referred_by_code", cols)

    def test_migration_005_adds_deletion_fields(self):
        migrations.upgrade_to_head()
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        self.assertIn("deletion_requested_at", cols)
        self.assertIn("deletion_scheduled_for", cols)
        self.assertIn("is_deleted", cols)


if __name__ == "__main__":
    unittest.main()
