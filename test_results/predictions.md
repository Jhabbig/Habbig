# Predictions & Extraction Tests

Date: 2026-05-15

## Command

```bash
python3 -m pytest gateway/tests/test_predictions*.py gateway/tests/test_extraction*.py -q -p no:logging
```

## Result

**0 passed, 0 failed, 29 skipped**

- Pass: 0
- Fail: 0
- Skipped: 29

## Notes

- No files match `gateway/tests/test_predictions*.py` or `gateway/tests/test_extraction*.py`.
- The closest existing file is `gateway/tests/test_user_predictions.py`, which collects 29 tests — all 29 skip under the current configuration.
- No extraction-prefixed test module exists in `gateway/tests/`.

## Breakdown

| File | Tests | Result |
|---|---|---|
| `gateway/tests/test_predictions*.py` | 0 | no match |
| `gateway/tests/test_extraction*.py` | 0 | no match |
| `gateway/tests/test_user_predictions.py` (closest match) | 29 | all skipped |
| **Total collected by literal pattern** | **0** | — |
