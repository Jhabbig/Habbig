"""Tests for the transmission/pusher module."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

from scraper.storage.models import RawPost
from scraper.transmission.pusher import push_batch, push_untransmitted


def _make_post(pid="twitter:1", platform="twitter"):
    return RawPost(
        id=pid, platform=platform, author_handle="test",
        author_display_name="Test", author_followers=100,
        author_verified=False, content="test content",
        posted_at=datetime.now(timezone.utc),
        scraped_at=datetime.now(timezone.utc),
        likes=0, retweets_or_boosts=0, replies=0,
        keyword_matched="test",
    )


class TestPushBatch:
    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.SCRAPER_API_KEY", "test-key-123")
    @patch("scraper.transmission.pusher.httpx.AsyncClient")
    async def test_push_batch_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        posts = [_make_post()]
        result = await push_batch(posts)
        assert result is True

        call_args = mock_client.post.call_args
        assert "Bearer test-key-123" in str(call_args)

    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.SCRAPER_API_KEY", "test-key-123")
    @patch("scraper.transmission.pusher.httpx.AsyncClient")
    async def test_push_batch_401_stops_retrying(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        posts = [_make_post()]
        result = await push_batch(posts)
        assert result is False
        # Should only call once on 401 (no retry)
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.SCRAPER_API_KEY", "test-key-123")
    @patch("scraper.transmission.pusher.httpx.AsyncClient")
    async def test_push_batch_5xx_retries(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        posts = [_make_post()]
        result = await push_batch(posts)
        assert result is False
        # Should retry 3 times on 5xx
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.SCRAPER_API_KEY", "")
    async def test_push_batch_no_api_key(self):
        posts = [_make_post()]
        result = await push_batch(posts)
        assert result is False


class TestPushUntransmitted:
    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.push_batch", new_callable=AsyncMock)
    @patch("scraper.transmission.pusher.store")
    async def test_push_untransmitted_success(self, mock_store, mock_push):
        posts = [_make_post("twitter:1"), _make_post("twitter:2")]
        mock_store.get_untransmitted.return_value = posts
        mock_push.return_value = True

        result = await push_untransmitted(platform="twitter")
        assert result["pushed"] == 2
        assert result["failed"] == 0
        mock_store.mark_transmitted.assert_called_once()

    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.push_batch", new_callable=AsyncMock)
    @patch("scraper.transmission.pusher.store")
    async def test_push_untransmitted_no_posts(self, mock_store, mock_push):
        mock_store.get_untransmitted.return_value = []
        result = await push_untransmitted()
        assert result["pushed"] == 0
        mock_push.assert_not_called()

    @pytest.mark.asyncio
    @patch("scraper.transmission.pusher.MAX_TRANSMISSION_ATTEMPTS", 10)
    @patch("scraper.transmission.pusher.push_batch", new_callable=AsyncMock)
    @patch("scraper.transmission.pusher.store")
    async def test_push_skips_exceeded_attempts(self, mock_store, mock_push):
        post = _make_post()
        post.transmission_attempts = 15  # Over max
        mock_store.get_untransmitted.return_value = [post]

        result = await push_untransmitted()
        assert result["skipped"] == 1
        assert result["pushed"] == 0
        mock_push.assert_not_called()
