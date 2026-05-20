"""Reddit + RSS scraper tests with mocked HTTP."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.scrapers.reddit import RedditScraper
from app.scrapers.rss import RSSScraper, _handle_from_feed_url


def _fake_reddit_listing(items):
    return {"data": {"children": [{"data": d} for d in items]}}


def _fake_http_response(status_code=200, json_data=None, text=""):
    class _R:
        def __init__(self):
            self.status_code = status_code
            self._json = json_data
            self.text = text
        def json(self):
            return self._json
    return _R()


@pytest.mark.asyncio
async def test_reddit_filters_short_posts_and_keywordless_posts():
    items = [
        {  # short — discarded
            "id": "a", "title": "yo", "selftext": "", "author": "alice",
            "created_utc": 1700000000, "score": 10, "subreddit_subscribers": 100,
        },
        {  # no keyword — discarded
            "id": "b", "title": "Just chatting about my weekend trip to the coast",
            "selftext": "It was very nice and sunny.", "author": "bob",
            "created_utc": 1700000000, "score": 5,
        },
        {  # contains "will win" — accepted
            "id": "c", "title": "Why the Lakers will win the championship this year",
            "selftext": "Long analysis of why I think the Lakers take it all this year.",
            "author": "carol", "created_utc": 1700000000, "score": 200,
            "subreddit_subscribers": 5_000_000, "num_comments": 50,
        },
    ]

    async def fake_get(self, url, params=None, **kwargs):
        return _fake_http_response(200, _fake_reddit_listing(items))

    with patch("httpx.AsyncClient.get", new=fake_get):
        posts = await RedditScraper(subreddits=["test"]).fetch(["will win"], limit=10)

    assert len(posts) == 1
    assert posts[0].id == "reddit:c"
    assert posts[0].author_handle == "carol"
    assert posts[0].engagement["score"] == 200
    # Reddit public JSON doesn't expose author karma; we deliberately don't
    # use subreddit_subscribers as a follower proxy (would inflate every
    # r/politics poster to ~8M followers in the credibility engine).
    assert posts[0].follower_count == 0


@pytest.mark.asyncio
async def test_reddit_skips_nsfw_and_deleted():
    items = [
        {"id": "x", "title": "X% chance Trump wins the primary in March of next year",
         "selftext": "decent analysis", "author": "[deleted]", "created_utc": 1700000000,
         "score": 1},
        {"id": "y", "title": "X% chance Trump wins the primary in March of next year",
         "selftext": "spicy", "author": "alice", "created_utc": 1700000000, "score": 1,
         "over_18": True},
    ]
    async def fake_get(self, url, params=None, **kwargs):
        return _fake_http_response(200, _fake_reddit_listing(items))

    with patch("httpx.AsyncClient.get", new=fake_get):
        posts = await RedditScraper(subreddits=["test"]).fetch(["chance"], limit=10)
    assert posts == []


@pytest.mark.asyncio
async def test_reddit_handles_404_gracefully():
    async def fake_get(self, url, params=None, **kwargs):
        return _fake_http_response(404)
    with patch("httpx.AsyncClient.get", new=fake_get):
        posts = await RedditScraper(subreddits=["nonexistent"]).fetch(["will"], limit=10)
    assert posts == []


def test_reddit_unavailable_when_no_subs():
    assert RedditScraper(subreddits=[]).is_available() is False


def test_rss_handle_from_url():
    assert _handle_from_feed_url("https://matt.substack.com/feed") == "matt"
    assert _handle_from_feed_url("https://www.example.com/rss") == "example.com"
    assert _handle_from_feed_url("") == "rss-unknown"


_RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Substack</title>
    <item>
      <guid>post-1</guid>
      <title>Why Tesla stock will hit 500 this year</title>
      <link>https://example.substack.com/p/post-1</link>
      <description>&lt;p&gt;I predict TSLA will hit 500. Big chance.&lt;/p&gt;</description>
      <pubDate>Mon, 04 May 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <guid>post-2</guid>
      <title>A poem about clouds</title>
      <link>https://example.substack.com/p/post-2</link>
      <description>Clouds drift past my window.</description>
      <pubDate>Tue, 05 May 2026 12:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


@pytest.mark.asyncio
async def test_rss_parses_substack_feed_and_filters_by_keyword():
    async def fake_get(self, url, params=None, **kwargs):
        return _fake_http_response(200, text=_RSS_SAMPLE)

    with patch("httpx.AsyncClient.get", new=fake_get):
        posts = await RSSScraper(feeds=["https://example.substack.com/feed"]).fetch(
            ["predict", "will hit"], limit=10,
        )

    assert len(posts) == 1  # poem post filtered out (no prediction keywords)
    assert posts[0].platform == "rss"
    assert posts[0].author_handle == "example"  # derived from substack subdomain
    assert "TSLA will hit 500" in posts[0].content


@pytest.mark.asyncio
async def test_rss_returns_empty_when_no_feeds():
    posts = await RSSScraper(feeds=[]).fetch(["anything"], limit=10)
    assert posts == []


_ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <id>atom-1</id>
    <title>I predict Bitcoin will break 200k</title>
    <link href="https://example.org/post1"/>
    <summary>Detailed analysis showing why BTC will hit 200k this cycle.</summary>
    <updated>2026-05-04T12:00:00Z</updated>
    <author><name>Jane Author</name></author>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_rss_parses_atom_feed():
    async def fake_get(self, url, params=None, **kwargs):
        return _fake_http_response(200, text=_ATOM_SAMPLE)

    with patch("httpx.AsyncClient.get", new=fake_get):
        posts = await RSSScraper(feeds=["https://example.org/atom"]).fetch(["predict"], limit=10)

    assert len(posts) == 1
    assert posts[0].author_handle == "Jane Author"
