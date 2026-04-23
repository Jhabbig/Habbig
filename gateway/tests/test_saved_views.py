"""End-to-end tests for saved views — schema validator, DB CRUD, HTTP
routes, and the /v/{token} share surface.

Uses tests/_testdb.py to run everything against an in-memory SQLite
with all migrations (including 126) applied.
"""

from __future__ import annotations

import json
import os
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")
os.environ.setdefault("EMBED_SIGNING_SECRET", "test-signing-secret-1234567890abcd")

from tests import _testdb  # noqa: F401,E402
import db  # noqa: E402
import saved_views_db as views  # noqa: E402
import saved_views_schema as schema  # noqa: E402
import server  # noqa: E402
import saved_views_routes  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402


_CSRF = "testcsrfsavedviews"
_user_counter = {"n": 0}


def _anon_client() -> TestClient:
    return TestClient(server.app)


def _auth_client(user_id: int) -> TestClient:
    c = TestClient(server.app)
    c.cookies.set(server.COOKIE_NAME, db.create_session(user_id))
    c.cookies.set(server.CSRF_COOKIE_NAME, _CSRF)
    return c


def _csrf_headers() -> dict:
    return {"x-csrf-token": _CSRF, "Content-Type": "application/json"}


def _make_user(paid: bool = True) -> int:
    _user_counter["n"] += 1
    i = _user_counter["n"]
    email = f"views{i}@test.example"
    uid = db.create_user(email, "TestPass123!", username=f"views{i}")
    if paid:
        db.upsert_subscription(uid, "betyc", plan="pro_monthly", duration_days=30)
    return uid


# ── Schema validator tests ──────────────────────────────────────────────────


class TestValidator(unittest.TestCase):
    def test_unknown_scope_returns_empty(self):
        self.assertEqual(schema.validate_filters("bogus", {"x": 1}), {})

    def test_unknown_field_dropped(self):
        v = schema.validate_filters("markets", {"not_a_field": "x", "min_edge": 0.15})
        self.assertNotIn("not_a_field", v)
        self.assertAlmostEqual(v["min_edge"], 0.15)

    def test_malformed_value_dropped(self):
        v = schema.validate_filters("markets", {"min_edge": "not-a-number"})
        self.assertNotIn("min_edge", v)  # dropped, not 500

    def test_category_whitelist_enforced(self):
        v = schema.validate_filters("markets", {"categories": "politics,bogus,crypto"})
        self.assertEqual(sorted(v["categories"]), ["crypto", "politics"])

    def test_platform_whitelist(self):
        v = schema.validate_filters("markets", {"platform": "kalshi"})
        self.assertEqual(v["platform"], "kalshi")
        v2 = schema.validate_filters("markets", {"platform": "hacked"})
        self.assertNotIn("platform", v2)

    def test_duration_parsing(self):
        v = schema.validate_filters("markets", {"close_within": "7d"})
        self.assertEqual(v["close_within"], 7 * 86400)
        v = schema.validate_filters("markets", {"close_within": "48h"})
        self.assertEqual(v["close_within"], 48 * 3600)
        v = schema.validate_filters("markets", {"close_within": "30m"})
        self.assertEqual(v["close_within"], 30 * 60)
        v = schema.validate_filters("markets", {"close_within": "1800"})
        self.assertEqual(v["close_within"], 1800)

    def test_range_clamping(self):
        v = schema.validate_filters("markets", {"market_prob_range": [-0.5, 1.5]})
        self.assertEqual(v["market_prob_range"], [0.0, 1.0])

    def test_range_swapped(self):
        v = schema.validate_filters("markets", {"market_prob_range": [0.8, 0.2]})
        self.assertEqual(v["market_prob_range"], [0.2, 0.8])

    def test_bool_truthy(self):
        for truthy in ["1", "true", "yes", "on", True]:
            v = schema.validate_filters(
                "markets", {"has_insider_signal": truthy})
            self.assertTrue(v["has_insider_signal"])
        for falsy in ["0", "false", "", "no", False]:
            v = schema.validate_filters(
                "markets", {"has_insider_signal": falsy})
            self.assertFalse(v.get("has_insider_signal"))

    def test_handle_list_strips_at(self):
        v = schema.validate_filters(
            "feed", {"sources": "@fedwatcher, @zerohedge,   "})
        self.assertEqual(sorted(v["sources"]), ["fedwatcher", "zerohedge"])

    def test_handle_list_bounded(self):
        raw = ",".join(f"handle{i}" for i in range(1000))
        v = schema.validate_filters("feed", {"sources": raw})
        self.assertLessEqual(len(v["sources"]), 500)

    def test_round_trip_query_params(self):
        original = {
            "categories": ["politics", "crypto"],
            "close_within": 7 * 86400,
            "min_edge": 0.1,
            "has_insider_signal": True,
        }
        q = schema.filters_to_query(original)
        self.assertEqual(q["categories"], "politics,crypto")
        self.assertEqual(q["close_within"], str(7 * 86400))
        self.assertEqual(q["has_insider_signal"], "1")
        # Round-trip through validate_filters
        roundtripped = schema.validate_filters("markets", q)
        self.assertEqual(roundtripped["categories"], ["politics", "crypto"])
        self.assertTrue(roundtripped["has_insider_signal"])

    def test_build_where_empty(self):
        sql, params, joins, having = schema.build_where("markets", {})
        self.assertEqual(sql, "")
        self.assertEqual(params, [])
        self.assertEqual(joins, [])
        self.assertEqual(having, [])

    def test_build_where_categories(self):
        sql, params, _, _ = schema.build_where(
            "markets", {"categories": ["politics", "crypto"]})
        self.assertIn("category IN", sql)
        self.assertIn("?,?", sql)
        self.assertEqual(params, ["politics", "crypto"])

    def test_build_where_min_source_count_emits_having(self):
        sql, params, _, having = schema.build_where(
            "markets", {"min_source_count": 3})
        self.assertEqual(sql, "")  # no WHERE clause
        self.assertEqual(len(having), 1)
        self.assertIn("COUNT(DISTINCT", having[0])
        self.assertEqual(params, [3])

    def test_cache_key_stable(self):
        a = schema.cache_key("markets", {"a": 1, "b": 2}, user_id=5)
        b = schema.cache_key("markets", {"b": 2, "a": 1}, user_id=5)
        self.assertEqual(a, b)


