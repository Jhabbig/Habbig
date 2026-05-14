"""Tests for cursor pagination on /api/public/v1/feed.

Covers the keyset cursor contract:
  - Page 1 (no cursor) returns the latest N rows and a `next_before` cursor
    equal to the smallest id in the page.
  - Page 2 (pass cursor) returns the next N rows with no overlap with page 1.
  - Page N at end of feed returns an empty list and `next_before = None`.

The 100-item hard cap on `limit` is also exercised so callers can't blow
past the public-API ceiling by passing a bigger number.
"""

from __future__ import annotations

import os
import unittest

from tests import _testdb  # noqa: F401  — shared in-memory DB bootstrap

os.environ["PRODUCTION"] = "0"

import db
import api_v1
from fastapi.testclient import TestClient


_HOST = {"host": "narve.ai"}


def _mk_user(email: str) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0])


def _mint_key(user_id: int, *, scopes: str = "read", rate_limit: int = 10_000):
    raw, key_id = api_v1.create_api_key(user_id=user_id, name="t", tier="standard")
    with db.conn() as c:
        c.execute(
            "UPDATE api_keys SET scopes = ?, rate_limit_hour = ? WHERE id = ?",
            (scopes, rate_limit, key_id),
        )
    return raw, key_id


def _client():
    import server
    return TestClient(server.app)


def _seed_predictions(handle: str, count: int) -> list[int]:
    """Insert ``count`` predictions for ``handle`` in time order. Returns
    the inserted row ids in insertion order (so ids[-1] is the newest)."""
    import time
    base = int(time.time())
    ids: list[int] = []
    with db.conn() as c:
        for i in range(count):
            cur = c.execute(
                "INSERT INTO predictions "
                "(source_handle, category, content, extracted_at) "
                "VALUES (?, ?, ?, ?)",
                (handle, "economics", f"pred-{i}", base + i),
            )
            ids.append(int(cur.lastrowid))
    return ids


class TestFeedCursorPagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"feedpag_{id(cls)}@t.com")
        cls.raw, _ = _mint_key(cls.uid)
        cls.c = _client()
        cls.handle = f"feedpag_{id(cls)}_src"
        # Seed exactly 25 rows so we can walk multiple pages with limit=10.
        cls.ids = _seed_predictions(cls.handle, 25)

    def _hdr(self):
        return {**_HOST, "authorization": f"Bearer {self.raw}"}

    def _feed_ids(self, items):
        return [int(it["id"]) for it in items if it.get("source_handle") == self.handle]

    def test_page_1_no_cursor_returns_latest_and_cursor(self):
        r = self.c.get("/api/public/v1/feed?limit=10", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("feed", body)
        self.assertIn("next_before", body)
        self.assertEqual(body["before_id"], None)

        mine = self._feed_ids(body["feed"])
        self.assertEqual(len(mine), 10)
        # Newest first — id DESC.
        self.assertEqual(mine, sorted(mine, reverse=True))
        # next_before == smallest id in the page, so the next request
        # picks up strictly older rows.
        self.assertEqual(body["next_before"], min(mine))

    def test_page_2_with_cursor_no_overlap(self):
        # Page 1.
        r1 = self.c.get("/api/public/v1/feed?limit=10", headers=self._hdr())
        page1 = self._feed_ids(r1.json()["feed"])
        cursor = r1.json()["next_before"]
        self.assertIsNotNone(cursor)

        # Page 2.
        r2 = self.c.get(
            f"/api/public/v1/feed?limit=10&before_id={cursor}",
            headers=self._hdr(),
        )
        self.assertEqual(r2.status_code, 200)
        body2 = r2.json()
        page2 = self._feed_ids(body2["feed"])

        self.assertEqual(len(page2), 10)
        self.assertEqual(body2["before_id"], cursor)
        # Strictly older than the cursor — no overlap with page 1.
        self.assertTrue(all(pid < cursor for pid in page2))
        self.assertEqual(set(page1).intersection(page2), set())
        # next_before still advances.
        self.assertEqual(body2["next_before"], min(page2))

    def test_page_past_end_returns_empty_and_null_cursor(self):
        # A cursor at the oldest seeded id leaves nothing older to return.
        oldest = min(self.ids)
        r = self.c.get(
            f"/api/public/v1/feed?limit=10&before_id={oldest}",
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Nothing from our seeded source.
        self.assertEqual(self._feed_ids(body["feed"]), [])
        # And if no other test data shares this database we expect a fully
        # empty feed page; either way next_before must be None for the
        # caller-from-this-source's perspective when the page is empty.
        if not body["feed"]:
            self.assertIsNone(body["next_before"])

    def test_limit_is_hard_capped_at_100(self):
        r = self.c.get("/api/public/v1/feed?limit=10000", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 100)

    def test_invalid_before_id_rejected(self):
        r = self.c.get(
            "/api/public/v1/feed?before_id=-1",
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
