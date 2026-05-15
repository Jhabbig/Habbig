# Billing + Stripe + Subscription Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_billing*.py gateway/tests/test_stripe*.py gateway/tests/test_subscription*.py --tb=line -q -p no:logging
```

**Note:** No `test_subscription*.py` files exist in `gateway/tests/`. The shell glob expansion failed on the unmatched pattern, so tests were invoked with the explicit file list below.

## Files run

- `gateway/tests/test_billing_portal.py`
- `gateway/tests/test_settings_billing.py`
- `gateway/tests/test_stripe_webhook_hardening.py`
- `gateway/tests/test_stripe_webhook_route.py`

## Summary

- **Passed:** 95
- **Failed:** 2
- **Total:** 97

## Failures

1. `gateway/tests/test_stripe_webhook_route.py::TestStripeWebhookRoute::test_duplicate_event_id_is_idempotent`
   - `AssertionError: 'ok' != 'already_processed'`
2. `gateway/tests/test_stripe_webhook_route.py::TestStripeWebhookRoute::test_subscription_created_writes_row`
   - `sqlite3.OperationalError: no such table: users`

## Warnings

- `pytest-asyncio` deprecation: `asyncio_default_fixture_loop_scope` unset.
- `httpx` per-request `cookies=` deprecation (15 + 35 occurrences across `test_billing_portal.py` and `test_settings_billing.py`).
