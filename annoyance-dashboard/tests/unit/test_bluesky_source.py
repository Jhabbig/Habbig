"""Unit tests for sources.bluesky.

Exercises the pure-Python pieces — post parsing, URL building, per-term
backoff state — without hitting the network. HTTP integration is covered
by tests/integration/test_end_to_end_two_sources.py.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

# Add project root to path so `import config`, `import sources.*` work.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

from sources.bluesky import (  # noqa: E402
    BlueskySource,
    _backoff,
    _is_term_in_backoff,
    _record_term_failure,
    _record_term_success,
    _reset_backoff_for_tests,
    _rkey_from_uri,
)


def _mk_raw_post(
    cid: str = "bafyreia",
    text: str = "This is so frustrating!",
    handle: str = "user.bsky.social",
    posted_at: str = "2026-04-14T15:30:00.000Z",
    likes: int | None = 5,
    reposts: int | None = 1,
    replies: int | None = 2,
    uri_suffix: str = "3kxabc123",
    did: str = "did:plc:abc123",
) -> dict:
    """Build a minimal AT Protocol `searchPosts` result row."""
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{uri_suffix}",
        "cid": cid,
        "author": {"handle": handle, "did": did},
        "record": {"text": text, "createdAt": posted_at},
        "likeCount": likes,
        "repostCount": reposts,
        "replyCount": replies,
    }


class TestParseValidPost(unittest.TestCase):
    def test_parse_valid_post(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        parsed = src._parse(raw, term="is down")

        self.assertEqual(parsed["id"], "bluesky:bafyreia")
        self.assertEqual(parsed["source"], "bluesky")
        self.assertEqual(parsed["source_channel"], "search:is down")
        self.assertEqual(parsed["author"], "user.bsky.social")
        self.assertEqual(parsed["content"], "This is so frustrating!")
        self.assertEqual(parsed["posted_at"], "2026-04-14T15:30:00.000Z")
        self.assertEqual(parsed["engagement"], 5 + 1 + 2)
        self.assertEqual(parsed["keyword"], "is down")

    def test_parse_returns_none_for_missing_cid(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        raw.pop("cid")
        self.assertIsNone(src._parse(raw, "anything"))

    def test_parse_returns_none_for_empty_text(self):
        src = BlueskySource()
        raw = _mk_raw_post(text="   ")
        self.assertIsNone(src._parse(raw, "anything"))

    def test_parse_returns_none_for_missing_created_at(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        raw["record"].pop("createdAt")
        self.assertIsNone(src._parse(raw, "anything"))


class TestParseHandlesMissingEngagementCounters(unittest.TestCase):
    """Brand-new posts often have zero engagement fields; we must default to 0."""

    def test_all_engagement_fields_missing(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        raw.pop("likeCount")
        raw.pop("repostCount")
        raw.pop("replyCount")
        parsed = src._parse(raw, "x")
        self.assertEqual(parsed["engagement"], 0)

    def test_some_engagement_fields_none(self):
        src = BlueskySource()
        raw = _mk_raw_post(likes=None, reposts=None, replies=3)
        parsed = src._parse(raw, "x")
        self.assertEqual(parsed["engagement"], 3)

    def test_engagement_fields_are_zero(self):
        src = BlueskySource()
        raw = _mk_raw_post(likes=0, reposts=0, replies=0)
        parsed = src._parse(raw, "x")
        self.assertEqual(parsed["engagement"], 0)


class TestParseBuildsBskyUrl(unittest.TestCase):
    """The URL is derived from `uri` + `author.handle`. Both must work with
    the Bluesky web app's profile/post route."""

    def test_url_built_from_handle_and_rkey(self):
        src = BlueskySource()
        raw = _mk_raw_post(
            uri_suffix="3kfoo_bar",
            handle="julian.bsky.social",
            did="did:plc:xyz",
        )
        parsed = src._parse(raw, "x")
        self.assertEqual(
            parsed["url"],
            "https://bsky.app/profile/julian.bsky.social/post/3kfoo_bar",
        )

    def test_url_falls_back_to_did_when_handle_missing(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        raw["author"].pop("handle")
        parsed = src._parse(raw, "x")
        self.assertIn("did:plc:abc123", parsed["url"])
        self.assertIn("3kxabc123", parsed["url"])

    def test_url_none_when_uri_malformed(self):
        src = BlueskySource()
        raw = _mk_raw_post()
        raw["uri"] = ""  # malformed — no path to extract rkey from
        parsed = src._parse(raw, "x")
        self.assertIsNone(parsed["url"])

    def test_rkey_helper(self):
        self.assertEqual(
            _rkey_from_uri("at://did:plc:abc/app.bsky.feed.post/3kfoo"),
            "3kfoo",
        )
        self.assertIsNone(_rkey_from_uri(""))


class TestBackoffStatePerTermIsolated(unittest.TestCase):
    """Failures on one term must not affect the backoff for another.
    Module-level state means tests share it; we reset explicitly."""

    def setUp(self):
        _reset_backoff_for_tests()

    def tearDown(self):
        _reset_backoff_for_tests()

    def test_success_on_new_term_clears_nothing(self):
        _record_term_success("fresh term")
        self.assertFalse(_is_term_in_backoff("fresh term"))

    def test_failure_puts_term_in_backoff(self):
        _record_term_failure("aws down")
        self.assertTrue(_is_term_in_backoff("aws down"))

    def test_failure_on_one_term_does_not_affect_another(self):
        _record_term_failure("term_a")
        self.assertTrue(_is_term_in_backoff("term_a"))
        self.assertFalse(_is_term_in_backoff("term_b"))

    def test_success_clears_backoff(self):
        _record_term_failure("term_a")
        self.assertTrue(_is_term_in_backoff("term_a"))
        _record_term_success("term_a")
        self.assertFalse(_is_term_in_backoff("term_a"))

    def test_repeated_failures_increase_delay(self):
        # Re-resolve _backoff at call time: another test file calls
        # importlib.reload(sources.bluesky), which rebinds the module's
        # _backoff to a fresh dict. The module-load-time `from ... import
        # _backoff` at the top of this file would then point at a stale dict.
        import sources.bluesky as _bmod
        _bmod._record_term_failure("term_x")
        first_ready_at, first_count = _bmod._backoff["term_x"]
        _bmod._record_term_failure("term_x")
        second_ready_at, second_count = _bmod._backoff["term_x"]
        # fail_count grows
        self.assertGreater(second_count, first_count)
        # ready_at pushes further into the future (delay doubled)
        self.assertGreater(second_ready_at, first_ready_at)


if __name__ == "__main__":
    unittest.main()
