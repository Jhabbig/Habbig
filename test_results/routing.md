# Routing / Paths Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_routing*.py gateway/tests/test_paths*.py -q -p no:logging
```

## Result

**0 passed, 0 failed** — no tests collected.

Neither glob (`test_routing*.py` nor `test_paths*.py`) matches any file under `gateway/tests/`. Pytest reported `file or directory not found: gateway/tests/test_routing*.py` and exited with `no tests ran in 0.28s`.

## Files exercised

None. No matching test modules exist in `gateway/tests/`.

Adjacent routing-flavoured suites that do exist (not invoked by this command):

- `gateway/tests/test_export_routes.py`
- `gateway/tests/test_feature_routes.py`
- `gateway/tests/test_feedback_routes.py`
- `gateway/tests/test_intelligence_routes.py`
- `gateway/tests/test_onboarding_routes.py`
- `gateway/tests/test_protected_routes.py`
- `gateway/tests/test_push_routes.py`
- `gateway/tests/test_stripe_webhook_route.py`

## Raw tail

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_routing*.py


no tests ran in 0.28s
```
