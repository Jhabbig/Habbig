"""
Unit tests for sources/reddit.py. Uses respx to mock the public /new.json
endpoint — no network traffic.
"""

from __future__ import annotations

import httpx
import pytest

import config
from sources.reddit import RedditSource


def _reddit_response(sub: str, posts: list[dict]) -> dict:
    return {
        "data": {
            "children": [
                {"data": {
                    "id": p.get("id", f"x{i}"),
                    "title": p.get("title", ""),
                    "selftext": p.get("selftext", ""),
                    "created_utc": p.get("created_utc", 1_700_000_000),
                    "ups": p.get("ups", 0),
                    "num_comments": p.get("num_comments", 0),
                    "permalink": p.get("permalink", f"/r/{sub}/comments/{i}"),
                    "author": p.get("author"),
                }}
                for i, p in enumerate(posts)
            ]
        }
    }


async def test_fetch_parses_title_and_selftext(mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "REDDIT_SUBS", ["mildlyinfuriating"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get(
        "https://www.reddit.com/r/mildlyinfuriating/new.json",
        params__contains={"limit": str(config.REDDIT_POSTS_PER_SUB)},
    ).mock(return_value=httpx.Response(200, json=_reddit_response(
        "mildlyinfuriating",
        [{"id": "abc", "title": "United cancelled", "selftext": "again",
          "ups": 10, "num_comments": 5, "author": "u1"}],
    )))
    posts = await RedditSource().fetch()
    assert len(posts) == 1
    p = posts[0]
    assert p["id"] == "reddit:abc"
    assert p["source"] == "reddit"
    assert "United cancelled" in p["content"]
    assert "again" in p["content"]
    assert p["source_channel"] == "r/mildlyinfuriating"
    assert p["engagement"] == 15  # ups + comments
    assert p["author"] == "u1"
    assert p["url"].startswith("https://www.reddit.com/r/mildlyinfuriating")


async def test_fetch_skips_empty_content(mock_httpx, monkeypatch):
    """No title + no selftext → skip the post."""
    monkeypatch.setattr(config, "REDDIT_SUBS", ["empty"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/empty/new.json").mock(
        return_value=httpx.Response(200, json=_reddit_response(
            "empty",
            [{"id": "x", "title": "", "selftext": ""}],
        )),
    )
    assert await RedditSource().fetch() == []


async def test_fetch_skips_posts_without_id(mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "REDDIT_SUBS", ["noid"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/noid/new.json").mock(
        return_value=httpx.Response(200, json={
            "data": {"children": [{"data": {"title": "ghost"}}]}
        }),
    )
    assert await RedditSource().fetch() == []


async def test_429_triggers_per_sub_backoff(mock_httpx, monkeypatch):
    from sources import reddit as reddit_mod
    monkeypatch.setattr(config, "REDDIT_SUBS", ["hot"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/hot/new.json").mock(
        return_value=httpx.Response(429),
    )
    await RedditSource().fetch()
    assert "hot" in reddit_mod._backoff
    ready_at, fail_count = reddit_mod._backoff["hot"]
    assert fail_count == 1


async def test_one_bad_sub_doesnt_stop_others(mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "REDDIT_SUBS", ["bad", "good"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/bad/new.json").mock(
        return_value=httpx.Response(500),
    )
    mock_httpx.get("https://www.reddit.com/r/good/new.json").mock(
        return_value=httpx.Response(200, json=_reddit_response(
            "good",
            [{"id": "g1", "title": "great complaint"}],
        )),
    )
    posts = await RedditSource().fetch()
    assert len(posts) == 1
    assert posts[0]["id"] == "reddit:g1"


async def test_content_truncated_to_4000_chars(mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "REDDIT_SUBS", ["long"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    body = "x" * 8000
    mock_httpx.get("https://www.reddit.com/r/long/new.json").mock(
        return_value=httpx.Response(200, json=_reddit_response(
            "long",
            [{"id": "L1", "title": "tl", "selftext": body}],
        )),
    )
    posts = await RedditSource().fetch()
    assert len(posts[0]["content"]) <= 4000
