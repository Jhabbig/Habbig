# GDPR / Data Export Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_data_export*.py gateway/tests/test_gdpr*.py gateway/tests/test_account_delete*.py --tb=line -q -p no:logging
```

## Result

**21 passed, 14 skipped in 2.55s**

## Discovery

Of the three glob patterns, only `test_data_export*.py` matched. No `test_gdpr*.py` or `test_account_delete*.py` files exist in `gateway/tests/`.

Files exercised:
- `gateway/tests/test_data_export.py` — 35 items collected (21 passed, 14 skipped)

## Output

```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.3.0, pluggy-1.6.0
rootdir: /Users/shocakarel/Habbig/gateway
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.24.0, cov-7.1.0, respx-0.23.1
asyncio: mode=strict, default_loop_scope=None
collected 35 items

gateway/tests/test_data_export.py .....ssssss................ssssssss    [100%]

======================== 21 passed, 14 skipped in 2.55s ========================
```

## Summary

- Pass: 21
- Fail: 0
- Skipped: 14
- Errors: 0
