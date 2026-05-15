# Forecast Tests

Date: 2026-05-15

## Command

```bash
python3 -m pytest gateway/tests/test_forecast*.py gateway/tests/test_external*.py -q -p no:logging 2>&1 | tail -30
```

## Result

**37 passed in 3.24s**

- Pass: 37
- Fail: 0

## Notes

- The glob `gateway/tests/test_forecast*.py` matched no files (no standalone
  `test_forecast*.py` exists). All 37 collected tests came from
  `gateway/tests/test_external_forecasts.py`, which matched
  `test_external*.py`.

## Breakdown

| File | Tests |
|---|---|
| `gateway/tests/test_external_forecasts.py` | 37 |
| **Total** | **37** |
