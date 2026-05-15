# Logging & Observability Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_logging*.py gateway/tests/test_observability*.py -q -p no:logging`

## Summary

- **Passed:** 20
- **Failed:** 0
- **Total:** 20
- **Duration:** 26.68s

## Output (tail)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
....................                                                     [100%]
20 passed in 26.68s
```

## Notes

- No `test_observability*.py` files exist in `gateway/tests/`; the glob matched only `test_logging.py`.
- All 20 tests in `test_logging.py` passed.
