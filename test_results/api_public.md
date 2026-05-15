# Public API Tests

Date: 2026-05-15

## Command

```bash
python3 -m pytest gateway/tests/test_api_public*.py gateway/tests/test_api_keys*.py --tb=line -q -p no:logging
```

## Result

**44 passed in 6.63s**

- Pass: 44
- Fail: 0

## Breakdown

| File | Tests |
|---|---|
| `gateway/tests/test_api_keys_management.py` | 17 |
| `gateway/tests/test_api_public.py` | 15 |
| `gateway/tests/test_api_public_polish.py` | 12 |
| **Total** | **44** |
