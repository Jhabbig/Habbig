# Middleware Tests

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_middleware*.py -q -p no:logging 2>&1 | tail -30`

## Result

**0 passed, 0 failed** — no tests ran (glob pattern matched no files).

The pattern `test_middleware*.py` matches files whose names begin with `test_middleware`. The repo has no such files; the existing middleware-related tests are `test_impersonation_middleware.py` and `test_subproduct_middleware.py`, which start with their feature prefix, not `test_middleware`. pytest receives the unexpanded literal and errors out.

## Output

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_middleware*.py


no tests ran in 0.03s
```

## Files covered

None. The two middleware-suffixed tests live under their feature prefixes and are covered elsewhere:

- `gateway/tests/test_impersonation_middleware.py` — see `test_results/impersonation.md`
- `gateway/tests/test_subproduct_middleware.py` — see `test_results/subproduct.md`
