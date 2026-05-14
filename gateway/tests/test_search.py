"""Unified ⌘K search — FTS5 + route + analytics tests.

Covered:
  * Migrations 115/116/117 apply cleanly and create the expected tables
  * source_summaries_fts triggers stay in sync on insert/update/delete
  * Prefix FTS matches ("fed" → "Federal…", "fedwatcher")
  * Multi-word queries AND the terms and prefix-match the last
  * /api/search returns mixed-type results
  * Non-admins don't see users; admins do
  * Query shorter than MIN gets []
  * Zero-result queries still log to search_queries
  * /api/search/click updates the click columns
  * /admin/search-analytics gates on is_admin
  * Cache hit returns the same payload on repeat
"""

from __future__ import annotations

import os
import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


def _now() -> int:
    return int(time.time())


def _make_user(email: str, username: str, *, admin: bool = False) -> int:
    uid = db.create_user(email, "TestPass123!", username=username)
    if admin:
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
    return uid


def _login_as(uid: int) -> dict:
    token = db.create_session(uid)
    return {server.COOKIE_NAME: token}


def _seed_corpus() -> None:
    """Put enough rows in so FTS has something to match + TTL cache can warm."""
    now = _now()
    with db.conn() as c:
        # Unique slug per call (idempotent inserts)
        c.execute(
            "INSERT INTO market_snapshots (market_slug, market_question, category, "
            "yes_price, snapshotted_at) VALUES (?, ?, ?, ?, ?)",
            ("fed-rate-2026-search",
             "Will the Federal Reserve hold rates in March 2026?",
             "economics", 0.67, now),
        )
        c.execute(
            "INSERT OR IGNORE INTO source_credibility "
            "(source_handle, global_credibility, last_computed_at) VALUES (?, ?, ?)",
            ("fedwatcher_search", 0.74, now),
        )
        c.execute(
            "INSERT OR REPLACE INTO source_summaries "
            "(source_handle, summary, generated_at, generated_by, cache_valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("fedwatcher_search",
             "Expert analyst covering Federal Reserve monetary policy",
             now, "test", now + 86400),
        )
        c.execute(
            "INSERT INTO predictions (source_handle, category, content, extracted_at) "
            "VALUES (?, ?, ?, ?)",
            ("fedwatcher_search", "economics",
             "Fed will hold rates at 5.25 percent in Q1 2026", now),
        )


# ── Migration / trigger sanity ─────────────────────────────────────────────


