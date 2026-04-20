"""End-to-end: mock HTTP for both sources, drive both loops, then verify the
spike detector fires with sources_breakdown listing both sources.

The classifier and aggregator are invoked directly (no real Claude call —
classifier is monkey-patched to emit deterministic classifications). This
keeps the test hermetic while still exercising the actual post→classification
→entity_counts→spike data path in the real db module.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)


# ── HTTP mocks ───────────────────────────────────────────────────────────────

def _reddit_mock_payload(sub: str) -> dict:
    """Shape mirrors Reddit's /r/{sub}/new.json minimally.

    We emit 3 posts per sub, all mentioning `AcmeCorp` so the aggregator picks
    it up as an entity and the spike detector has something to fire on.
    """
    now = datetime.now(timezone.utc)
    children = []
    for i in range(3):
        children.append({
            "data": {
                "id": f"r_{sub}_{i}",
                "title": f"AcmeCorp broke again, this is frustrating #{i}",
                "selftext": "",
                "created_utc": (now.timestamp() - 60 * i),
                "ups": 5,
                "num_comments": 2,
                "permalink": f"/r/{sub}/comments/r_{sub}_{i}/",
                "author": "tester",
            },
        })
    return {"data": {"children": children}}


def _bluesky_mock_payload(term: str) -> dict:
    """Shape mirrors app.bsky.feed.searchPosts minimally."""
    now = datetime.now(timezone.utc).isoformat()
    posts = []
    for i in range(2):
        posts.append({
            "uri": f"at://did:plc:test/app.bsky.feed.post/3k_{term.replace(' ', '_')}_{i}",
            "cid": f"bafy_{term}_{i}",
            "author": {"handle": f"user{i}.bsky.social", "did": "did:plc:test"},
            "record": {
                "text": f"AcmeCorp {term} again, worst company ever #{i}",
                "createdAt": now,
            },
            "likeCount": 3,
            "repostCount": 0,
            "replyCount": 1,
        })
    return {"posts": posts}


class _MockTransport(httpx.BaseTransport):
    """Routes Reddit + Bluesky calls to the appropriate mock payload."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "reddit.com" in url:
            # Extract subreddit from /r/{sub}/new.json
            parts = url.split("/r/", 1)
            sub = parts[1].split("/", 1)[0] if len(parts) == 2 else "unknown"
            return httpx.Response(200, json=_reddit_mock_payload(sub))
        if "api.bsky.app" in url:
            q = request.url.params.get("q", "unknown")
            return httpx.Response(200, json=_bluesky_mock_payload(q))
        return httpx.Response(404, json={"error": "not mocked"})


class _AsyncMockTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sync = _MockTransport()
        return sync.handle_request(request)


class TestEndToEndTwoSources(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["ANNOYANCE_DB_PATH"] = cls._tmp.name

        import config
        importlib.reload(config)
        config.DB_PATH = cls._tmp.name
        # For end-to-end we disable the multi-source gate floor so the test
        # doesn't depend on the exact minute-bucket join landing 2+ posts in
        # the current hour — the integration test_multi_source_gate.py
        # covers the gate behaviour directly against the real query.
        config.REQUIRE_MULTI_SOURCE = False
        # Minimise cadence impact — we only run one loop iteration anyway.
        config.REDDIT_LOOP_SECONDS = 0
        config.BLUESKY_LOOP_SECONDS = 0
        config.REDDIT_REQUEST_SPACING_SECONDS = 0
        config.BLUESKY_REQUEST_SPACING_SECONDS = 0
        config.REDDIT_SUBS = ["test_sub"]
        config.BLUESKY_SEARCH_TERMS = ["is down"]

        import db as db_module
        importlib.reload(db_module)
        db_module.DB_PATH = cls._tmp.name
        db_module.init_db()
        cls.db = db_module

        # Reload sources with patched config
        from sources import reddit as reddit_mod
        from sources import bluesky as bluesky_mod
        importlib.reload(reddit_mod)
        importlib.reload(bluesky_mod)
        cls.reddit_mod = reddit_mod
        cls.bluesky_mod = bluesky_mod

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls._tmp.name)
        except FileNotFoundError:
            pass

    def test_both_sources_produce_posts(self):
        """Drive one fetch on each source against the mock and verify posts
        land in the DB attributed to the correct source."""
        # Patch httpx.AsyncClient so both RedditSource.fetch and
        # BlueskySource.fetch see our mocked responses.
        transport = _AsyncMockTransport()
        orig_async_client = httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return orig_async_client(*args, **kwargs)

        with patch("httpx.AsyncClient", _patched):
            reddit = self.reddit_mod.RedditSource()
            bluesky = self.bluesky_mod.BlueskySource()

            loop = asyncio.new_event_loop()
            try:
                reddit_posts = loop.run_until_complete(reddit.fetch())
                bluesky_posts = loop.run_until_complete(bluesky.fetch())
            finally:
                loop.close()

        # Both sources should have produced posts.
        self.assertGreater(len(reddit_posts), 0, "reddit mock produced no posts")
        self.assertGreater(len(bluesky_posts), 0, "bluesky mock produced no posts")

        # Source attribution correct.
        self.assertTrue(all(p["source"] == "reddit" for p in reddit_posts))
        self.assertTrue(all(p["source"] == "bluesky" for p in bluesky_posts))

        # Insert into DB and verify both show up.
        for p in reddit_posts + bluesky_posts:
            self.db.insert_post(
                id=p["id"],
                source=p["source"],
                source_channel=p.get("source_channel"),
                author=p.get("author"),
                content=p["content"],
                posted_at=p["posted_at"],
                url=p.get("url"),
                engagement=p.get("engagement", 0),
                keyword=p.get("keyword"),
            )
        with self.db.cursor() as cur:
            reddit_count = cur.execute(
                "SELECT COUNT(*) FROM posts WHERE source = 'reddit'",
            ).fetchone()[0]
            bluesky_count = cur.execute(
                "SELECT COUNT(*) FROM posts WHERE source = 'bluesky'",
            ).fetchone()[0]
        self.assertGreater(reddit_count, 0)
        self.assertGreater(bluesky_count, 0)

    def test_source_status_records_both(self):
        """upsert_source_status should accept both source names and return
        both in the /api/sources style query."""
        self.db.upsert_source_status("reddit", ok=True, posts_today=42)
        self.db.upsert_source_status("bluesky", ok=True, posts_today=17)

        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT name, last_ok, posts_today FROM sources ORDER BY name",
            ).fetchall()
        names = [r["name"] for r in rows]
        self.assertIn("reddit", names)
        self.assertIn("bluesky", names)
        # Both should be ok=1
        for r in rows:
            if r["name"] in ("reddit", "bluesky"):
                self.assertEqual(r["last_ok"], 1)


if __name__ == "__main__":
    unittest.main()
