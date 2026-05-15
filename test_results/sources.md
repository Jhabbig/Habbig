# Sources & Credibility Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_sources*.py gateway/tests/test_credibility*.py -q -p no:logging 2>&1 | tail -30
```

## Summary

- **Passed:** 22
- **Failed:** 0

## Status: PASS

All credibility tests pass. No `test_sources*.py` files exist in the suite; the glob matched zero files and pytest ran only `test_credibility*.py`.

## Files Collected

- `gateway/tests/test_credibility_dashboard.py`
- `gateway/tests/test_credibility_recompute.py`

## Output (tail -30)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
......................                                                   [100%]
```
