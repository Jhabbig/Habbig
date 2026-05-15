# Feedback Tests

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_feedback*.py --tb=line -q -p no:logging`

## Result

**49 passed, 0 failed** (87 warnings, 17.95s)

## Files Covered

- `gateway/tests/test_feedback.py`
- `gateway/tests/test_feedback_routes.py`

## Output

```
.................................................                        [100%]
=============================== warnings summary ===============================
tests/test_feedback_routes.py: 87 warnings
  /Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_client.py:812: DeprecationWarning: Setting per-request cookies=<...> is being deprecated, because the expected behaviour on cookie persistence is ambiguous. Set cookies directly on the client instance instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
49 passed, 87 warnings in 17.95s
```

## Notes

- All feedback tests pass.
- 87 deprecation warnings from httpx (per-request cookies deprecated; non-blocking).
- pytest-asyncio default fixture loop scope is unset (non-blocking deprecation).