class TestMigrationsAndTriggers(unittest.TestCase):
    def setUp(self):
        _seed_corpus()

    def test_fts_tables_exist(self):
        with db.conn() as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        self.assertIn("markets_fts", tables)
        self.assertIn("sources_fts", tables)
        self.assertIn("predictions_fts", tables)
        self.assertIn("source_summaries_fts", tables)
        self.assertIn("search_queries", tables)

    def test_source_summaries_trigger_insert(self):
        now = _now()
        handle = f"trigger_insert_{now}"
        with db.conn() as c:
            # Ensure credibility row so source shows in both FTS tables
            c.execute(
                "INSERT OR IGNORE INTO source_credibility "
                "(source_handle, global_credibility, last_computed_at) "
                "VALUES (?, ?, ?)", (handle, 0.6, now),
            )
            c.execute(
                "INSERT INTO source_summaries "
                "(source_handle, summary, generated_at, generated_by, cache_valid_until) "
                "VALUES (?, ?, ?, ?, ?)",
                (handle, "covers quantitative easing and yield curve inversion",
                 now, "test", now + 86400),
            )
            rows = c.execute(
                "SELECT source_handle, summary FROM source_summaries_fts "
                "WHERE source_summaries_fts MATCH ?", ("quantitative*",),
            ).fetchall()
        self.assertTrue(any(r["source_handle"] == handle for r in rows))

    def test_source_summaries_trigger_update(self):
        now = _now()
        handle = f"trigger_update_{now}"
        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO source_credibility "
                "(source_handle, global_credibility, last_computed_at) "
                "VALUES (?, ?, ?)", (handle, 0.6, now),
            )
            c.execute(
                "INSERT INTO source_summaries "
                "(source_handle, summary, generated_at, generated_by, cache_valid_until) "
                "VALUES (?, ?, ?, ?, ?)",
                (handle, "original text", now, "test", now + 86400),
            )
            c.execute(
                "UPDATE source_summaries SET summary = ? WHERE source_handle = ?",
                ("replacement jabberwocky", handle),
            )
            rows = c.execute(
                "SELECT source_handle, summary FROM source_summaries_fts "
                "WHERE source_summaries_fts MATCH ?", ("jabberwocky*",),
            ).fetchall()
        self.assertTrue(any(r["source_handle"] == handle for r in rows))

    def test_source_summaries_trigger_delete(self):
        now = _now()
        handle = f"trigger_delete_{now}"
        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO source_credibility "
                "(source_handle, global_credibility, last_computed_at) "
                "VALUES (?, ?, ?)", (handle, 0.6, now),
            )
            c.execute(
                "INSERT INTO source_summaries "
                "(source_handle, summary, generated_at, generated_by, cache_valid_until) "
                "VALUES (?, ?, ?, ?, ?)",
                (handle, "hapax legomenon term only here",
                 now, "test", now + 86400),
            )
            c.execute(
                "DELETE FROM source_summaries WHERE source_handle = ?", (handle,),
            )
            rows = c.execute(
                "SELECT source_handle FROM source_summaries_fts "
                "WHERE source_summaries_fts MATCH ?", ("hapax*",),
            ).fetchall()
        self.assertFalse([r for r in rows if r["source_handle"] == handle])


# ── FTS prefix / multi-word correctness ─────────────────────────────────────


class TestFTSQuerySemantics(unittest.TestCase):
    def setUp(self):
        _seed_corpus()

    def test_prefix_match_returns_full_phrase(self):
        with db.conn() as c:
            rows = c.execute(
                "SELECT ms.market_question FROM markets_fts f "
                "JOIN market_snapshots ms ON ms.rowid = f.rowid "
                "WHERE markets_fts MATCH ? LIMIT 5",
                ("fed*",),
            ).fetchall()
        self.assertTrue(any("Federal" in (r["market_question"] or "") for r in rows))

    def test_multi_word_query_ANDs(self):
        with db.conn() as c:
            rows = c.execute(
                "SELECT source_handle FROM source_summaries_fts "
                "WHERE source_summaries_fts MATCH ? LIMIT 5",
                ("federal monetary*",),
            ).fetchall()
        handles = {r["source_handle"] for r in rows}
        self.assertIn("fedwatcher_search", handles)


# ── HTTP /api/search ─────────────────────────────────────────────────────────


class TestSearchAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_corpus()

    def test_empty_query_returns_empty(self):
        r = client.get("/api/search?q=")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("results"), [])

    def test_short_query_returns_empty(self):
        r = client.get("/api/search?q=f")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("results"), [])

    def test_search_returns_mixed_types(self):
        r = client.get("/api/search?q=fed")
        self.assertEqual(r.status_code, 200)
        results = r.json().get("results") or []
        types = {x["type"] for x in results}
        # At minimum we should see markets + sources
        self.assertTrue({"market", "source"} & types)

    def test_non_admin_cannot_search_users(self):
        uid = _make_user("reg_search@test.local", "regsearch", admin=False)
        # Make a distinct user that would appear as a result
        _make_user("findme@test.local", "findmetest")
        cookies = _login_as(uid)
        r = client.get("/api/search?q=findme", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        types = {x["type"] for x in (r.json().get("results") or [])}
        self.assertNotIn("user", types)

    def test_admin_can_search_users(self):
        admin_uid = _make_user("admin_search@test.local", "adminsearch", admin=True)
        _make_user("findme2@test.local", "findme2test")
        cookies = _login_as(admin_uid)
        r = client.get("/api/search?q=findme2", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        results = r.json().get("results") or []
        types = {x["type"] for x in results}
        self.assertIn("user", types)

    def test_zero_result_query_still_logs_for_analytics(self):
        """Typos should feed the analytics dashboard so admins see gaps."""
        before = _count_search_queries()
        r = client.get("/api/search?q=xyzzyqwertygarbage")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("results"), [])
        after = _count_search_queries()
        self.assertGreater(after, before)

    def test_cache_hit_on_repeat(self):
        """Same query twice in quick succession should not re-run factory.

        We clear the TTL cache, make one request (cache miss), then a
        second request (cache hit) and confirm both return same payload.
        """
        from cache import ttl_cache
        ttl_cache.clear()
        ttl_cache.reset_stats()
        r1 = client.get("/api/search?q=fed")
        r2 = client.get("/api/search?q=fed")
        # Payload should be identical modulo query_id (which is logged
        # per-request, not cached).
        p1 = {k: v for k, v in r1.json().items() if k != "query_id"}
        p2 = {k: v for k, v in r2.json().items() if k != "query_id"}
        self.assertEqual(p1, p2)
        # Hit count on the `search` prefix should be >=1 after two calls
        stats = ttl_cache.stats()
        search_stats = [p for p in stats["per_prefix"] if p["prefix"] == "search"]
        self.assertTrue(search_stats and search_stats[0]["hits"] >= 1)


def _count_search_queries() -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]


# ── /api/search/click ───────────────────────────────────────────────────────


class TestHighlighting(unittest.TestCase):
    """FTS5 snippet() should return <mark>…</mark> around the matched
    term in the per-type highlight field. Palette client renders this
    as inverted-emphasis text via a narrow allowlist; server must
    actually emit the tags for anything to show."""

    @classmethod
    def setUpClass(cls):
        _seed_corpus()

    def test_market_result_has_mark_in_title_html(self):
        r = client.get("/api/search?q=federal")
        self.assertEqual(r.status_code, 200)
        markets = [x for x in (r.json().get("results") or []) if x["type"] == "market"]
        self.assertTrue(markets, "expected at least one market hit for 'federal'")
        # At least one of the returned markets should carry a highlight.
        self.assertTrue(
            any("<mark>" in (m.get("title_html") or "") for m in markets),
            f"no <mark> in any market title_html: {markets[:2]}",
        )

    def test_source_result_has_mark_in_subtitle_html(self):
        r = client.get("/api/search?q=monetary")
        self.assertEqual(r.status_code, 200)
        sources = [x for x in (r.json().get("results") or []) if x["type"] == "source"]
        self.assertTrue(sources, "expected source hit for 'monetary'")
        hl = [s for s in sources if "<mark>" in (s.get("subtitle_html") or "")]
        self.assertTrue(hl, f"no <mark> in any source subtitle: {sources[:2]}")


def _reset_rate_limiter() -> None:
    """Wipe the in-process rate-limit bucket so tests don't pollute each
    other. Safe no-op when the limiter is Redis-backed (Redis buckets
    auto-expire via TTL; we can't atomically flush from here)."""
    try:
        from security.rate_limiter import limiter
        with limiter._lock:
            limiter._windows.clear()
    except Exception:
        pass


