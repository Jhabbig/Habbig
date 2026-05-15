# Auth / Session / Token Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_auth*.py gateway/tests/test_session*.py gateway/tests/test_gate*.py gateway/tests/test_token*.py gateway/tests/test_login*.py --tb=line -q -p no:logging
```

**Note:** The `test_gate*.py` and `test_login*.py` globs matched no files in `gateway/tests/`. Gate/login behaviour is exercised indirectly through `test_auth_flow.py` and the session/token suites listed below.

## Result

**90 passed, 3 skipped, 0 failed** (~21s)

## Files exercised

- `gateway/tests/test_auth_flow.py` — 51 tests
- `gateway/tests/test_session_cookies.py` — 13 tests
- `gateway/tests/test_sessions_management.py` — 8 tests
- `gateway/tests/test_token_first_auth.py` — 21 tests

Total: 93 collected, 90 passed, 3 skipped.

## Raw tail

```
platform darwin -- Python 3.9.6, pytest-8.3.0, pluggy-1.6.0
rootdir: /Users/shocakarel/Habbig/gateway
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.24.0, cov-7.1.0, respx-0.23.1

........................................s.......ss...................... [ 77%]
.....................                                                    [100%]
=============================== warnings summary ===============================
tests/test_auth_flow.py: 22 warnings
tests/test_token_first_auth.py: 3 warnings
  /Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_client.py:812: DeprecationWarning: Setting per-request cookies=<...> is being deprecated, because the expected behaviour on cookie persistence is ambiguous. Set cookies directly on the client instance instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
90 passed, 3 skipped, 25 warnings in 21.31s
```

The only non-pass results are 3 explicit `pytest.skip` markers in the suite — no failures or errors.
