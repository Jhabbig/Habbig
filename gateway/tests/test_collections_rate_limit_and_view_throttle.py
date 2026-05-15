"""Regression tests for the collections rate-limit + view-bump throttle.

AUDIT (MED) — see gateway/collections_routes.py (rate-limit decorator
on api_follow / api_unfollow) and gateway/queries/collections.py
(per-(viewer, collection) bump throttle).

Asserted behaviour:

  1. The 31st follow/unfollow toggle inside a 60 s window from one user
     returns 429.
  2. A signed-in viewer reloading the same public collection inside the
     ten-minute throttle window does NOT bump view_count twice.

Both tests use the shared in-memory DB so they run alongside the rest
of test_collections.py without colliding on schema setup.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
# Give a high global cap so the inner-decorator's 30/min check is the
# only thing the test exercises.
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

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
from queries import collections as coll  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


_ctr = 0


def _mk(email_prefix: str) -> tuple[int, str]:
    global _ctr
    _ctr += 1
    email = f"{email_prefix}{_ctr}@t.com"
    username = f"{email_prefix}{_ctr}"
    uid = db.create_user(email, "TestPass123!", username=username)
    token = db.create_session(uid)
    return uid, token


def _auth(token: str) -> dict:
    return {
        "Cookie": f"pm_gateway_session={token}; _csrf=t",
        "x-csrf-token": "t",
    }


def _clear():
    _conn.execute("DELETE FROM collection_items")
    _conn.execute("DELETE FROM collection_follows")
    _conn.execute("DELETE FROM collections")
    _conn.commit()


class TestFollowRateLimit(unittest.TestCase):
    """30 follow/unfollow actions per user per minute is the cap; the
    31st must 429.

    We exercise the limit by toggling follow/unfollow on the same public
    board — both verbs share one bucket so the attacker can't dodge by
    alternating them."""

    def setUp(self):
        _clear()
        client.cookies.clear()
        # Reset the limiter's in-memory state so previous test runs in
        # this process don't poison the bucket count.
        try:
            from security.rate_limiter import limiter
            limiter._windows.clear()
        except Exception:
            pass

    def test_follow_unfollow_thrash_hits_429(self):
        owner, _ = _mk("rl_o")
        attacker, token = _mk("rl_a")
        cid = coll.create_collection(owner, "Spam target", visibility="public")

        # 30 alternating actions land. The 31st (or sooner if some hit
        # the bucket as duplicates) returns 429.
        statuses: list[int] = []
        for i in range(35):
            if i % 2 == 0:
                r = client.post(
                    f"/api/collections/{cid}/follow", headers=_auth(token),
                )
            else:
                r = client.delete(
                    f"/api/collections/{cid}/follow", headers=_auth(token),
                )
            statuses.append(r.status_code)
            if r.status_code == 429:
                break

        self.assertIn(
            429, statuses,
            f"expected the limiter to fire within 35 hits; got {statuses}",
        )


class TestViewBumpThrottle(unittest.TestCase):
    """Same viewer reloading a collection inside the throttle window must
    NOT double-count view_count.

    Anonymous viewers still bump on every hit (they have no stable id
    to dedup on)."""

    def setUp(self):
        _clear()
        client.cookies.clear()
        # Clear the process-local bump cache so a previous test class
        # didn't already record this (viewer, collection) pair.
        try:
            coll._VIEW_BUMP_CACHE.clear()
        except Exception:
            pass

    def test_repeated_viewer_does_not_double_bump(self):
        owner, _ = _mk("vt_o")
        viewer, _ = _mk("vt_v")
        cid = coll.create_collection(owner, "Public board", visibility="public")

        before = coll.get_collection(cid, viewer_user_id=owner)["view_count"]
        # First view from a non-owner triggers one bump.
        coll.get_collection(cid, viewer_user_id=viewer, bump_views=True)
        after_first = coll.get_collection(cid, viewer_user_id=owner)["view_count"]
        self.assertEqual(after_first, before + 1)

        # Second view from the same viewer inside the window: no bump.
        coll.get_collection(cid, viewer_user_id=viewer, bump_views=True)
        coll.get_collection(cid, viewer_user_id=viewer, bump_views=True)
        after_repeats = coll.get_collection(cid, viewer_user_id=owner)["view_count"]
        self.assertEqual(after_repeats, after_first)

    def test_owner_view_does_not_bump(self):
        """Owners never count toward their own view_count — the throttle
        layer must not undo this invariant."""
        owner, _ = _mk("vt_self")
        cid = coll.create_collection(owner, "Self board", visibility="public")
        before = coll.get_collection(cid, viewer_user_id=owner)["view_count"]
        coll.get_collection(cid, viewer_user_id=owner, bump_views=True)
        after = coll.get_collection(cid, viewer_user_id=owner)["view_count"]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
