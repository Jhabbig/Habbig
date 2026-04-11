"""Tests for Sentry initialization and the sensitive-data scrubber."""

from __future__ import annotations

import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestScrubSensitiveData(unittest.TestCase):
    def setUp(self):
        from observability.sentry_setup import scrub_sensitive_data
        self.scrub = scrub_sensitive_data

    def test_strips_authorization_header(self):
        event = {"request": {"headers": {"Authorization": "Bearer SECRET", "Accept": "*/*"}}}
        out = self.scrub(event, None)
        self.assertEqual(out["request"]["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(out["request"]["headers"]["Accept"], "*/*")

    def test_strips_csrf_and_cookie_headers(self):
        event = {"request": {"headers": {"Cookie": "x=1", "X-CSRF-Token": "abcd"}}}
        out = self.scrub(event, None)
        self.assertEqual(out["request"]["headers"]["Cookie"], "[Filtered]")
        self.assertEqual(out["request"]["headers"]["X-CSRF-Token"], "[Filtered]")

    def test_strips_password_field(self):
        event = {"request": {"data": {"username": "alice", "password": "hunter2"}}}
        out = self.scrub(event, None)
        self.assertEqual(out["request"]["data"]["password"], "[Filtered]")
        self.assertEqual(out["request"]["data"]["username"], "alice")

    def test_strips_token_and_secret_fields(self):
        event = {"request": {"data": {"api_token": "x", "client_secret": "y", "name": "ok"}}}
        out = self.scrub(event, None)
        self.assertEqual(out["request"]["data"]["api_token"], "[Filtered]")
        self.assertEqual(out["request"]["data"]["client_secret"], "[Filtered]")
        self.assertEqual(out["request"]["data"]["name"], "ok")

    def test_strips_card_field(self):
        event = {"request": {"data": {"card_number": "4242424242424242", "amount": 100}}}
        out = self.scrub(event, None)
        self.assertEqual(out["request"]["data"]["card_number"], "[Filtered]")
        self.assertEqual(out["request"]["data"]["amount"], 100)

    def test_strips_cookies(self):
        event = {"request": {"cookies": {"session": "abc", "_csrf": "xyz"}}}
        out = self.scrub(event, None)
        for v in out["request"]["cookies"].values():
            self.assertEqual(v, "[Filtered]")

    def test_no_crash_on_minimal_event(self):
        out = self.scrub({}, None)
        self.assertEqual(out, {})


class TestInitSentry(unittest.TestCase):
    def test_returns_false_without_dsn(self):
        from observability.sentry_setup import init_sentry
        os.environ.pop("SENTRY_DSN", None)
        self.assertFalse(init_sentry())

    def test_returns_false_with_blank_dsn(self):
        from observability.sentry_setup import init_sentry
        os.environ["SENTRY_DSN"] = "   "
        try:
            self.assertFalse(init_sentry())
        finally:
            os.environ.pop("SENTRY_DSN", None)


class TestSetUserContext(unittest.TestCase):
    def test_does_not_crash_when_sentry_not_loaded(self):
        from observability.sentry_setup import set_user_context
        set_user_context(123, "user@example.com", "pro")  # should be a no-op


if __name__ == "__main__":
    unittest.main()
