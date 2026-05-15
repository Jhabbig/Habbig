# Signal / Insider Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_signal*.py gateway/tests/test_insider*.py -q -p no:logging 2>&1 | tail -30
```

**Note:** Neither `test_signal*.py` nor `test_insider*.py` glob matched any files in `gateway/tests/`. No test files with those prefixes exist in the suite. Insider/signal behaviour lives in `gateway/insider_routes.py`, `gateway/jobs/insider_jobs.py`, `gateway/jobs/compute_churn_signals.py`, `gateway/backend/markets/portfolio_signals.py`, and `gateway/migrations/059_insider_signals.py` / `093_churn_signals.py`, but is not exercised by a dedicated test module under these prefixes.

## Result

**0 passed, 0 skipped, 0 failed** — pytest collected no tests (`ERROR: file or directory not found`).

## Raw tail

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_signal*.py


no tests ran in 0.01s
```

No tests executed. Consider adding `tests/test_insider_routes.py` and `tests/test_signals.py` to cover insider alerts / churn signals / portfolio signals modules.
