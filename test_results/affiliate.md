# Affiliate Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_affiliate*.py --tb=line -q -p no:logging
```

## Summary

- **Passed:** 32
- **Failed:** 0
- **Skipped:** 0
- **Duration:** 66.07s

## Status: PASS

All affiliate tests pass.

## Coverage

- `gateway/tests/test_affiliate.py` — 32 tests covering affiliate flows

## Notes

- 11 `DeprecationWarning`s emitted by `httpx` regarding per-request `cookies=<...>` usage. Non-blocking; suggest setting cookies on the client instance to silence.
- One `PytestDeprecationWarning` for unset `asyncio_default_fixture_loop_scope` in pytest-asyncio config. Non-blocking.
