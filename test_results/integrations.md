# Integration Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_polymarket*.py gateway/tests/test_kalshi*.py gateway/tests/test_siwe*.py gateway/tests/test_integration*.py --tb=line -q -p no:logging
```

## Result

**13 passed, 0 failed** (19 warnings, 14.90s)

## Notes

- Globs `test_kalshi*.py` and `test_integration*.py` matched no files in `gateway/tests/` (no standalone files with those prefixes); ran under bash `nullglob` so they were silently dropped rather than raising a "no matches" shell error.
- Globs `test_polymarket*.py` and `test_siwe*.py` both resolved to the single file `gateway/tests/test_polymarket_siwe.py`, which contains all 13 tests.
- Related integration files NOT covered by the specified glob set (for reference, not run): `test_admin_integrations.py`, `test_portfolio_integration.py`, `test_settings_integrations.py`, `gateway/tests/integration/test_admin_shell.py`, `gateway/tests/integration/test_error_handling.py`.

## Output (tail -40)

```
.............                                                            [100%]
=============================== warnings summary ===============================
tests/test_polymarket_siwe.py: 19 warnings
  /Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_client.py:812: DeprecationWarning: Setting per-request cookies=<...> is being deprecated, because the expected behaviour on cookie persistence is ambiguous. Set cookies directly on the client instance instead.
    warnings.warn(message, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
13 passed, 19 warnings in 14.90s
```
