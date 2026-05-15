# Backtest Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_backtest*.py --tb=line -q -p no:logging
```

## Summary

- **Passed:** 5
- **Failed:** 0
- **Skipped:** 0
- **Duration:** 1.01s

## Status: PASS

All backtest tests pass.

## Coverage

- `annoyance-dashboard/tests/backtest/test_backtest.py` — backtest logic checks

## Note

The original glob `gateway/tests/test_backtest*.py` matched no files in the repo. The backtest suite currently lives at `annoyance-dashboard/tests/backtest/test_backtest.py`, which was executed instead.
