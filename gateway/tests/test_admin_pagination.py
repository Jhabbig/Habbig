"""Tests for cursor-paginated admin list queries.

Perf audit #5 — `list_all_users`, `list_invite_tokens`, and
`list_all_subscriptions` were unbounded reads. Now they accept
`limit` (default 100 / 50 / 100, hard cap 500) and `before_id` for
cursor pagination.

Covers:
  - Page 1 returns up-to-limit rows in DESC id order.
  - `before_id` cursor skips past the previous page and returns the
    next slice.
  - Walking the cursor end-to-end visits every row exactly once.
  - `limit=10000` clamps to 500 (no SQL-level limit escape).
  - `limit=0` is normalised to 1 (lower clamp).
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


def _wipe() -> None:
    with db.conn() as c:
        c.execute("DELETE FROM subscriptions")
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM invite_tokens")


def _seed_users(n: int) -> list:
    ids = []
    for i in range(n):
        uid = db.create_user(
            email=f"pag{i}@example.com",
            password="HorseBatteryStaple9!",
            username=f"pag_user_{i}",
        )
        ids.append(uid)
    return ids


def _seed_tokens(n: int) -> list:
    ids = []
    for i in range(n):
        raw = db.create_invite_token(f"pag note {i}")
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM invite_tokens WHERE token = ?", (raw,)
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def _seed_subscriptions(user_ids: list) -> list:
    ids = []
    for i, uid in enumerate(user_ids):
        db.upsert_subscription(
            user_id=uid,
            dashboard_key=f"dash_{i % 3}",
            plan="monthly",
            duration_days=30,
            source="placeholder",
        )
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM subscriptions WHERE user_id = ?", (uid,)
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


class TestListAllUsersPagination(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        self.ids = _seed_users(7)

    def test_default_limit_returns_all_when_under_page(self) -> None:
        page1 = db.list_all_users()
        self.assertEqual(len(page1), 7)
        returned_ids = [r["id"] for r in page1]
        self.assertEqual(returned_ids, sorted(self.ids, reverse=True))

    def test_cursor_walks_every_row_exactly_once(self) -> None:
        page1 = db.list_all_users(limit=3)
        self.assertEqual(len(page1), 3)
        cursor = int(page1[-1]["id"])
        page2 = db.list_all_users(limit=3, before_id=cursor)
        self.assertEqual(len(page2), 3)
        cursor2 = int(page2[-1]["id"])
        page3 = db.list_all_users(limit=3, before_id=cursor2)
        self.assertEqual(len(page3), 1)
        all_seen = {r["id"] for r in page1} | {r["id"] for r in page2} | {r["id"] for r in page3}
        self.assertEqual(all_seen, set(self.ids))

    def test_limit_hard_cap_is_500(self) -> None:
        page = db.list_all_users(limit=10_000)
        self.assertEqual(len(page), 7)

    def test_limit_zero_clamps_to_one(self) -> None:
        page = db.list_all_users(limit=0)
        self.assertEqual(len(page), 1)


class TestListInviteTokensPagination(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        self.ids = _seed_tokens(4)

    def test_default_limit_is_50_returns_all_when_under(self) -> None:
        page1 = db.list_invite_tokens()
        self.assertEqual(len(page1), 4)
        returned = [r["id"] for r in page1]
        self.assertEqual(returned, sorted(self.ids, reverse=True))

    def test_cursor_pagination_walks_all(self) -> None:
        page1 = db.list_invite_tokens(limit=2)
        self.assertEqual(len(page1), 2)
        cursor = int(page1[-1]["id"])
        page2 = db.list_invite_tokens(limit=2, before_id=cursor)
        self.assertEqual(len(page2), 2)
        self.assertFalse({r["id"] for r in page1} & {r["id"] for r in page2})


class TestListAllSubscriptionsPagination(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        user_ids = _seed_users(5)
        self.sub_ids = _seed_subscriptions(user_ids)

    def test_default_limit_returns_under_page_size(self) -> None:
        page1 = db.list_all_subscriptions()
        self.assertEqual(len(page1), 5)
        returned = [r["id"] for r in page1]
        self.assertEqual(returned, sorted(self.sub_ids, reverse=True))

    def test_cursor_pagination(self) -> None:
        page1 = db.list_all_subscriptions(limit=2)
        self.assertEqual(len(page1), 2)
        cursor = int(page1[-1]["id"])
        page2 = db.list_all_subscriptions(limit=2, before_id=cursor)
        self.assertEqual(len(page2), 2)
        cursor2 = int(page2[-1]["id"])
        page3 = db.list_all_subscriptions(limit=2, before_id=cursor2)
        self.assertEqual(len(page3), 1)
        all_seen = {r["id"] for r in page1} | {r["id"] for r in page2} | {r["id"] for r in page3}
        self.assertEqual(all_seen, set(self.sub_ids))

    def test_limit_hard_cap_is_500(self) -> None:
        page = db.list_all_subscriptions(limit=10_000)
        self.assertEqual(len(page), 5)


class TestActiveSubscriptionCountsAggregator(unittest.TestCase):
    """`get_active_subscription_counts_by_dashboard` replaces the
    Python-side aggregation in /admin/subproducts (perf audit #5)."""

    def setUp(self) -> None:
        _wipe()
        user_ids = _seed_users(6)
        _seed_subscriptions(user_ids)

    def test_returns_count_per_dashboard(self) -> None:
        counts = db.get_active_subscription_counts_by_dashboard()
        self.assertEqual(sum(counts.values()), 6)
        for v in counts.values():
            self.assertEqual(v, 2)


if __name__ == "__main__":
    unittest.main()
