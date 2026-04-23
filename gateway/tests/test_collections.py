"""Tests for the Collections feature (migrations 120-121).

Covers:
  - create / list / update / delete
  - visibility enforcement (private → 404 for non-owner,
    shared → signed-in only, public → anyone)
  - add / remove / reorder items
  - follow / unfollow
  - notification fires on add_item for followers
  - auto-collections are created, populated from source tables,
    and not editable
  - admin is_featured toggle
  - public ``/c/{handle}/{slug}`` page marks robots=index only for public
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
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


# ── Fixtures ────────────────────────────────────────────────────────────


_ctr = 0


def _mk(email_prefix: str, *, admin_level: int = 0) -> tuple[int, str]:
    global _ctr
    _ctr += 1
    email = f"{email_prefix}{_ctr}@test.com"
    username = f"{email_prefix}{_ctr}"
    uid = db.create_user(email, "TestPass123!", username=username, admin_level=admin_level)
    token = db.create_session(uid)
    return uid, token


def _auth(token: str) -> dict:
    # CSRF middleware requires a header + cookie pair on POST/PATCH/DELETE;
    # setting both here means every mutating test passes the same value.
    return {
        "Cookie": f"pm_gateway_session={token}; _csrf=t",
        "x-csrf-token": "t",
    }


def _clear():
    _conn.execute("DELETE FROM collection_items")
    _conn.execute("DELETE FROM collection_follows")
    _conn.execute("DELETE FROM collections")
    _conn.execute("DELETE FROM notifications")
    _conn.commit()


# ── Pure DB tests ───────────────────────────────────────────────────────


class TestCollectionsDb(unittest.TestCase):
    def setUp(self):
        _clear()

    def test_create_and_list(self):
        uid, _ = _mk("owner")
        cid = coll.create_collection(uid, "Fed meetings Q2",
                                     description="Rate calls",
                                     visibility="private")
        rows = coll.list_user_collections(uid, include_system=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], cid)
        self.assertEqual(rows[0]["slug"], "fed-meetings-q2")

    def test_private_visibility_hides_from_others(self):
        owner, _ = _mk("priv")
        stranger, _ = _mk("other")
        cid = coll.create_collection(owner, "Private board", visibility="private")
        with self.assertRaises(PermissionError):
            coll.get_collection(cid, viewer_user_id=stranger)

    def test_shared_visibility_allows_signed_in(self):
        owner, _ = _mk("share")
        stranger, _ = _mk("other")
        cid = coll.create_collection(owner, "Shared board", visibility="shared")
        # Signed-in user can see it
        row = coll.get_collection(cid, viewer_user_id=stranger)
        self.assertEqual(row["id"], cid)
        # Anon (viewer=None) cannot
        with self.assertRaises(PermissionError):
            coll.get_collection(cid, viewer_user_id=None)

    def test_public_visibility_allows_anon(self):
        owner, _ = _mk("pub")
        cid = coll.create_collection(owner, "Public board", visibility="public")
        row = coll.get_collection(cid, viewer_user_id=None)
        self.assertEqual(row["id"], cid)

    def test_add_and_reorder_items(self):
        owner, _ = _mk("items")
        cid = coll.create_collection(owner, "Test")
        a = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:a")
        b = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:b")
        c_id = coll.add_item(cid, owner_id=owner, item_type="source", item_ref="alice")

        # Original order: a, b, c
        items = coll.list_items(cid)
        self.assertEqual([it["item_ref"] for it in items], ["poly:a", "poly:b", "alice"])

        # Reorder to c, a, b
        coll.reorder_items(cid, owner_id=owner, ordering=[
            {"item_id": c_id, "position": 0},
            {"item_id": a, "position": 1},
            {"item_id": b, "position": 2},
        ])
        items = coll.list_items(cid)
        self.assertEqual([it["item_ref"] for it in items], ["alice", "poly:a", "poly:b"])

    def test_add_item_dedupes(self):
        owner, _ = _mk("dedupe")
        cid = coll.create_collection(owner, "Test")
        first = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:x")
        second = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:x")
        self.assertEqual(first, second)
        self.assertEqual(len(coll.list_items(cid)), 1)

    def test_non_owner_cannot_add(self):
        owner, _ = _mk("own")
        stranger, _ = _mk("str")
        cid = coll.create_collection(owner, "Owned")
        with self.assertRaises(PermissionError):
            coll.add_item(cid, owner_id=stranger,
                          item_type="market", item_ref="poly:x")

    def test_follow_private_collection_rejected(self):
        owner, _ = _mk("o")
        stranger, _ = _mk("s")
        cid = coll.create_collection(owner, "Private", visibility="private")
        with self.assertRaises(PermissionError):
            coll.follow_collection(stranger, cid)

    def test_follow_public_collection_increments_count(self):
        owner, _ = _mk("o")
        stranger, _ = _mk("s")
        cid = coll.create_collection(owner, "Public", visibility="public")
        coll.follow_collection(stranger, cid)
        row = coll.get_collection(cid, viewer_user_id=stranger)
        self.assertEqual(row["follower_count"], 1)
        self.assertTrue(coll.is_following(stranger, cid))
        # Unfollow drops the count
        coll.unfollow_collection(stranger, cid)
        row = coll.get_collection(cid, viewer_user_id=stranger)
        self.assertEqual(row["follower_count"], 0)

    def test_auto_collections_created_for_user(self):
        uid, _ = _mk("auto")
        ids = coll.ensure_system_collections(uid)
        self.assertIn("saved", ids)
        self.assertIn("watchlist", ids)
        # Idempotent — re-running returns the same ids
        ids2 = coll.ensure_system_collections(uid)
        self.assertEqual(ids, ids2)

    def test_auto_collection_not_deletable(self):
        uid, _ = _mk("auto2")
        ids = coll.ensure_system_collections(uid)
        with self.assertRaises(PermissionError):
            coll.delete_collection(ids["saved"], owner_id=uid)

    def test_auto_collection_not_addable(self):
        uid, _ = _mk("auto3")
        ids = coll.ensure_system_collections(uid)
        with self.assertRaises(PermissionError):
            coll.add_item(ids["saved"], owner_id=uid,
                          item_type="market", item_ref="poly:x")

    def test_saved_system_mirrors_saved_predictions(self):
        uid, _ = _mk("mirror")
        # Seed a prediction and save it.
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO predictions (source_handle, category, content, extracted_at) "
                "VALUES (?, 'finance', 'Rates hold', ?)",
                ("alice", int(time.time())),
            )
            pid = cur.lastrowid
            c.execute(
                "INSERT INTO saved_predictions (user_id, prediction_id, saved_at) "
                "VALUES (?, ?, ?)",
                (uid, pid, int(time.time())),
            )
        coll.ensure_system_collections(uid)
        coll.rebuild_system_collection_items(uid, "saved")
        ids = coll.ensure_system_collections(uid)
        items = coll.list_items(ids["saved"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_type"], "prediction")
        self.assertEqual(items[0]["item_ref"], str(pid))


# ── HTTP tests ──────────────────────────────────────────────────────────


class TestCollectionsHttp(unittest.TestCase):
    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_create_via_api(self):
        uid, token = _mk("http")
        r = client.post(
            "/api/collections", headers=_auth(token),
            json={"title": "2026 midterms", "visibility": "public"},
        )
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["title"], "2026 midterms")
        self.assertEqual(body["visibility"], "public")

    def test_create_requires_auth(self):
        # Send CSRF header+cookie so the middleware passes us through to
        # the route's own auth check — otherwise we'd be testing CSRF not auth.
        r = client.post(
            "/api/collections", json={"title": "x"},
            headers={"Cookie": "_csrf=t", "x-csrf-token": "t"},
        )
        self.assertEqual(r.status_code, 401)

    def test_private_get_returns_404_for_stranger(self):
        owner, t1 = _mk("p1")
        stranger, t2 = _mk("p2")
        cid = coll.create_collection(owner, "Private", visibility="private")
        r = client.get(f"/api/collections/{cid}", headers=_auth(t2))
        self.assertEqual(r.status_code, 404)
        # Owner can see
        r_owner = client.get(f"/api/collections/{cid}", headers=_auth(t1))
        self.assertEqual(r_owner.status_code, 200)

    def test_update_respects_ownership(self):
        owner, t1 = _mk("u1")
        stranger, t2 = _mk("u2")
        cid = coll.create_collection(owner, "Mine")
        r = client.patch(
            f"/api/collections/{cid}", headers=_auth(t2),
            json={"title": "Not yours"},
        )
        # Non-owner: either 403 or 404 is acceptable — both deny.
        self.assertIn(r.status_code, (403, 404))

    def test_reorder_api(self):
        owner, token = _mk("ro")
        cid = coll.create_collection(owner, "Reorder")
        a = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:a")
        b = coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:b")
        r = client.post(
            f"/api/collections/{cid}/items/reorder", headers=_auth(token),
            json=[{"item_id": b, "position": 0}, {"item_id": a, "position": 1}],
        )
        self.assertEqual(r.status_code, 200)
        items = coll.list_items(cid)
        self.assertEqual(items[0]["item_ref"], "poly:b")

    def test_public_page_seo_robots(self):
        owner, _ = _mk("seo")
        cid = coll.create_collection(owner, "SEO Board", visibility="public")
        coll.add_item(cid, owner_id=owner, item_type="market", item_ref="poly:a")
        with db.conn() as c:
            handle = c.execute("SELECT username FROM users WHERE id = ?",
                               (owner,)).fetchone()["username"]
        r = client.get(f"/c/{handle}/seo-board")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="robots" content="index,follow"', r.text)

    def test_shared_page_not_indexed(self):
        owner, _ = _mk("noindex")
        cid = coll.create_collection(owner, "Shared Board", visibility="shared")
        with db.conn() as c:
            handle = c.execute("SELECT username FROM users WHERE id = ?",
                               (owner,)).fetchone()["username"]
        # Shared boards need an authed viewer to even load the public URL.
        stranger, t = _mk("s3")
        r = client.get(f"/c/{handle}/shared-board", headers=_auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="robots" content="noindex,nofollow"', r.text)

    def test_private_public_url_404(self):
        owner, _ = _mk("p404")
        cid = coll.create_collection(owner, "Hidden", visibility="private")
        with db.conn() as c:
            handle = c.execute("SELECT username FROM users WHERE id = ?",
                               (owner,)).fetchone()["username"]
        r = client.get(f"/c/{handle}/hidden")
        self.assertEqual(r.status_code, 404)

    def test_admin_feature_toggle(self):
        admin, a_tok = _mk("admin", admin_level=1)
        owner, _ = _mk("user")
        cid = coll.create_collection(owner, "Featured me", visibility="public")

        r = client.post(
            f"/admin/api/collections/{cid}/feature", headers=_auth(a_tok),
            json={"is_featured": True},
        )
        self.assertEqual(r.status_code, 200)
        row = coll.get_collection(cid)
        self.assertTrue(row["is_featured"])

        # Now the /explore featured list must include it.
        r = client.get("/api/collections/explore")
        self.assertEqual(r.status_code, 200)
        featured_ids = {c["id"] for c in r.json()["featured"]}
        self.assertIn(cid, featured_ids)

    def test_non_admin_cannot_feature(self):
        non_admin, token = _mk("nonadmin")
        owner, _ = _mk("user2")
        cid = coll.create_collection(owner, "Board", visibility="public")
        r = client.post(
            f"/admin/api/collections/{cid}/feature", headers=_auth(token),
            json={"is_featured": True},
        )
        self.assertEqual(r.status_code, 403)


# ── Notification fan-out ────────────────────────────────────────────────


class TestFollowNotifications(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear()
        client.cookies.clear()

    async def test_add_item_notifies_followers(self):
        owner, t_owner = _mk("not_o")
        follower, _ = _mk("not_f")
        cid = coll.create_collection(owner, "Watched", visibility="public")
        coll.follow_collection(follower, cid)

        # Capture what create_notification receives rather than relying on
        # the full DB-backed notification stack to know "collection_update".
        seen: list[dict] = []

        async def _fake_create(**kwargs):
            seen.append(kwargs)
            return 1

        with patch("notifications.create_notification", new=_fake_create):
            r = client.post(
                f"/api/collections/{cid}/items", headers=_auth(t_owner),
                json={"item_type": "market", "item_ref": "poly:newitem"},
            )
        self.assertEqual(r.status_code, 201)
        # Give the fire-and-forget fan-out a chance to land.
        import asyncio as _a
        await _a.sleep(0.05)

        self.assertTrue(
            any(kw.get("user_id") == follower and
                kw.get("link_url") == f"/collections/{cid}"
                for kw in seen),
            f"expected a follower notification, got {seen}",
        )


class TestExtras(unittest.TestCase):
    """Coverage for the five follow-up additions: typeahead search,
    notification opt-out, share button presence, RSS feed, and the
    profile-page public-collections section."""

    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_search_returns_source_matches(self):
        # Seed a source credibility row so the typeahead has something to
        # match. The endpoint is authenticated — make one and query.
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, total_predictions, "
                " correct_predictions, categories_active, last_computed_at) "
                "VALUES ('test_seer', 0.8, 42, 30, 3, ?)",
                (int(time.time()),),
            )
        uid, tok = _mk("ta")
        r = client.get("/api/collections/search?q=test_seer&kind=source",
                       headers=_auth(tok))
        self.assertEqual(r.status_code, 200)
        results = r.json()["results"]
        self.assertTrue(any(x["item_ref"] == "test_seer" and x["item_type"] == "source"
                            for x in results))

    def test_search_rejects_short_query(self):
        _, tok = _mk("sh")
        r = client.get("/api/collections/search?q=a", headers=_auth(tok))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"], [])

    def test_search_requires_auth(self):
        r = client.get("/api/collections/search?q=foo")
        self.assertEqual(r.status_code, 401)

    def test_patch_follow_toggles_notifications(self):
        owner, _ = _mk("po")
        follower, tok = _mk("pf")
        cid = coll.create_collection(owner, "Pub", visibility="public")
        coll.follow_collection(follower, cid)

        r = client.patch(
            f"/api/collections/{cid}/follow", headers=_auth(tok),
            json={"notifications_on": False},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["notifications_on"])
        # followers list with only_notifiable=True should now exclude them
        self.assertEqual(coll.list_followers(cid, only_notifiable=True), [])

    def test_patch_follow_requires_existing_follow(self):
        owner, _ = _mk("pox")
        stranger, tok = _mk("pst")
        cid = coll.create_collection(owner, "Pub", visibility="public")
        r = client.patch(
            f"/api/collections/{cid}/follow", headers=_auth(tok),
            json={"notifications_on": False},
        )
        self.assertEqual(r.status_code, 404)

    def test_rss_feed_public_only(self):
        owner, _ = _mk("rss")
        cid_public = coll.create_collection(owner, "RSS board", visibility="public")
        cid_private = coll.create_collection(owner, "Private RSS", visibility="private")
        coll.add_item(cid_public, owner_id=owner,
                      item_type="source", item_ref="alice")
        with db.conn() as c:
            handle = c.execute(
                "SELECT username FROM users WHERE id = ?", (owner,),
            ).fetchone()["username"]

        r = client.get(f"/c/{handle}/rss-board.rss")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/rss+xml", r.headers.get("content-type", ""))
        self.assertIn("<rss", r.text)
        self.assertIn("<item>", r.text)

        # Private → 404 (no feed fingerprinting).
        r2 = client.get(f"/c/{handle}/private-rss.rss")
        self.assertEqual(r2.status_code, 404)

    def test_share_button_in_public_page(self):
        owner, _ = _mk("sh")
        cid = coll.create_collection(owner, "Share me", visibility="public")
        with db.conn() as c:
            handle = c.execute(
                "SELECT username FROM users WHERE id = ?", (owner,),
            ).fetchone()["username"]
        r = client.get(f"/c/{handle}/share-me")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="c-share-btn"', r.text)

    def test_profile_lists_public_collections(self):
        uid, tok = _mk("prof")
        coll.create_collection(uid, "Prof board", visibility="public")
        r = client.get("/profile", headers=_auth(tok))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Public collections", r.text)
        self.assertIn("Prof board", r.text)

    def test_profile_hides_section_for_user_with_no_public(self):
        uid, tok = _mk("quiet")
        coll.create_collection(uid, "Secret", visibility="private")
        r = client.get("/profile", headers=_auth(tok))
        self.assertEqual(r.status_code, 200)
        # Section is omitted entirely when the user has nothing public,
        # so the heading doesn't appear. A private board should not leak
        # into the profile surface.
        self.assertNotIn("Public collections", r.text)


if __name__ == "__main__":
    unittest.main()
