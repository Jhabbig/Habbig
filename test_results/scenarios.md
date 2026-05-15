# Scenarios & Kelly Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_scenario*.py gateway/tests/test_kelly*.py -q -p no:logging`

## Summary

- **Passed:** 40
- **Failed:** 0
- **Total:** 40
- **Duration:** 9.01s

## Output (tail)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................                                 [100%]
40 passed in 9.01s
```

## Notes

- Matched files: `gateway/tests/test_scenarios.py` and `gateway/tests/test_kelly.py`.
- All 40 tests passed.
