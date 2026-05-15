"""
HIGH FIX (H-2) regression tests — message-content redaction patterns.

The base behaviour of ``StructuredFormatter`` (request-context wiring,
ring buffer, extra-field scrubbing on the obvious password/token keys)
is covered in ``test_logging.py``. This file pins down the H-2
expansion that closes the gaps the security audit flagged:

  1. ``_MESSAGE_REDACT_PATTERNS`` catches JWTs, the ``Stripe-Signature``
     header value, and HMAC-style ``sig=``/``hmac=`` query parameters,
     in addition to the bearer / basic-auth / email patterns already
     covered.
  2. ``SENSITIVE_KEY_HINTS`` redacts the short-lived secrets and signed-
     URL components that pre-H-2 leaked in the clear (``otp``, ``code``,
     ``signature``, ``hash``, ``salt``, ``nonce``, ``magic_link``,
     ``callback_url``, generic ``url``).
  3. ``StructuredFormatter.format()`` reads ``ENVIRONMENT`` from
     ``os.environ`` on every record — so a runtime override (or a
     monkeypatched test env) shows up on the next log line without
     a process restart.

Each test isolates one of those guarantees. Failing any of them means a
previously-confidential value would land in plaintext in either the
local rotating file, the BetterStack ingest, or the admin ring buffer.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest

# Tests live one directory below ``gateway/`` — pull the production module
# from the parent. ``conftest.py`` already adds the gateway dir to
# ``sys.path`` for the rest of the suite; we keep the explicit insert here
# so this file is runnable standalone (``python -m unittest tests.test_log_redaction``).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging_config as lc  # noqa: E402


class _CapturingHandler(logging.Handler):
    """Captures formatted records so individual JSON payloads can be inspected.

    Duplicated from ``test_logging.py`` rather than imported because that
    test file is also self-contained and a cross-import would couple the
    two suites together (changing the helper in one place silently
    breaks the other).
    """

    def __init__(self, formatter):
        super().__init__()
        self.setFormatter(formatter)
        self.records: list[str] = []

    def emit(self, record):  # pragma: no cover — standard path
        try:
            self.records.append(self.format(record))
        except Exception:
            self.handleError(record)


class _RedactionTestBase(unittest.TestCase):
    """Shared scaffolding — every test in this file needs a logger wired
    to the structured JSON formatter AND the redaction filter, because
    the filter is what strips secrets out of message bodies before the
    formatter ever sees them."""

    def setUp(self):
        self.formatter = lc.StructuredFormatter()
        self.handler = _CapturingHandler(self.formatter)
        # The filter is what mutates record.msg / record.args in-place,
        # so it MUST run before the handler emits. In production the
        # filter is wired to every handler inside ``configure_logging``;
        # here we attach it to the single capture handler.
        self.handler.addFilter(lc._redaction_filter)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False  # don't double-emit via root
        lc.clear_request_context()

    def tearDown(self):
        self.logger.handlers.clear()
        lc.clear_request_context()

    def _last_json(self) -> dict:
        self.assertTrue(self.handler.records, "no records captured")
        return json.loads(self.handler.records[-1])


class TestJwtRedaction(_RedactionTestBase):
    """JWTs are the highest-priority leak. A single stray ``log.info`` of a
    cookie value or an Authorization header pre-H-2 would dump a fully-
    valid session token into the file log AND into BetterStack."""

    def test_bare_jwt_in_message_is_redacted(self):
        # Realistic three-part JWT shape (header.payload.signature). The
        # signature is intentionally short — the regex only requires the
        # ``eyJ`` prefix + base64url chars + dots.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyXzQyIn0.abc-def_GHI"
        self.logger.info("validating token %s", jwt)
        rec = self._last_json()
        self.assertNotIn(jwt, rec["message"],
                         "JWT must not survive into the rendered message")
        self.assertIn("<jwt-redacted>", rec["message"])

    def test_jwt_in_url_query_string_is_redacted(self):
        # Common case: ``/callback?token=eyJ...`` showing up in an access
        # log. The JWT pattern fires on the ``eyJ`` prefix regardless of
        # surrounding URL structure.
        url = "/callback?session=eyJ0eXAiOiJKV1QifQ.payloadbody.sigchunk&other=1"
        self.logger.info("request %s", url)
        rec = self._last_json()
        self.assertNotIn("eyJ0eXAiOiJKV1QifQ", rec["message"])
        self.assertIn("<jwt-redacted>", rec["message"])
        # Non-JWT query params must survive untouched — we don't want
        # the redactor eating legitimate audit context.
        self.assertIn("other=1", rec["message"])

    def test_bearer_prefix_preserved_with_jwt_body(self):
        # The ``bearer`` pattern runs BEFORE the bare-JWT pattern in
        # ``_MESSAGE_REDACT_PATTERNS``. Both must scrub the body, but the
        # bearer match wins so we get the ``bearer [REDACTED]`` shape
        # rather than ``bearer <jwt-redacted>``.
        self.logger.info("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.body.sig")
        rec = self._last_json()
        # Compare case-insensitively on the prefix only — the replacement
        # text ``[REDACTED]`` is upper-cased verbatim by the substitution
        # rule, but the original ``Bearer`` prefix was Title-cased, so we
        # lowercase the whole message for a stable substring check.
        self.assertIn("bearer [redacted]", rec["message"].lower())
        self.assertNotIn("eyJ", rec["message"])

    def test_stripe_signature_header_redacted(self):
        # The Stripe-Signature header value is itself a comma-separated
        # ``t=...,v1=...,v0=...`` blob. Treat the whole thing as opaque
        # — anything after the colon up to the next whitespace burns.
        sig = "t=1700000000,v1=abc1234567890abcdef,v0=def9876"
        self.logger.info("incoming webhook headers: Stripe-Signature: %s host: api", sig)
        rec = self._last_json()
        self.assertNotIn(sig, rec["message"])
        self.assertIn("stripe-signature: <redacted>", rec["message"].lower())
        # The remainder of the headers list must remain visible.
        self.assertIn("host: api", rec["message"])

    def test_hmac_sig_param_redacted(self):
        # HMAC-signed download URLs (Cloudflare R2, AWS S3 pre-signed):
        # ``?sig=<base64>&expires=<unix>``. We only zap the secret half.
        self.logger.info("download url: https://cdn/file?sig=ABCDxyz123==&expires=1700")
        rec = self._last_json()
        self.assertIn("sig=<redacted>", rec["message"])
        self.assertNotIn("ABCDxyz123==", rec["message"])
        # ``expires`` is a public timestamp; must not be touched.
        self.assertIn("expires=1700", rec["message"])

    def test_hmac_param_redacted(self):
        # Same shape, different param name. Telegram webhook URLs and
        # some custom signing schemes use ``hmac=`` instead of ``sig=``.
        self.logger.info("verify ?hmac=deadbeefcafe1234 source=tg")
        rec = self._last_json()
        self.assertIn("hmac=<redacted>", rec["message"])
        self.assertNotIn("deadbeefcafe1234", rec["message"])


class TestOtpAndExtrasRedaction(_RedactionTestBase):
    """``extra={...}`` fields go through ``_scrub_value``, NOT the message
    regex pipeline. The H-2 hint additions (``otp``, ``code``, ``hash``,
    etc.) all live on that side; missing one of them silently leaks the
    secret as a top-level JSON field in the BetterStack record."""

    def test_otp_code_in_extras_is_redacted(self):
        # The canonical case from the audit: magic-link OTP arriving as
        # an ``extra`` field on the auth route's log call.
        self.logger.info("login attempt", extra={"otp_code": "839204",
                                                  "user_id": 42})
        rec = self._last_json()
        self.assertEqual(rec["otp_code"], "[REDACTED]")
        # user_id is allowlisted — must stay visible for support.
        self.assertEqual(rec["user_id"], 42)

    def test_bare_otp_field_redacted(self):
        # Sometimes the field is just ``otp`` with no suffix.
        self.logger.info("verify", extra={"otp": "112233"})
        rec = self._last_json()
        self.assertEqual(rec["otp"], "[REDACTED]")

    def test_signature_hash_salt_nonce_redacted(self):
        # The four short-lived crypto fields from H-2. All come through
        # the substring match — anything containing one of these tokens
        # in the key name burns.
        self.logger.info("crypto", extra={
            "signature": "abc==",
            "payload_hash": "0xdeadbeef",
            "password_salt": "saltyvalue",
            "request_nonce": "n0nc3val",
        })
        rec = self._last_json()
        self.assertEqual(rec["signature"], "[REDACTED]")
        self.assertEqual(rec["payload_hash"], "[REDACTED]")
        self.assertEqual(rec["password_salt"], "[REDACTED]")
        self.assertEqual(rec["request_nonce"], "[REDACTED]")

    def test_magic_link_and_callback_url_redacted(self):
        # Full URLs that carry a one-shot token in the query string.
        self.logger.info("send email", extra={
            "magic_link": "https://narve.ai/m/verify?t=abc123tokenbody",
            "callback_url": "https://oauth.example/cb?code=secretcode",
        })
        rec = self._last_json()
        self.assertEqual(rec["magic_link"], "[REDACTED]")
        self.assertEqual(rec["callback_url"], "[REDACTED]")

    def test_generic_url_field_redacted_by_default(self):
        # The bare ``url`` hint covers any *_url field that isn't
        # explicitly allowlisted — e.g. ``invite_url`` / ``reset_url``
        # which carry a one-shot token.
        self.logger.info("email send", extra={
            "invite_url": "https://narve.ai/redeem/abc123",
            "reset_url": "https://narve.ai/reset/xyz789",
        })
        rec = self._last_json()
        self.assertEqual(rec["invite_url"], "[REDACTED]")
        self.assertEqual(rec["reset_url"], "[REDACTED]")

    def test_allowlisted_url_fields_visible(self):
        # ``app_url``, ``share_url``, ``og_image_url`` are NOT secrets —
        # they're config values or public share links. They opt out of
        # the ``url`` hint via the allowlist so support tooling can
        # still see them.
        self.logger.info("template ctx", extra={
            "app_url": "https://narve.ai",
            "share_url": "/s/m/abc",
            "og_image_url": "/og/shared/market/xyz",
            "avatar_url": "/static/avatar/42.png",
        })
        rec = self._last_json()
        self.assertEqual(rec["app_url"], "https://narve.ai")
        self.assertEqual(rec["share_url"], "/s/m/abc")
        self.assertEqual(rec["og_image_url"], "/og/shared/market/xyz")
        self.assertEqual(rec["avatar_url"], "/static/avatar/42.png")

    def test_diagnostic_code_fields_visible(self):
        # The ``code`` hint added in H-2 would otherwise catch
        # ``status_code`` / ``error_code`` etc. Those MUST stay visible
        # for ops to debug — that's what the H-2 allowlist guards.
        self.logger.info("response", extra={
            "status_code": 500,
            "error_code": "billing_failed",
            "country_code": "US",
        })
        rec = self._last_json()
        self.assertEqual(rec["status_code"], 500)
        self.assertEqual(rec["error_code"], "billing_failed")
        self.assertEqual(rec["country_code"], "US")


class TestEnvironmentReadPerRecord(_RedactionTestBase):
    """MED-1: ``ENVIRONMENT`` is read on every record, not captured at
    import time. A test or runtime config flip must take effect on the
    next log line — without this, switching ENVIRONMENT in a test
    fixture (e.g. to assert dev-only branches) silently keeps the
    captured ``production`` value forever."""

    def test_monkeypatched_environment_takes_effect(self):
        orig = os.environ.get("ENVIRONMENT")
        try:
            os.environ["ENVIRONMENT"] = "production"
            self.logger.info("prod log")
            rec_prod = self._last_json()
            self.assertEqual(rec_prod["environment"], "production")

            # Flip the env var — the very next record must reflect it.
            os.environ["ENVIRONMENT"] = "dev"
            self.logger.info("dev log")
            rec_dev = self._last_json()
            self.assertEqual(rec_dev["environment"], "dev")

            # And flipping back must work too — no stuck cache.
            os.environ["ENVIRONMENT"] = "staging"
            self.logger.info("staging log")
            rec_staging = self._last_json()
            self.assertEqual(rec_staging["environment"], "staging")
        finally:
            if orig is None:
                os.environ.pop("ENVIRONMENT", None)
            else:
                os.environ["ENVIRONMENT"] = orig

    def test_missing_environment_falls_back_to_production(self):
        orig = os.environ.pop("ENVIRONMENT", None)
        try:
            self.logger.info("no env set")
            rec = self._last_json()
            self.assertEqual(rec["environment"], "production")
        finally:
            if orig is not None:
                os.environ["ENVIRONMENT"] = orig

    def test_empty_string_environment_falls_back_to_production(self):
        # ``ENVIRONMENT=""`` is a real failure mode — docker compose
        # sometimes injects empty strings for unset values. The helper
        # treats whitespace-only / empty as "unset" and returns the
        # production default.
        orig = os.environ.get("ENVIRONMENT")
        try:
            os.environ["ENVIRONMENT"] = "   "
            self.logger.info("whitespace env")
            rec = self._last_json()
            self.assertEqual(rec["environment"], "production")
        finally:
            if orig is None:
                os.environ.pop("ENVIRONMENT", None)
            else:
                os.environ["ENVIRONMENT"] = orig


if __name__ == "__main__":
    unittest.main()
