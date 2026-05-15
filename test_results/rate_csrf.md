# Rate-Limit & CSRF Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_rate_limit*.py gateway/tests/test_csrf*.py --tb=line -q -p no:logging
```

## Result

**45 passed, 0 failed** (1.21s)

## Files exercised

- `gateway/tests/test_rate_limiting.py` — 14 tests
- `gateway/tests/test_csrf.py` — 31 tests

## Raw tail

```
platform darwin -- Python 3.9.6, pytest-8.3.0, pluggy-1.6.0
rootdir: /Users/shocakarel/Habbig/gateway
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.24.0, cov-7.1.0, respx-0.23.1
asyncio: mode=strict, default_loop_scope=None
collected 45 items

gateway/tests/test_rate_limiting.py ..............                       [ 31%]
gateway/tests/test_csrf.py ...............................               [100%]

============================== 45 passed in 1.21s ==============================
```
