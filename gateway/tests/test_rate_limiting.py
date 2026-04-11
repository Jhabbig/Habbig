"""Tests for rate limiting — sliding window, keys, decorator, headers."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from security.rate_limiter import (
    SlidingWindowRateLimiter,
    rate_limit,
    get_client_ip,
    is_rate_limited,
    limiter as global_limiter,
)


class TestSlidingWindowRateLimiter(unittest.TestCase):
    def setUp(self):
        self.limiter = SlidingWindowRateLimiter()
        self.limiter._redis = None  # Force in-memory for tests

    def test_allows_under_limit(self):
        for i in range(5):
            allowed, remaining, _ = self.limiter.check("test1", 5, 60)
            self.assertTrue(allowed)

    def test_denies_over_limit(self):
        for _ in range(5):
            self.limiter.check("test2", 5, 60)
        allowed, remaining, retry_after = self.limiter.check("test2", 5, 60)
        self.assertFalse(allowed)
        self.assertEqual(remaining, 0)
        self.assertGreater(retry_after, 0)

    def test_different_keys_dont_interfere(self):
        # Exhaust limit for user_a
        for _ in range(5):
            self.limiter.check("user_a", 5, 60)
        allowed_a, _, _ = self.limiter.check("user_a", 5, 60)
        self.assertFalse(allowed_a)

        # user_b should still be allowed
        allowed_b, _, _ = self.limiter.check("user_b", 5, 60)
        self.assertTrue(allowed_b)

    def test_remaining_decrements(self):
        allowed, remaining, _ = self.limiter.check("rem_test", 5, 60)
        self.assertEqual(remaining, 4)
        allowed, remaining, _ = self.limiter.check("rem_test", 5, 60)
        self.assertEqual(remaining, 3)

    def test_window_expires(self):
        # Fill the window
        for _ in range(3):
            self.limiter.check("expire_test", 3, 60)
        allowed, _, _ = self.limiter.check("expire_test", 3, 60)
        self.assertFalse(allowed)

        # Manually move all timestamps into the past
        with self.limiter._lock:
            self.limiter._windows["expire_test"].clear()

        allowed, _, _ = self.limiter.check("expire_test", 3, 60)
        self.assertTrue(allowed)


class TestGetClientIP(unittest.TestCase):
    def test_direct_connection(self):
        request = MagicMock()
        request.headers = {}
        request.client.host = "10.0.0.1"
        self.assertEqual(get_client_ip(request), "10.0.0.1")

    def test_cloudflare_header(self):
        request = MagicMock()
        request.headers = {"cf-connecting-ip": "1.2.3.4"}
        request.client.host = "10.0.0.1"
        self.assertEqual(get_client_ip(request), "1.2.3.4")

    def test_x_forwarded_for(self):
        request = MagicMock()
        request.headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        request.client.host = "10.0.0.1"
        self.assertEqual(get_client_ip(request), "1.2.3.4")

    def test_no_client(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        self.assertEqual(get_client_ip(request), "unknown")


class TestRateLimitDecorator(unittest.TestCase):
    def test_decorator_allows_under_limit(self):
        from fastapi import Request
        from fastapi.responses import JSONResponse

        @rate_limit(limit=3, window_seconds=60)
        async def handler(request: Request):
            return JSONResponse({"ok": True})

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "raw_path": b"/test",
            "query_string": b"",
            "headers": [],
            "client": ("10.0.0.2", 1234),
        }
        request = Request(scope)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            for _ in range(3):
                result = loop.run_until_complete(handler(request))
                self.assertEqual(result.status_code, 200)
        finally:
            loop.close()

    def test_decorator_denies_over_limit(self):
        from fastapi import Request
        from fastapi.responses import JSONResponse

        @rate_limit(limit=2, window_seconds=60)
        async def handler(request: Request):
            response = JSONResponse({"ok": True})
            return response

        # Build a minimal Starlette scope-based Request
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "raw_path": b"/test",
            "query_string": b"",
            "headers": [],
            "client": ("10.0.0.3", 1234),
        }
        request = Request(scope)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Fill the limit
            loop.run_until_complete(handler(request))
            loop.run_until_complete(handler(request))
            # This should be rate limited
            result = loop.run_until_complete(handler(request))
            # Check it returned a 429 JSONResponse
            self.assertEqual(result.status_code, 429)
            self.assertIn("retry-after", {k.lower() for k in result.headers.keys()})
            self.assertIn("x-ratelimit-limit", {k.lower() for k in result.headers.keys()})
        finally:
            loop.close()


class TestIsRateLimitedWrapper(unittest.TestCase):
    """Backwards-compatible wrapper test."""

    def test_allows_first_calls(self):
        # Use a unique key
        key = f"test_wrapper_{time.time()}"
        self.assertFalse(is_rate_limited(key, limit=3, window_seconds=60))
        self.assertFalse(is_rate_limited(key, limit=3, window_seconds=60))
        self.assertFalse(is_rate_limited(key, limit=3, window_seconds=60))

    def test_denies_over_limit(self):
        key = f"test_wrapper_deny_{time.time()}"
        for _ in range(3):
            is_rate_limited(key, limit=3, window_seconds=60)
        # 4th should be denied
        self.assertTrue(is_rate_limited(key, limit=3, window_seconds=60))


class TestRateLimitDisabled(unittest.TestCase):
    def test_disabled_via_env(self):
        """When RATE_LIMIT_ENABLED=false, check always returns allowed."""
        from security import rate_limiter
        original = rate_limiter.RATE_LIMIT_ENABLED
        try:
            rate_limiter.RATE_LIMIT_ENABLED = False
            l = SlidingWindowRateLimiter()
            l._redis = None
            # Even after many calls, should still be allowed
            for _ in range(100):
                allowed, _, _ = l.check("disabled_test", 5, 60)
                self.assertTrue(allowed)
        finally:
            rate_limiter.RATE_LIMIT_ENABLED = original


if __name__ == "__main__":
    unittest.main()
