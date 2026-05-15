# Foundation / Bundle / PWA Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_foundation*.py gateway/tests/test_bundle*.py gateway/tests/test_pwa*.py --tb=line -q -p no:logging
```

**Note:** The `test_bundle*.py` glob matched no files. Bundle tests live in `test_foundation_bundle.py`, so they are covered by the foundation glob. PWA tests are in `test_pwa_v2.py`.

## Result

**77 passed, 0 failed** (1.89s)

## Files exercised

- `gateway/tests/test_foundation_bundle.py` — 34 tests
- `gateway/tests/test_pwa_v2.py` — 43 tests

## Raw tail

```
platform darwin -- Python 3.9.6, pytest-8.3.0, pluggy-1.6.0
rootdir: /Users/shocakarel/Habbig/gateway
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.24.0, cov-7.1.0, respx-0.23.1
collected 77 items

gateway/tests/test_foundation_bundle.py ................................ [ 41%]
..                                                                       [ 44%]
gateway/tests/test_pwa_v2.py ........................................... [100%]

============================== 77 passed in 1.89s ==============================
```
