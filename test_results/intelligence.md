# Intelligence / Chat Module Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_intelligence*.py gateway/tests/test_chat*.py -q -p no:logging 2>&1 | tail -30
```

## Result

**63 passed, 0 failed** (~13.1s)

## Files exercised

- `gateway/tests/test_intelligence.py`
- `gateway/tests/test_intelligence_layer.py`
- `gateway/tests/test_intelligence_routes.py`

No `test_chat*.py` files exist in `gateway/tests/`; the glob expanded to nothing and pytest collected only the intelligence files.

Total: 63 collected, 63 passed, 0 failed.

## Raw tail

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
...............................................................          [100%]
63 passed in 13.09s
```
