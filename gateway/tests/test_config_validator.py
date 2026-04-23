"""Tests for gateway/config.py's startup validator.

The validator runs at boot and is expected to:
  - Exit the process with code 2 when a REQUIRED var is missing in PRODUCTION.
  - Warn (not exit) in dev mode.
  - Validate shape of optional vars when they ARE set.
  - Skip checks for conditional vars when their trigger isn't set.

We exercise each branch with monkey-patched os.environ. No DB, no
network.
"""

from __future__ import annotations

import os
import unittest

import config


def _env(mapping: dict) -> dict:
    """Return an os.environ-like snapshot to restore later."""
    return {k: os.environ.get(k) for k in mapping}


def _restore(snapshot: dict) -> None:
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _set(mapping: dict) -> None:
    for k, v in mapping.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestValidateConfig(unittest.TestCase):
    def setUp(self):
        # Track every var we touch so teardown puts the environment back
        # exactly as we found it — tests share a process.
        self.tracked: dict = {}

    def tearDown(self):
        _restore(self.tracked)

    # ── Required vars ────────────────────────────────────────────────

    def test_missing_required_in_dev_returns_errors(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "",
            "CREDENTIALS_ENCRYPTION_KEY": "",
            "GATEWAY_COOKIE_SECRET": "",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)

        errs = config.validate_config()
        self.assertTrue(any("SITE_ACCESS_TOKEN" in e for e in errs))
        self.assertTrue(any("CREDENTIALS_ENCRYPTION_KEY" in e for e in errs))
        self.assertTrue(any("GATEWAY_COOKIE_SECRET" in e for e in errs))

    def test_required_short_values_rejected(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "short",
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)

        errs = config.validate_config()
        self.assertTrue(any("SITE_ACCESS_TOKEN" in e for e in errs))
        # The other two are long enough, so they must NOT appear.
        self.assertFalse(any("CREDENTIALS_ENCRYPTION_KEY" in e for e in errs))
        self.assertFalse(any("GATEWAY_COOKIE_SECRET" in e for e in errs))

    def test_all_required_set_returns_empty(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        # Optional shape errors can still happen if the shared env has
        # a bad SENTRY_TRACES_SAMPLE_RATE etc. Assert only that no
        # required-var complaints landed.
        for required in ("SITE_ACCESS_TOKEN", "CREDENTIALS_ENCRYPTION_KEY",
                         "GATEWAY_COOKIE_SECRET"):
            self.assertFalse(any(required in e for e in errs),
                             f"unexpected error for {required}: {errs}")

    # ── Conditional vars ─────────────────────────────────────────────

    def test_stripe_secret_not_required_when_no_price_id(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "STRIPE_PRICE_ID_TRADERS_MONTHLY": "",
            "STRIPE_SECRET_KEY": "",
            "STRIPE_WEBHOOK_SECRET": "",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertFalse(any("STRIPE_SECRET_KEY" in e for e in errs))
        self.assertFalse(any("STRIPE_WEBHOOK_SECRET" in e for e in errs))

    def test_stripe_secret_required_when_price_id_set(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "STRIPE_PRICE_ID_TRADERS_MONTHLY": "price_abc123",
            "STRIPE_SECRET_KEY": "",
            "STRIPE_WEBHOOK_SECRET": "",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertTrue(any("STRIPE_SECRET_KEY" in e for e in errs))
        self.assertTrue(any("STRIPE_WEBHOOK_SECRET" in e for e in errs))

    def test_stripe_secret_bad_prefix_rejected(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "STRIPE_PRICE_ID_TRADERS_MONTHLY": "price_abc",
            "STRIPE_SECRET_KEY": "oops_wrong_prefix_abc123",
            "STRIPE_WEBHOOK_SECRET": "whsec_ok",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertTrue(any("STRIPE_SECRET_KEY" in e for e in errs))

    # ── Optional shape checks ────────────────────────────────────────

    def test_optional_skipped_when_unset(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "SENTRY_TRACES_SAMPLE_RATE": "",
            "ANTHROPIC_API_KEY": "",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertFalse(any("SENTRY_TRACES_SAMPLE_RATE" in e for e in errs))
        self.assertFalse(any("ANTHROPIC_API_KEY" in e for e in errs))

    def test_optional_bad_shape_rejected(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "SENTRY_TRACES_SAMPLE_RATE": "not-a-float",
            "LOG_LEVEL": "VERBOSE",  # not in allowed set
            "GLOBAL_RATE_LIMIT_PER_MIN": "-1",
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertTrue(any("SENTRY_TRACES_SAMPLE_RATE" in e for e in errs))
        self.assertTrue(any("LOG_LEVEL" in e for e in errs))
        self.assertTrue(any("GLOBAL_RATE_LIMIT_PER_MIN" in e for e in errs))

    def test_sentry_rate_out_of_range_rejected(self):
        vars_to_touch = {
            "PRODUCTION": "0",
            "SITE_ACCESS_TOKEN": "x" * 32,
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
            "SENTRY_TRACES_SAMPLE_RATE": "1.5",  # > 1.0
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        errs = config.validate_config()
        self.assertTrue(any("SENTRY_TRACES_SAMPLE_RATE" in e for e in errs))

    # ── Production exits on error ────────────────────────────────────

    def test_production_with_errors_sys_exits(self):
        vars_to_touch = {
            "PRODUCTION": "1",
            "SITE_ACCESS_TOKEN": "",  # missing, so an error will be raised
            "CREDENTIALS_ENCRYPTION_KEY": "x" * 32,
            "GATEWAY_COOKIE_SECRET": "x" * 32,
        }
        self.tracked = _env(vars_to_touch)
        _set(vars_to_touch)
        with self.assertRaises(SystemExit) as cm:
            config.validate_config()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
