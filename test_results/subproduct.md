# Subproduct / Middleware Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_subproduct*.py gateway/tests/test_middleware*.py -k "subproduct or middleware" --tb=line -q -p no:logging 2>&1 | tail -40
```

## Summary

- **Passed:** 59
- **Failed:** 0
- **Duration:** 0.66s

## Files Collected

- `gateway/tests/test_subproduct_middleware.py`
- `gateway/tests/test_subproduct_access.py`
- `gateway/tests/test_subproducts.py`
- `gateway/tests/test_subproduct_filters.py`
- `gateway/tests/e2e/test_subproduct_access_flow.py`

Note: No `test_middleware*.py` files exist in `gateway/tests/`; the glob expanded to nothing for that pattern. Subproduct test files include middleware coverage (`test_subproduct_middleware.py`).

## Output (tail -40)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
...........................................................              [100%]
59 passed in 0.66s
```
