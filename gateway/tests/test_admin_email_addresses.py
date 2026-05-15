"""Tests for the /admin/email-addresses aggregator query.

Focus is the data layer (``aggregate_email_addresses``) — the page itself
is HTML and is exercised end-to-end by manual QA. We seed one row in each
of the 9 source tables and assert the UNION yields one row per email with
the correct ``source`` discriminator and the right ``all_sources`` stack
when one email lives in multiple sources.

Auth + render flow tests follow the same pattern as test_admin_newsletter
and aren't worth duplicating here; the high-value coverage is "did the
SQL UNION pick up every source we promised, and is the dedupe sane?"
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
from queries.admin import (  # noqa: E402
    EMAIL_SOURCE_LABELS,
    aggregate_email_addresses,
    count_email_addresses_by_source,
)


def _suffix() -> str:
    return f"{os.getpid()}"


def _seed_one_per_source(c, now: int) -> dict:
    """Insert one distinct email per source so every source label fires.

    Returns a {label: email} map so the assertions can pivot off the
    label and not the literal string.
    """
    sfx = _suffix()
    seeded: dict[str, str] = {}

    # 1) newsletter (post-launch, source!='prerelease', confirmed, no unsub)
    email_nl = f"nl_{sfx}@test.local"
    c.execute(
        "INSERT INTO newsletter_subscribers "
        "(email, subscribed_at, source, segment, frequency, "
        " confirmation_token, confirmed_at, last_confirmation_sent_at, unsubscribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (email_nl, now - 100, "landing", "all", "weekly", None, now - 100, None, None),
    )
    seeded["newsletter"] = email_nl

    # 5) prerelease (newsletter_subscribers with source='prerelease')
    email_pre = f"pre_{sfx}@test.local"
    c.execute(
        "INSERT INTO newsletter_subscribers "
        "(email, subscribed_at, source, segment, frequency, "
        " confirmation_token, confirmed_at, last_confirmation_sent_at, unsubscribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (email_pre, now - 200, "prerelease", "all", "weekly", None, now - 200, None, None),
    )
    seeded["prerelease"] = email_pre

    # 8) unsubscribe (newsletter_subscribers with unsubscribed_at IS NOT NULL)
    email_unsub = f"unsub_{sfx}@test.local"
    c.execute(
        "INSERT INTO newsletter_subscribers "
        "(email, subscribed_at, source, segment, frequency, "
        " confirmation_token, confirmed_at, last_confirmation_sent_at, unsubscribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (email_unsub, now - 300, "prerelease", "all", "weekly", None, now - 250, None, now - 50),
    )
    seeded["unsubscribe"] = email_unsub

    # 2) user (real account, password_hash != '')
    email_user = f"user_{sfx}@test.local"
    cur = c.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, "
        " created_at, is_admin, suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"user_{sfx}", email_user, "hashed", "salt", now - 400, 0, 0),
    )
    user_id = cur.lastrowid

    # 6) shell (users.email where password_hash == '')
    email_shell = f"shell_{sfx}@test.local"
    cur = c.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, "
        " created_at, is_admin, suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"shell_{sfx}", email_shell, "", "salt", now - 500, 0, 0),
    )
    shell_id = cur.lastrowid

    seeded["user"] = email_user
    seeded["shell"] = email_shell

    # 3) enquiry
    email_enq = f"enq_{sfx}@test.local"
    c.execute(
        "INSERT INTO enquiries (email, job_title, message, created_at, read) "
        "VALUES (?, ?, ?, ?, ?)",
        (email_enq, "PM", "Hello", now - 600, 0),
    )
    seeded["enquiry"] = email_enq

    # 4) feedback — links via user_id; needs the user row to already exist.
    #    Reuse the email_user row so feedback resolves to email_user.
    c.execute(
        "INSERT INTO feedback_submissions "
        "(user_id, type, message, priority, page_url, user_tier, "
        " screenshot_url, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, "bug", "broken", "high", "/x", "free", None, "open", now - 700),
    )
    # feedback row points at user_id which has email_user — the aggregator
    # treats this as a 'feedback' sighting that merges with 'user' on
    # dedupe. The label seeded["feedback"] is the email that should
    # appear in all_sources for both labels.
    seeded["feedback"] = email_user

    # 7) outbound queue (background_jobs name='send_email'). The test DB
    # may not have this table — older migrations build it later — so we
    # try and tolerate absence. The aggregator itself wraps the same
    # SELECT in try/except for the same reason.
    email_out = f"out_{sfx}@test.local"
    try:
        c.execute(
            "INSERT INTO background_jobs "
            "(name, payload, status, attempts, max_attempts, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("send_email",
             json.dumps({"to": email_out, "template": "welcome"}),
             "queued", 0, 3, now - 800),
        )
        seeded["outbound"] = email_out
    except Exception:
        seeded["outbound"] = None

    # 9) invite token target_email
    email_inv = f"inv_{sfx}@test.local"
    c.execute(
        "INSERT INTO invite_tokens "
        "(token, status, target_email, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"tok_{sfx}", "unclaimed", email_inv, "", now - 900),
    )
    seeded["invite"] = email_inv

    return seeded


class AggregateEmailAddressesTests(unittest.TestCase):
    """Verify the aggregator surfaces all 9 sources and dedupes sanely."""

    def setUp(self):
        # Clean each tested table so seed counts are deterministic. We
        # don't drop the schema — other tests in the suite share the same
        # in-memory DB via tests/_testdb.py.
        with db.conn() as c:
            c.execute("DELETE FROM newsletter_subscribers")
            c.execute("DELETE FROM enquiries")
            c.execute("DELETE FROM feedback_submissions")
            c.execute("DELETE FROM invite_tokens")
            try:
                c.execute("DELETE FROM background_jobs WHERE name = 'send_email'")
            except Exception:
                pass  # table not present in this test DB build
            # Users table cannot be wholly truncated (FK noise from sessions/
            # subscriptions etc), so just delete the test users we'll seed.
            c.execute(
                "DELETE FROM users WHERE email LIKE ?",
                (f"%_{_suffix()}@test.local",),
            )

    def test_every_source_label_appears(self):
        """One row per source → one row per email, label set covers all 9."""
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(limit=200)

        # Build a {email: primary_source} map. Filter to only the emails
        # we seeded so unrelated rows from earlier tests don't trip us.
        # Sources whose backing table is absent in the test DB (e.g.
        # outbound when background_jobs isn't built) come through as None.
        seeded_emails = set(v.lower() for v in seeded.values() if v)
        by_email = {
            r["email"]: r for r in rows if r["email"] in seeded_emails
        }

        # Every label that actually got seeded should map to a row.
        for label, email in seeded.items():
            if email is None:
                continue  # source skipped due to missing test-DB table
            key = email.lower()
            self.assertIn(
                key, by_email,
                f"source {label!r} (email {email!r}) missing from aggregator",
            )

        # Feedback shares its email with the 'user' row — assert the
        # dedupe collapsed them into a single output row, and that the
        # all_sources stack carries both labels.
        user_row = by_email[seeded["user"].lower()]
        self.assertIn("feedback", user_row["all_sources"])
        self.assertIn("user", user_row["all_sources"])

        # The distinct labels with their own emails should each appear as
        # the primary source on the corresponding row.
        primary_only = ("newsletter", "prerelease", "unsubscribe",
                        "enquiry", "outbound", "invite", "shell")
        for label in primary_only:
            email = seeded.get(label)
            if email is None:
                continue
            email = email.lower()
            self.assertEqual(
                by_email[email]["source"], label,
                f"{email!r} should have primary source {label!r}, "
                f"got {by_email[email]['source']!r}",
            )

    def test_dedup_keeps_most_recent_source(self):
        """When one email lives in multiple sources, the newest ts wins."""
        now = int(time.time())
        shared = f"shared_{_suffix()}@test.local"

        with db.conn() as c:
            # Sighting 1: invite (old).
            c.execute(
                "INSERT INTO invite_tokens "
                "(token, status, target_email, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"tokA_{_suffix()}", "unclaimed", shared, "", now - 1000),
            )
            # Sighting 2: outbound (newest). Skip if background_jobs isn't
            # in this test DB build.
            try:
                c.execute(
                    "INSERT INTO background_jobs "
                    "(name, payload, status, attempts, max_attempts, enqueued_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("send_email",
                     json.dumps({"to": shared, "template": "x"}),
                     "queued", 0, 3, now - 10),
                )
            except Exception:
                self.skipTest("background_jobs table not present in this test DB")

        rows = aggregate_email_addresses(limit=200)
        match = [r for r in rows if r["email"] == shared.lower()]
        self.assertEqual(len(match), 1,
                         "dedupe should yield exactly one row per email")
        row = match[0]
        self.assertEqual(row["source"], "outbound",
                         "newest ts (outbound) should win as primary source")
        self.assertIn("invite", row["all_sources"])
        self.assertIn("outbound", row["all_sources"])

    def test_source_filter(self):
        """Filtering by source returns only rows containing that source."""
        now = int(time.time())
        with db.conn() as c:
            _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(source="enquiry")
        for r in rows:
            # 'enquiry' must appear in the source-or-all_sources set.
            self.assertTrue(
                r["source"] == "enquiry" or "enquiry" in r["all_sources"],
                f"source filter leaked a non-enquiry row: {r!r}",
            )

    def test_counts_by_source(self):
        """count_email_addresses_by_source covers every label key."""
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        counts = count_email_addresses_by_source()
        for label in EMAIL_SOURCE_LABELS:
            self.assertIn(label, counts)
            if seeded.get(label) is None:
                continue  # source skipped due to missing test-DB table
            self.assertGreaterEqual(counts[label], 1,
                                    f"label {label!r} expected >=1, got {counts[label]}")

    # ------------------------------------------------------------------
    # Sort feature (commit 85914f4) — column-click sorting on every column.
    # The aggregator takes ``sort`` + ``sort_dir`` kwargs, whitelisted against
    # {ts, first_seen, email, source, status, user_id}. Anything else falls
    # back to ts/desc. We seed the standard fixture and pivot on the seeded
    # emails so unrelated rows from other tests don't trip the assertions.
    # ------------------------------------------------------------------

    def _seeded_view(self, rows, seeded):
        """Filter aggregator output to just the rows we seeded.

        Returns the rows in the order the aggregator emitted them — order is
        what we're asserting on, so we can't sort or set-ify here.
        """
        seeded_emails = set(v.lower() for v in seeded.values() if v)
        return [r for r in rows if r["email"] in seeded_emails]

    def test_sort_email_asc(self):
        """sort=email & dir=asc returns rows in alphabetical email order."""
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(
            limit=200, sort="email", sort_dir="asc",
        )
        emails = [r["email"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(emails, sorted(emails),
                         f"sort=email&dir=asc must be alphabetical, got {emails!r}")

    def test_sort_email_desc(self):
        """sort=email & dir=desc returns rows in reverse-alphabetical order."""
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(
            limit=200, sort="email", sort_dir="desc",
        )
        emails = [r["email"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(emails, sorted(emails, reverse=True),
                         f"sort=email&dir=desc must be reverse-alpha, got {emails!r}")

    def test_sort_ts_desc_default(self):
        """No explicit sort + dir=desc returns newest ts first.

        The aggregator defaults to sort=ts/sort_dir=desc, which is also
        what the admin page renders on first load. Seeded rows have
        decreasing ts the further down ``_seed_one_per_source`` you go
        (now-100 → now-900), so newest-first means newsletter (-100)
        ahead of invite (-900).
        """
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(limit=200, sort_dir="desc")
        ts_seq = [r["ts"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(ts_seq, sorted(ts_seq, reverse=True),
                         f"default sort must be ts/desc, got {ts_seq!r}")

    def test_sort_ts_asc(self):
        """sort=ts & dir=asc returns oldest first."""
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(
            limit=200, sort="ts", sort_dir="asc",
        )
        ts_seq = [r["ts"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(ts_seq, sorted(ts_seq),
                         f"sort=ts&dir=asc must be oldest-first, got {ts_seq!r}")

    def test_sort_source_asc(self):
        """sort=source & dir=asc groups rows by source alphabetically.

        Each seeded email lives in exactly one primary source (except
        the user/feedback dedupe — feedback collapses into the user row
        so the user row carries source='user'). Ordering primaries
        alphabetically gives a deterministic sequence we can assert on.
        """
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        rows = aggregate_email_addresses(
            limit=200, sort="source", sort_dir="asc",
        )
        sources = [r["source"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(sources, sorted(sources),
                         f"sort=source&dir=asc must be alpha by source, got {sources!r}")

    def test_sort_garbage_falls_back_to_ts_desc(self):
        """Bogus sort/dir values silently fall back to the ts/desc default.

        A malicious or fat-fingered ?sort=__class__&dir=lol must not crash
        the page or leak dict internals — the aggregator whitelists sort
        keys and any non-'asc' direction is treated as desc.
        """
        now = int(time.time())
        with db.conn() as c:
            seeded = _seed_one_per_source(c, now)

        # Should not raise.
        rows = aggregate_email_addresses(
            limit=200, sort="garbage", sort_dir="garbage",
        )
        ts_seq = [r["ts"] for r in self._seeded_view(rows, seeded)]
        self.assertEqual(
            ts_seq, sorted(ts_seq, reverse=True),
            f"unknown sort must fall back to ts/desc, got {ts_seq!r}",
        )


if __name__ == "__main__":
    unittest.main()
