# Market & Portfolio Tests

Date: 2026-05-15

## Command

```bash
python3 -m pytest gateway/tests/test_market*.py gateway/tests/test_portfolio*.py gateway/tests/test_polymarket*.py gateway/tests/test_kalshi*.py --tb=line -q -p no:logging
```

Note: `test_kalshi*.py` glob matched no files (no kalshi test suite present yet).

## Result

**136 passed in 71.48s**

- Pass: 136
- Fail: 0

## Breakdown

| File | Tests |
|---|---|
| `gateway/tests/test_market_resolution.py` | 5 |
| `gateway/tests/test_market_takes.py` | 50 |
| `gateway/tests/test_markets.py` | 29 |
| `gateway/tests/test_polymarket_siwe.py` | 13 |
| `gateway/tests/test_portfolio_integration.py` | 30 |
| `gateway/tests/test_portfolio_sync.py` | 9 |
| **Total** | **136** |
