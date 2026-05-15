# Email System Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_email*.py gateway/tests/test_resend*.py gateway/tests/test_unsubscribe*.py --tb=line -q -p no:logging
```

## Summary

- **Passed:** 50
- **Failed:** 3
- **Warnings:** 2
- **Duration:** 4.14s

## Test Files Collected

- `gateway/tests/test_admin_emails.py`
- `gateway/tests/test_admin_test_emails.py`
- `gateway/tests/test_email_system.py`
- `gateway/tests/test_email_template_overrides.py`
- `gateway/tests/test_email_watermark.py`
- `gateway/tests/test_email_welcome.py`

No matching files for `test_resend*.py` or `test_unsubscribe*.py` (glob expanded to nothing).

## Failures

All three failures are in `gateway/tests/test_email_watermark.py::TestAdminTraceRoute`:

- `test_route_400_on_garbage`
- `test_route_404s_on_unknown`
- `test_route_resolves_watermark`

### Root Cause

```
/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/asyncio/events.py:642: RuntimeError: There is no current event loop in thread 'MainThread'.
```

All three failures share the same `RuntimeError: There is no current event loop in thread 'MainThread'`, raised from the asyncio events module on Python 3.9. Likely environmental — the route handler tries to access `asyncio.get_event_loop()` without an active loop on the test thread.

## Warnings

```
tests/test_email_watermark.py::TestAdminTraceForensicAlerts::test_sentry_capture_also_on_miss
tests/test_email_watermark.py::TestAdminTraceForensicAlerts::test_sentry_capture_on_hit
  RuntimeWarning: coroutine 'InProcessBackend._run' was never awaited
```

Plus the standard `pytest-asyncio` `asyncio_default_fixture_loop_scope` deprecation warning.
