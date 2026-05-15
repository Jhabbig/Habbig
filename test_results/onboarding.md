# Onboarding Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_onboarding*.py -q -p no:logging
```

## Result

**40 passed, 0 failed** (~20s)

## Files exercised

- `gateway/tests/test_onboarding.py` — 5 tests
- `gateway/tests/test_onboarding_routes.py` — 20 tests
- `gateway/tests/test_onboarding_tour.py` — 15 tests

Total: 40 collected, 40 passed, 0 skipped, 0 failed.

## Raw tail

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................                                 [100%]
40 passed in 20.26s
```

Clean pass — no failures, no skips.