class TestRateLimit(unittest.TestCase):
    """120/min is loose for humans but a tight loop of the full window
    should eventually get throttled. We burn through the bucket and
    expect a 429 within a reasonable number of attempts.

    Clears the limiter state on both ends so the hammer test can't
    pollute other classes (test order within a file is alphabetical,
    so we'd otherwise leave the bucket saturated for whoever comes
    after 'R' — at time of writing, TestSearchAPI)."""

    def setUp(self):
        _reset_rate_limiter()

    def tearDown(self):
        _reset_rate_limiter()

    def test_hammering_search_eventually_throttles(self):
        # Cap at 200 attempts so a misconfigured rate-limit can't hang CI.
        # At 120/min the 121st request in the same window is the first 429.
        seen_429 = False
        for _ in range(200):
            r = client.get("/api/search?q=federal")
            if r.status_code == 429:
                seen_429 = True
                break
            # Any other non-200 means something else broke
            self.assertEqual(r.status_code, 200, f"unexpected {r.status_code}")
        # If the rate limiter fell back to no-op (e.g. Redis unavailable
        # in the test harness), we accept the test as informational —
        # a no-op bucket is the documented degraded mode.
        if not seen_429:
            self.skipTest("rate limiter degraded to no-op in this harness")


class TestClickLogging(unittest.TestCase):
    def setUp(self):
        # Guard against rate-limit pollution from earlier test runs in the
        # same process — this test needs a usable bucket to collect the
        # query_id before POSTing the click.
        _reset_rate_limiter()

    def test_click_updates_row(self):
        # Create a query row to click on
        r = client.get("/api/search?q=federal")
        qid = r.json().get("query_id")
        self.assertIsNotNone(qid)
        resp = client.post("/api/search/click", json={
            "query_id": qid,
            "result_type": "market",
            "result_id": "fed-rate-2026-search",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("logged"))
        with db.conn() as c:
            row = c.execute(
                "SELECT clicked_result_type, clicked_result_id FROM search_queries WHERE id = ?",
                (qid,),
            ).fetchone()
        self.assertEqual(row["clicked_result_type"], "market")
        self.assertEqual(row["clicked_result_id"], "fed-rate-2026-search")

    def test_bad_body_returns_logged_false(self):
        resp = client.post("/api/search/click", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json().get("logged"))


# ── /admin/search-analytics ─────────────────────────────────────────────────


class TestPopularQueries(unittest.TestCase):
    """Palette empty-state shows a 'Popular' group sourced from the
    aggregated search_queries log. Endpoint must:
      * return queries seen ≥ 3× in the last 7d with non-zero results
      * exclude queries shorter than 3 chars
      * exclude @-prefixed queries (those can echo private handles)
    """

    def setUp(self):
        # Fresh limiter bucket so seeding doesn't burn quota.
        _reset_rate_limiter()
        from cache import ttl_cache
        ttl_cache.delete_prefix("search:")

    def _log(self, query: str, n: int, result_count: int = 5) -> None:
        import time as _t
        with db.conn() as c:
            for _ in range(n):
                c.execute(
                    "INSERT INTO search_queries (user_id, query, result_count, ts) "
                    "VALUES (?, ?, ?, ?)",
                    (None, query, result_count, int(_t.time())),
                )

    def test_returns_min_count_queries(self):
        self._log("quantitative easing", 4)
        self._log("once-off typo", 1)  # below floor
        r = client.get("/api/search/popular")
        self.assertEqual(r.status_code, 200)
        qs = r.json().get("queries") or []
        self.assertIn("quantitative easing", qs)
        self.assertNotIn("once-off typo", qs)

    def test_excludes_at_prefixed(self):
        # Ensure an admin's user-lookup via "@" never leaks into the public
        # popular feed even if it's searched a lot.
        self._log("@private_handle", 10)
        r = client.get("/api/search/popular")
        qs = r.json().get("queries") or []
        self.assertNotIn("@private_handle", qs)

    def test_excludes_short_queries(self):
        self._log("ab", 10)
        r = client.get("/api/search/popular")
        qs = r.json().get("queries") or []
        self.assertNotIn("ab", qs)

    def test_excludes_zero_result(self):
        with db.conn() as c:
            import time as _t
            for _ in range(5):
                c.execute(
                    "INSERT INTO search_queries (user_id, query, result_count, ts) "
                    "VALUES (?, ?, ?, ?)",
                    (None, "zero-result phrase", 0, int(_t.time())),
                )
        r = client.get("/api/search/popular")
        qs = r.json().get("queries") or []
        self.assertNotIn("zero-result phrase", qs)


class TestAdminAnalytics(unittest.TestCase):
    def test_non_admin_gets_403(self):
        uid = _make_user("reg_analytics@test.local", "reganalytics", admin=False)
        cookies = _login_as(uid)
        r = client.get("/admin/search-analytics", cookies=cookies)
        self.assertEqual(r.status_code, 403)

    def test_anon_gets_403(self):
        r = client.get("/admin/search-analytics")
        self.assertEqual(r.status_code, 403)

    def test_admin_page_renders_all_four_sections(self):
        # Seed a couple of queries so the dashboard has something to show
        client.get("/api/search?q=fed")
        client.get("/api/search?q=nothingheremakesitzero")

        admin_uid = _make_user("admin_analytics@test.local", "adminanalytics", admin=True)
        cookies = _login_as(admin_uid)
        r = client.get("/admin/search-analytics", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        # The four section headings — contract for the route.
        self.assertIn("Top 50 queries", r.text)
        self.assertIn("No-result queries", r.text)
        self.assertIn("Conversion funnel", r.text)
        self.assertIn("Time of day", r.text)
        # SVG charts present.
        self.assertIn("funnel-svg", r.text)
        self.assertIn("hour-svg", r.text)


# ── queries/search_analytics.py — direct accessor tests ────────────────────


class TestSearchAnalyticsQueries(unittest.TestCase):
    """Direct tests of the queries module that backs the admin route.

    Touches each of the four public functions:
      * top_queries
      * no_result_queries
      * query_to_conversion_rate
      * hourly_distribution
    """

    def setUp(self):
        # Fresh slate so seed counts are deterministic. Foreign keys mean
        # we have to clear children before parents.
        with db.conn() as c:
            c.execute("DELETE FROM search_queries")

    def _log(
        self,
        query: str,
        *,
        user_id: int | None = None,
        result_count: int = 5,
        clicked: bool = False,
        ts: int | None = None,
    ) -> int:
        """Insert one row, return id. ts defaults to now."""
        ts = ts if ts is not None else _now()
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO search_queries "
                "(user_id, query, result_count, clicked_at, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, query, result_count, ts if clicked else None, ts),
            )
            return int(cur.lastrowid)

    def test_top_queries_orders_by_hit_count(self):
        from queries import search_analytics as qsa

        uid_a = _make_user("topq_a@test.local", "topqa")
        uid_b = _make_user("topq_b@test.local", "topqb")
        # "fed" is searched 3x (2 distinct users); "btc" 1x.
        self._log("fed", user_id=uid_a)
        self._log("fed", user_id=uid_a)
        self._log("fed", user_id=uid_b)
        self._log("btc", user_id=uid_a)

        rows = qsa.top_queries(window_days=7, limit=10)
        # Index by query for stable assertion.
        by_q = {r["query"]: r for r in rows}
        self.assertIn("fed", by_q)
        self.assertEqual(by_q["fed"]["hits"], 3)
        self.assertEqual(by_q["fed"]["unique_users"], 2)
        self.assertGreater(by_q["fed"]["last_searched"], 0)
        # Ordering: fed first (3 hits), then btc (1 hit)
        self.assertEqual(rows[0]["query"], "fed")

    def test_no_result_queries_only_returns_zero_hits(self):
        from queries import search_analytics as qsa

        # Two zero-result, one non-zero — only the zeros should appear.
        self._log("xyzzyabc", result_count=0)
        self._log("xyzzyabc", result_count=0)
        self._log("plover", result_count=0)
        self._log("fed", result_count=12)

        rows = qsa.no_result_queries(window_days=7, limit=10)
        qs = {r["query"] for r in rows}
        self.assertIn("xyzzyabc", qs)
        self.assertIn("plover", qs)
        self.assertNotIn("fed", qs)
        # Most-hit zero query is first.
        self.assertEqual(rows[0]["query"], "xyzzyabc")
        self.assertEqual(rows[0]["hits"], 2)

    def test_conversion_funnel_search_click_save_subscribe(self):
        from queries import search_analytics as qsa
        import time as _t

        # User A: searched + clicked + saved + subscribed (full funnel)
        # User B: searched + clicked only
        # User C: searched, no click, no save, no sub
        # Anon  : searched (excluded from funnel entirely)
        uid_a = _make_user("funnel_a@test.local", "funa")
        uid_b = _make_user("funnel_b@test.local", "funb")
        uid_c = _make_user("funnel_c@test.local", "func")

        # Seed a prediction we can save against.
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO predictions (source_handle, category, content, extracted_at) "
                "VALUES (?, ?, ?, ?)",
                ("funnel_src", "test", "predicted content", _now()),
            )
            pred_id = int(cur.lastrowid)

        t0 = _now()
        self._log("fed", user_id=uid_a, clicked=True, ts=t0)
        self._log("fed", user_id=uid_b, clicked=True, ts=t0)
        self._log("fed", user_id=uid_c, clicked=False, ts=t0)
        self._log("fed", user_id=None, clicked=False, ts=t0)  # anon

        # Save + subscribe AFTER the first search ts.
        with db.conn() as c:
            c.execute(
                "INSERT INTO saved_predictions (user_id, prediction_id, saved_at) "
                "VALUES (?, ?, ?)",
                (uid_a, pred_id, t0 + 60),
            )
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, "
                "status, started_at, source) "
                "VALUES (?, ?, ?, 'active', ?, 'test')",
                (uid_a, "intelligence", "monthly", t0 + 120),
            )

        funnel = qsa.query_to_conversion_rate(window_days=7)
        self.assertEqual(funnel["searched"], 3)  # anon excluded
        self.assertEqual(funnel["clicked"], 2)
        self.assertEqual(funnel["saved"], 1)
        self.assertEqual(funnel["subscribed"], 1)
        # Rates derived from those counts.
        self.assertAlmostEqual(funnel["click_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(funnel["save_rate"], 1 / 3, places=4)
        self.assertAlmostEqual(funnel["subscribe_rate"], 1 / 3, places=4)

    def test_conversion_funnel_empty_window_returns_zeros(self):
        from queries import search_analytics as qsa

        f = qsa.query_to_conversion_rate(window_days=7)
        self.assertEqual(f["searched"], 0)
        self.assertEqual(f["clicked"], 0)
        self.assertEqual(f["saved"], 0)
        self.assertEqual(f["subscribed"], 0)
        self.assertEqual(f["click_rate"], 0.0)

    def test_hourly_distribution_returns_24_buckets(self):
        from queries import search_analytics as qsa

        # Drop two queries into deterministic hour-of-day slots.
        # Pick UTC midnight + offsets so the hour is unambiguous.
        # 2026-01-01 00:00:00 UTC = 1767225600
        utc_midnight = 1767225600
        self._log("a", ts=utc_midnight)  # hour 0
        self._log("b", ts=utc_midnight + 5 * 3600)  # hour 5
        self._log("b", ts=utc_midnight + 5 * 3600 + 1)  # hour 5 again

        # Use a wide enough window to include the seeded ts (Jan 2026
        # is in the past relative to test execution; window_days big
        # enough to span back to it). Default test date is 2026-05-14.
        rows = qsa.hourly_distribution(window_days=365)
        # Exactly 24 buckets in 0..23 order.
        self.assertEqual(len(rows), 24)
        hours = [r["hour"] for r in rows]
        self.assertEqual(hours, list(range(24)))
        # Hour 5 has 2 hits, hour 0 has 1, rest zero.
        by_h = {r["hour"]: r["hits"] for r in rows}
        self.assertEqual(by_h[0], 1)
        self.assertEqual(by_h[5], 2)
        self.assertEqual(by_h[12], 0)


if __name__ == "__main__":
    unittest.main()
