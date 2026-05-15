# Trading Add-on Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_trading*.py gateway/tests/test_addon*.py --tb=line -q -p no:logging`

## Result

**12 passed, 15 failed** in 5.45s

Note: the original glob `gateway/tests/test_addon*.py` matched no files in `gateway/tests/`. The two relevant files actually present are:

- `gateway/tests/test_trading_addon.py`
- `gateway/tests/test_settings_trading_addon.py`

Effective command used:

```bash
python3 -m pytest gateway/tests/test_trading_addon.py gateway/tests/test_settings_trading_addon.py --tb=line -p no:logging
```

## Summary

- **Passed:** 12 (all of `test_trading_addon.py` + 3 validation tests in `test_settings_trading_addon.py`)
- **Failed:** 15 (all in `test_settings_trading_addon.py`)

## Failure Root Cause

Every failure in `test_settings_trading_addon.py` traces to the same SQLite error:

```
gateway/queries/auth.py:224: sqlite3.OperationalError: table sessions has no column named token
```

This is a test-DB schema mismatch — the `sessions` table in the test fixture is missing the `token` column that `gateway/queries/auth.py` expects when creating authenticated sessions for the test client. Tests that don't go through session creation (the pure-validation `TestConfigPatch::test_patch_validates_*` cases that check 401/422 without auth) pass; tests that need an authenticated session fail.

## Failed Tests

```
test_settings_trading_addon.py::TestPageRender::test_renders_empty_state_when_not_subscribed
test_settings_trading_addon.py::TestPageRender::test_renders_when_subscribed
test_settings_trading_addon.py::TestConfigGet::test_get_active_for_trader
test_settings_trading_addon.py::TestConfigGet::test_get_inactive_for_plain_user
test_settings_trading_addon.py::TestConfigPatch::test_patch_accepts_max_cap_boundaries
test_settings_trading_addon.py::TestConfigPatch::test_patch_accepts_zero_daily_cap
test_settings_trading_addon.py::TestConfigPatch::test_patch_null_clears_optional_limits
test_settings_trading_addon.py::TestConfigPatch::test_patch_requires_addon
test_settings_trading_addon.py::TestConfigPatch::test_patch_requires_csrf
test_settings_trading_addon.py::TestConfigPatch::test_patch_round_trip
test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_currency
test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_daily_cap_negative
test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_kelly_fraction
test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_max_cap_lower_bound
test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_max_cap_upper_bound
```

## Fix

The test fixture that builds the in-memory test DB needs to add the `token` column to its `sessions` table definition to match the production schema referenced by `gateway/queries/auth.py:224`.
