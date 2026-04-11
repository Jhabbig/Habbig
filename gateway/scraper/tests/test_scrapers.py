"""Tests for scraper modules."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from scraper.storage.models import RawPost
from scraper.scrapers.twitter import TwitterScraper
from scraper.scrapers.truthsocial import TruthSocialScraper


class TestTwitterScraper:
    def setup_method(self):
        self.scraper = TwitterScraper()

    @patch("scraper.scrapers.twitter.store")
    def test_is_available_no_session(self, mock_store):
        mock_store.get_session.return_value = None
        assert self.scraper.is_available() is False

    @patch("scraper.scrapers.twitter.store")
    def test_is_available_invalid_session(self, mock_store):
        mock_store.get_session.return_value = MagicMock(valid=False)
        assert self.scraper.is_available() is False

    def test_parse_tweet_valid(self):
        raw = {
            "rest_id": "123456789",
            "legacy": {
                "id_str": "123456789",
                "full_text": "Bitcoin will win the election prediction market",
                "created_at": "Mon Jan 15 18:30:00 +0000 2024",
                "favorite_count": 42,
                "retweet_count": 10,
                "reply_count": 5,
            },
            "core": {
                "user_results": {
                    "result": {
                        "legacy": {
                            "screen_name": "testuser",
                            "name": "Test User",
                            "followers_count": 1000,
                            "verified": False,
                        },
                        "is_blue_verified": True,
                    }
                }
            },
        }
        post = self.scraper._parse_tweet(raw, "will win")
        assert post is not None
        assert post.id == "twitter:123456789"
        assert post.platform == "twitter"
        assert post.author_handle == "testuser"
        assert post.author_display_name == "Test User"
        assert post.author_followers == 1000
        assert post.author_verified is True
        assert "Bitcoin" in post.content
        assert post.likes == 42
        assert post.retweets_or_boosts == 10
        assert post.replies == 5
        assert post.keyword_matched == "will win"

    def test_parse_tweet_skips_retweet(self):
        raw = {
            "legacy": {
                "id_str": "111",
                "full_text": "RT @someone: hello",
                "retweeted_status_result": {"result": {}},
            },
            "core": {"user_results": {"result": {"legacy": {"screen_name": "x"}}}},
        }
        assert self.scraper._parse_tweet(raw, "test") is None

    def test_parse_tweet_missing_text(self):
        raw = {"legacy": {"id_str": "222", "full_text": ""}, "core": {}}
        assert self.scraper._parse_tweet(raw, "test") is None

    def test_parse_tweet_no_legacy(self):
        assert self.scraper._parse_tweet({}, "test") is None

    def test_extract_tweets_from_response(self):
        data = {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "999",
                                                            "legacy": {
                                                                "id_str": "999",
                                                                "full_text": "test tweet",
                                                                "favorite_count": 1,
                                                                "retweet_count": 0,
                                                                "reply_count": 0,
                                                            },
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "legacy": {
                                                                            "screen_name": "u",
                                                                            "name": "U",
                                                                            "followers_count": 5,
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
        tweets = self.scraper._extract_tweets_from_response(data)
        assert len(tweets) >= 1


class TestTruthSocialScraper:
    def setup_method(self):
        self.scraper = TruthSocialScraper()

    def test_is_available_always_true(self):
        """TruthSocial is always available for prominent account scraping."""
        assert self.scraper.is_available() is True

    @patch("scraper.scrapers.truthsocial.store")
    def test_has_session_no_session(self, mock_store):
        mock_store.get_session.return_value = None
        assert self.scraper._has_session() is False

    def test_parse_status_valid(self):
        raw = {
            "id": "109876543",
            "content": "<p>This is a test post about elections</p>",
            "created_at": "2024-01-15T18:30:00.000Z",
            "favourites_count": 20,
            "reblogs_count": 5,
            "replies_count": 3,
            "reblog": None,
            "account": {
                "acct": "testaccount",
                "display_name": "Test Account",
                "followers_count": 5000,
                "verified": True,
            },
        }
        post = self.scraper._parse_status(raw, keyword_matched="election")
        assert post is not None
        assert post.id == "truthsocial:109876543"
        assert post.platform == "truthsocial"
        assert post.author_handle == "testaccount"
        assert post.author_followers == 5000
        assert post.author_verified is True
        assert "elections" in post.content
        assert "<p>" not in post.content  # HTML stripped
        assert post.likes == 20
        assert post.keyword_matched == "election"

    def test_parse_status_skips_reblog(self):
        raw = {
            "id": "111",
            "content": "<p>hello</p>",
            "reblog": {"id": "222"},
            "account": {"acct": "x"},
        }
        assert self.scraper._parse_status(raw) is None

    def test_parse_status_empty_content(self):
        raw = {
            "id": "333",
            "content": "",
            "reblog": None,
            "account": {"acct": "x"},
        }
        assert self.scraper._parse_status(raw) is None


class TestRawPostDedup:
    def test_duplicate_post_id_format(self):
        p1 = RawPost(
            id="twitter:123", platform="twitter", author_handle="a",
            author_display_name="A", author_followers=0, author_verified=False,
            content="test", posted_at=datetime.now(timezone.utc),
            scraped_at=datetime.now(timezone.utc), likes=0,
            retweets_or_boosts=0, replies=0, keyword_matched="test",
        )
        p2 = RawPost(
            id="twitter:123", platform="twitter", author_handle="a",
            author_display_name="A", author_followers=0, author_verified=False,
            content="test", posted_at=datetime.now(timezone.utc),
            scraped_at=datetime.now(timezone.utc), likes=0,
            retweets_or_boosts=0, replies=0, keyword_matched="test",
        )
        assert p1.id == p2.id  # Same ID = duplicate

    def test_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        p = RawPost(
            id="truthsocial:456", platform="truthsocial", author_handle="b",
            author_display_name="B", author_followers=100, author_verified=True,
            content="hello", posted_at=now, scraped_at=now,
            likes=5, retweets_or_boosts=2, replies=1, keyword_matched="test",
        )
        d = p.to_dict()
        assert d["id"] == "truthsocial:456"
        assert d["author_verified"] is True
        assert isinstance(d["posted_at"], str)
