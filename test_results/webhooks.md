# Webhooks + Delivery + DLQ Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_webhook*.py gateway/tests/test_delivery*.py gateway/tests/test_dlq*.py --tb=line -q -p no:logging
```

## Summary

- **Passed:** 16
- **Failed:** 0
- **Duration:** 8.51s

## Files matched

- `gateway/tests/test_webhooks.py` (16 tests)
- `gateway/tests/test_delivery*.py` — no matching files
- `gateway/tests/test_dlq*.py` — no matching files

Note: `test_stripe_webhook_hardening.py` and `test_stripe_webhook_route.py` did not match the `test_webhook*.py` glob (they have a `test_stripe_` prefix). If they should be included, the glob would need to be `test_*webhook*.py`.

## Output (tail)

```
................                                                         [100%]
16 passed in 8.51s
```