# ── Share-token signing tests ───────────────────────────────────────────────


class TestShareToken(unittest.TestCase):
    def test_roundtrip(self):
        for view_id in [1, 42, 999999]:
            token = views.sign_view_token(view_id)
            self.assertEqual(views.verify_view_token(token), view_id)

    def test_rejects_invalid(self):
        self.assertIsNone(views.verify_view_token(""))
        self.assertIsNone(views.verify_view_token("garbage"))
        self.assertIsNone(views.verify_view_token("not:signed:properly"))

    def test_token_unique_per_view(self):
        tokens = {views.sign_view_token(i) for i in range(1, 21)}
        self.assertEqual(len(tokens), 20)


# ── DB CRUD tests ───────────────────────────────────────────────────────────


class TestCrud(unittest.TestCase):
    def test_create_and_get(self):
        uid = _make_user()
        row = views.create_view(uid, "markets", "Politics high-EV",
                                {"categories": ["politics"], "min_edge": 0.1})
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Politics high-EV")
        self.assertEqual(row["scope"], "markets")
        self.assertEqual(row["filters"]["categories"], ["politics"])
        fetched = views.get_user_view(uid, row["id"])
        self.assertEqual(fetched["id"], row["id"])

    def test_list_ordering(self):
        uid = _make_user()
        v1 = views.create_view(uid, "markets", "First", {})
        v2 = views.create_view(uid, "markets", "Second pinned", {}, is_pinned=True)
        v3 = views.create_view(uid, "markets", "Third default", {}, is_default=True)
        rows = views.list_user_views(uid, "markets")
        self.assertEqual(len(rows), 3)
        # Pinned comes first, then default, then created_at desc.
        self.assertEqual(rows[0]["id"], v2["id"])
        self.assertEqual(rows[1]["id"], v3["id"])

    def test_default_exclusive_per_scope(self):
        uid = _make_user()
        v1 = views.create_view(uid, "markets", "A", {}, is_default=True)
        v2 = views.create_view(uid, "markets", "B", {}, is_default=True)
        # Only one should be default.
        rows = views.list_user_views(uid, "markets")
        defaults = [r for r in rows if r["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], v2["id"])

    def test_different_scopes_can_both_default(self):
        uid = _make_user()
        views.create_view(uid, "markets", "M", {}, is_default=True)
        views.create_view(uid, "feed", "F", {}, is_default=True)
        self.assertIsNotNone(views.get_default(uid, "markets"))
        self.assertIsNotNone(views.get_default(uid, "feed"))

    def test_pin_filter(self):
        uid = _make_user()
        views.create_view(uid, "markets", "M-pin", {}, is_pinned=True)
        views.create_view(uid, "feed", "F-pin", {}, is_pinned=True)
        views.create_view(uid, "markets", "M-nopin", {})
        pinned = views.list_pinned(uid)
        names = {v["name"] for v in pinned}
        self.assertEqual(names, {"M-pin", "F-pin"})

    def test_update(self):
        uid = _make_user()
        v = views.create_view(uid, "markets", "Old", {"min_edge": 0.05})
        updated = views.update_view(uid, v["id"],
                                    name="New",
                                    filters={"min_edge": 0.2},
                                    is_pinned=True)
        self.assertEqual(updated["name"], "New")
        self.assertEqual(updated["filters"]["min_edge"], 0.2)
        self.assertTrue(updated["is_pinned"])

    def test_update_default_flips_others(self):
        uid = _make_user()
        a = views.create_view(uid, "markets", "A", {}, is_default=True)
        b = views.create_view(uid, "markets", "B", {})
        updated = views.update_view(uid, b["id"], is_default=True)
        self.assertTrue(updated["is_default"])
        # A should no longer be default.
        a_after = views.get_user_view(uid, a["id"])
        self.assertFalse(a_after["is_default"])

    def test_delete(self):
        uid = _make_user()
        v = views.create_view(uid, "markets", "X", {})
        self.assertTrue(views.delete_view(uid, v["id"]))
        self.assertIsNone(views.get_user_view(uid, v["id"]))

    def test_scope_acl(self):
        owner = _make_user()
        intruder = _make_user()
        v = views.create_view(owner, "markets", "mine", {})
        self.assertIsNone(views.get_user_view(intruder, v["id"]))
        self.assertFalse(views.delete_view(intruder, v["id"]))
        self.assertIsNone(views.update_view(intruder, v["id"], name="stolen"))

    def test_limit_per_scope(self):
        uid = _make_user()
        for i in range(views.MAX_VIEWS_PER_USER_PER_SCOPE):
            row = views.create_view(uid, "markets", f"v{i}", {})
            self.assertIsNotNone(row, f"expected create {i} to succeed")
        overflow = views.create_view(uid, "markets", "too many", {})
        self.assertIsNone(overflow)

    def test_clone_duplicates_filters(self):
        a = _make_user()
        b = _make_user()
        src = views.create_view(a, "markets", "Source",
                                {"categories": ["crypto"], "min_edge": 0.15},
                                is_default=True, is_pinned=True)
        clone = views.clone_view(b, src["id"])
        self.assertIsNotNone(clone)
        self.assertEqual(clone["filters"], src["filters"])
        # Clone is neutral — not default, not pinned.
        self.assertFalse(clone["is_default"])
        self.assertFalse(clone["is_pinned"])
        self.assertNotEqual(clone["id"], src["id"])
        self.assertEqual(clone["user_id"], b)


# ── HTTP route tests ────────────────────────────────────────────────────────


class TestHttpRoutes(unittest.TestCase):
    def test_create_requires_auth(self):
        c = _anon_client()
        c.cookies.set(server.CSRF_COOKIE_NAME, _CSRF)
        r = c.post("/api/saved-views",
                   json={"scope": "markets", "name": "x"},
                   headers=_csrf_headers())
        self.assertIn(r.status_code, (401, 403))

    def test_create_requires_subscription(self):
        uid = _make_user(paid=False)
        c = _auth_client(uid)
        r = c.post("/api/saved-views",
                   json={"scope": "markets", "name": "x"},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 403)

    def test_create_happy_path(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.post("/api/saved-views",
                   json={
                       "scope": "markets",
                       "name": "EV politics",
                       "filters": {"categories": "politics", "min_edge": 0.1},
                       "is_pinned": True,
                   },
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()["view"]
        self.assertEqual(body["name"], "EV politics")
        self.assertTrue(body["is_pinned"])
        self.assertEqual(body["filters"]["categories"], ["politics"])
        self.assertAlmostEqual(body["filters"]["min_edge"], 0.1)
        self.assertIn("share_token", body)

    def test_create_invalid_scope(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.post("/api/saved-views",
                   json={"scope": "badscope", "name": "x"},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 400)

    def test_create_missing_name(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.post("/api/saved-views",
                   json={"scope": "markets", "name": ""},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 400)

    def test_list_mine_filtered_by_scope(self):
        uid = _make_user()
        c = _auth_client(uid)
        c.post("/api/saved-views",
               json={"scope": "markets", "name": "m"},
               headers=_csrf_headers())
        c.post("/api/saved-views",
               json={"scope": "feed", "name": "f"},
               headers=_csrf_headers())
        r = c.get("/api/saved-views?scope=markets")
        self.assertEqual(r.status_code, 200)
        names = {v["name"] for v in r.json()["views"]}
        self.assertEqual(names, {"m"})

    def test_pinned_endpoint(self):
        uid = _make_user()
        c = _auth_client(uid)
        c.post("/api/saved-views",
               json={"scope": "markets", "name": "p", "is_pinned": True},
               headers=_csrf_headers())
        c.post("/api/saved-views",
               json={"scope": "markets", "name": "np"},
               headers=_csrf_headers())
        r = c.get("/api/saved-views/pinned")
        self.assertEqual(r.status_code, 200)
        names = {v["name"] for v in r.json()["views"]}
        self.assertEqual(names, {"p"})

    def test_default_endpoint(self):
        uid = _make_user()
        c = _auth_client(uid)
        c.post("/api/saved-views",
               json={"scope": "markets", "name": "d", "is_default": True},
               headers=_csrf_headers())
        r = c.get("/api/saved-views/default?scope=markets")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["view"])
        self.assertEqual(r.json()["view"]["name"], "d")
        r = c.get("/api/saved-views/default?scope=feed")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["view"])

    def test_update_partial(self):
        uid = _make_user()
        c = _auth_client(uid)
        created = c.post("/api/saved-views",
                         json={"scope": "markets", "name": "old"},
                         headers=_csrf_headers()).json()["view"]
        r = c.patch(f"/api/saved-views/{created['id']}",
                    json={"name": "renamed", "is_pinned": True},
                    headers=_csrf_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["view"]["name"], "renamed")
        self.assertTrue(r.json()["view"]["is_pinned"])

    def test_delete(self):
        uid = _make_user()
        c = _auth_client(uid)
        created = c.post("/api/saved-views",
                         json={"scope": "markets", "name": "bye"},
                         headers=_csrf_headers()).json()["view"]
        r = c.delete(f"/api/saved-views/{created['id']}",
                     headers=_csrf_headers())
        self.assertEqual(r.status_code, 200)
        r2 = c.get(f"/api/saved-views/{created['id']}")
        self.assertEqual(r2.status_code, 404)

    def test_cross_user_404(self):
        owner = _make_user()
        intruder = _make_user()
        view_id = views.create_view(owner, "markets", "a", {})["id"]
        c = _auth_client(intruder)
        self.assertEqual(c.get(f"/api/saved-views/{view_id}").status_code, 404)
        self.assertEqual(
            c.patch(f"/api/saved-views/{view_id}",
                    json={"name": "h"}, headers=_csrf_headers()).status_code, 404)
        self.assertEqual(
            c.delete(f"/api/saved-views/{view_id}", headers=_csrf_headers()).status_code, 404)

    def test_preview_endpoint_basic(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.post("/api/saved-views/preview",
                   json={"scope": "markets", "filters": {"categories": "politics"}},
                   headers=_csrf_headers())
        # Preview shouldn't 500 even if the markets table doesn't exist in
        # the test schema — it should degrade to count=0 via the error
        # branch.
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("count", body)
        self.assertIn("total", body)
        self.assertEqual(body["scope"], "markets")

    def test_preview_invalid_scope(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.post("/api/saved-views/preview",
                   json={"scope": "bogus", "filters": {}},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 400)

    def test_preview_drops_bad_filters_silently(self):
        uid = _make_user()
        c = _auth_client(uid)
        # Malformed filter values must NOT 500 — they should be silently
        # dropped and the preview count should still return.
        r = c.post("/api/saved-views/preview",
                   json={"scope": "markets", "filters": {
                       "min_edge": "not-a-number",
                       "close_within": "broken",
                       "categories": "bogus_category",
                       "nonexistent": "foo",
                   }},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 200)


# ── Share-link /v/{token} tests ─────────────────────────────────────────────


class TestShareFlow(unittest.TestCase):
    def test_share_token_redirects_to_scope(self):
        uid = _make_user()
        v = views.create_view(uid, "feed", "EV feed",
                              {"categories": ["crypto"], "min_edge": 0.1})
        c = _anon_client()
        r = c.get(f"/v/{v['share_token']}", follow_redirects=False)
        self.assertIn(r.status_code, (301, 302, 307))
        loc = r.headers.get("location", "")
        self.assertIn("/signal-search", loc)  # feed maps to signal-search
        self.assertIn("categories=crypto", loc)
        self.assertIn("view_id=", loc)

    def test_invalid_token_renders_error_page(self):
        c = _anon_client()
        r = c.get("/v/clearly-not-a-token")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Share link unavailable", r.text)

    def test_deleted_view_renders_error_page(self):
        uid = _make_user()
        v = views.create_view(uid, "markets", "gone", {})
        token = v["share_token"]
        views.delete_view(uid, v["id"])
        c = _anon_client()
        r = c.get(f"/v/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("deleted by its owner", r.text)

    def test_clone_endpoint(self):
        owner = _make_user()
        recipient = _make_user()
        v = views.create_view(owner, "markets", "source",
                              {"categories": ["politics"]})
        c = _auth_client(recipient)
        r = c.post(f"/api/saved-views/{v['id']}/clone",
                   json={"name": "My clone"},
                   headers=_csrf_headers())
        self.assertEqual(r.status_code, 201, r.text)
        cloned = r.json()["view"]
        self.assertEqual(cloned["user_id"], recipient)
        self.assertEqual(cloned["name"], "My clone")
        self.assertEqual(cloned["filters"]["categories"], ["politics"])


class TestSettingsPage(unittest.TestCase):
    def test_redirects_when_unauthenticated(self):
        c = _anon_client()
        r = c.get("/settings/saved-views", follow_redirects=False)
        # Dev bypass may return 200; otherwise 302 → /token. Both signal
        # "route registered, not 404'd".
        self.assertIn(r.status_code, (200, 302))

    def test_renders_for_authed(self):
        uid = _make_user()
        c = _auth_client(uid)
        r = c.get("/settings/saved-views")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Saved views", r.text)
        # Scope tabs must all be present in the static HTML.
        for scope in ("markets", "feed", "sources", "predictions"):
            self.assertIn(f'data-scope="{scope}"', r.text)


class TestFilterIntegrationPredictions(unittest.TestCase):
    """Extended /api/v1/predictions with saved-views filter schema."""

    @classmethod
    def setUpClass(cls):
        # Create an API key for auth, plus a handful of predictions across
        # categories / sources / timestamps so filter narrowing is testable.
        import time as _time
        cls.uid = _make_user()
        from api_v1 import create_api_key
        cls.raw_key, _ = create_api_key(cls.uid, name="test", tier="standard")

        now = int(_time.time())
        # Source credibility rows
        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, correct_predictions, categories_active, last_computed_at) "
                "VALUES (?, 0.82, 1, 30, 24, 2, ?)",
                ("alice", now),
            )
            c.execute(
                "INSERT OR IGNORE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, correct_predictions, categories_active, last_computed_at) "
                "VALUES (?, 0.52, 1, 20, 11, 1, ?)",
                ("bob", now),
            )
            # Predictions — mix of politics/crypto, mix of sources, mix of age.
            cls._pred_ids = []
            for (src, cat, age, resolved) in [
                ("alice", "politics", 3600, 0),
                ("alice", "politics", 120, 0),
                ("alice", "crypto",   3600, 1),
                ("bob",   "crypto",   3600, 0),
                ("bob",   "crypto",   3600, 1),
                ("bob",   "sports",   86400 * 40, 1),  # old, so "posted_within=24h" excludes
            ]:
                cur = c.execute(
                    "INSERT INTO predictions "
                    "(source_handle, market_id, category, direction, predicted_probability, content, extracted_at, resolved, resolved_correct) "
                    "VALUES (?, ?, ?, 'up', 0.6, 'x', ?, ?, NULL)",
                    (src, "mkt", cat, now - age, resolved),
                )
                cls._pred_ids.append(cur.lastrowid)

    def _get(self, query: str):
        c = TestClient(server.app)
        r = c.get(
            "/api/v1/predictions" + query,
            headers={"Authorization": f"Bearer {self.raw_key}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_legacy_params_still_work(self):
        body = self._get("?category=politics")
        cats = {p["category"] for p in body["predictions"]}
        # Legacy single-value path: only politics returned.
        self.assertEqual(cats, {"politics"})

    def test_saved_views_categories_list(self):
        body = self._get("?categories=politics,crypto")
        cats = {p["category"] for p in body["predictions"]}
        self.assertEqual(cats, {"politics", "crypto"})
        # Filters are echoed back so the client can cross-check.
        self.assertEqual(
            sorted(body["filters_applied"]["categories"]),
            ["crypto", "politics"],
        )

    def test_saved_views_overrides_legacy_when_both_present(self):
        # New categories param takes priority over legacy category.
        body = self._get("?categories=politics&category=sports")
        cats = {p["category"] for p in body["predictions"]}
        self.assertEqual(cats, {"politics"})

    def test_resolution_filter(self):
        body = self._get("?resolution=resolved")
        self.assertTrue(all(p["resolved"] for p in body["predictions"]))
        body = self._get("?resolution=pending")
        self.assertTrue(all(not p["resolved"] for p in body["predictions"]))

    def test_source_cred_range_narrows(self):
        body = self._get("?source_cred_range=0.8,1.0")
        # Only alice (cred=0.82) should qualify.
        handles = {p["source_handle"] for p in body["predictions"]}
        self.assertTrue(handles.issubset({"alice"}))

    def test_posted_within_excludes_old(self):
        body = self._get("?posted_within=24h")
        # bob's 40-day-old prediction must be filtered out.
        self.assertFalse(any(
            p["source_handle"] == "bob" and p["category"] == "sports"
            for p in body["predictions"]))

    def test_malformed_filter_does_not_500(self):
        body = self._get("?min_edge=not-a-number&close_within=busted")
        # The endpoint should still return 200 and drop the bad filters.
        self.assertIn("predictions", body)

    def test_bogus_filter_key_ignored(self):
        body = self._get("?this_is_not_a_field=whatever")
        # Should return results, not 500. filters_applied excludes unknown.
        self.assertNotIn("this_is_not_a_field", body["filters_applied"])


class TestFilterIntegrationSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import time as _time
        cls.uid = _make_user()
        from api_v1 import create_api_key
        cls.raw_key, _ = create_api_key(cls.uid, name="t", tier="standard")

        now = int(_time.time())
        with db.conn() as c:
            # Seed three sources at different credibilities + prediction counts.
            for (h, cred, preds) in [("high1", 0.90, 200),
                                     ("mid1",  0.55, 50),
                                     ("low1",  0.20, 5)]:
                c.execute(
                    "INSERT OR IGNORE INTO source_credibility "
                    "(source_handle, global_credibility, accuracy_unlocked, total_predictions, correct_predictions, categories_active, last_computed_at) "
                    "VALUES (?, ?, 1, ?, 0, 1, ?)",
                    (h, cred, preds, now),
                )

    def _get(self, query: str):
        c = TestClient(server.app)
        r = c.get(
            "/api/v1/sources" + query,
            headers={"Authorization": f"Bearer {self.raw_key}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_min_credibility(self):
        body = self._get("?min_credibility=0.8")
        handles = {s["handle"] for s in body["sources"]}
        # Only high1 (0.90) qualifies; mid1/low1 excluded.
        self.assertIn("high1", handles)
        self.assertNotIn("mid1", handles)
        self.assertNotIn("low1", handles)

    def test_min_predictions(self):
        body = self._get("?min_predictions=100")
        handles = {s["handle"] for s in body["sources"]}
        self.assertIn("high1", handles)
        self.assertNotIn("mid1", handles)

    def test_filters_echoed(self):
        body = self._get("?min_credibility=0.5&min_predictions=40")
        self.assertIn("filters_applied", body)
        self.assertAlmostEqual(body["filters_applied"]["min_credibility"], 0.5)
        self.assertEqual(body["filters_applied"]["min_predictions"], 40)


if __name__ == "__main__":
    unittest.main()
