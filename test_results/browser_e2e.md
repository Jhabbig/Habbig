# Browser + E2E Test Results

**Date:** 2026-05-15
**Commands:**
```bash
python3 -m pytest gateway/tests/browser/ gateway/tests/e2e/ --tb=line -q -p no:logging --collect-only
python3 -m pytest gateway/tests/browser/ gateway/tests/e2e/ -k "not playwright and not selenium and not chrome and not firefox" --tb=line -q -p no:logging
```

## Summary

- **Collected:** 78 items
- **Deselected (require live browser):** 4 (firefox-marked)
- **Selected/Run:** 74
- **Passed:** 1
- **Failed:** 12
- **Errors:** 2
- **Skipped (no live browser available):** 59
- **Duration:** ~6s

## Collection breakdown

| Path | Test count |
|---|---|
| `gateway/tests/browser/test_critical_flows.py` | 12 |
| `gateway/tests/browser/test_mobile_quirks.py` | 4 |
| `gateway/tests/browser/test_visual_regression.py` | 48 |
| `gateway/tests/e2e/test_admin_impersonation_flow.py` | 1 |
| `gateway/tests/e2e/test_cancellation_flow.py` | 1 |
| `gateway/tests/e2e/test_data_export_flow.py` | 1 |
| `gateway/tests/e2e/test_leaderboard_flow.py` | 1 |
| `gateway/tests/e2e/test_login_logout_flow.py` | 1 |
| `gateway/tests/e2e/test_offline_flow.py` | 1 |
| `gateway/tests/e2e/test_onboarding_flow.py` | 1 |
| `gateway/tests/e2e/test_password_reset_flow.py` | 1 |
| `gateway/tests/e2e/test_prediction_submit_flow.py` | 1 |
| `gateway/tests/e2e/test_share_flow.py` | 1 |
| `gateway/tests/e2e/test_signup_flow.py` | 1 |
| `gateway/tests/e2e/test_subproduct_access_flow.py` | 1 |
| `gateway/tests/e2e/test_subscription_flow.py` | 1 |
| `gateway/tests/e2e/test_watchlist_flow.py` | 1 |

## Tests requiring a live browser (skipped / deselected)

### Deselected by `-k "not ... firefox"` (4)

Firefox-specific parametrizations of `test_critical_flows.py`:

| Test | Reason |
|---|---|
| `test_homepage_loads_on_every_engine[firefox]` | live firefox browser engine |
| `test_gate_form_is_usable[firefox]` | live firefox browser engine |
| `test_feature_detection_not_browser_sniffing[firefox]` | live firefox browser engine |
| `test_no_console_errors_on_homepage[firefox]` | live firefox browser engine |

### Skipped at runtime — `playwright not installed` (59)

| File | Skipped | Reason |
|---|---|---|
| `gateway/tests/browser/test_critical_flows.py` | 8 | playwright not installed |
| `gateway/tests/browser/test_mobile_quirks.py` | 3 | playwright not installed |
| `gateway/tests/browser/test_visual_regression.py` | 48 | playwright not installed |

(Total browser suite needing live browser: 4 deselected + 59 skipped = 63 of 64 browser tests.)

## Passed tests (1)

| Test | Result |
|---|---|
| `gateway/tests/browser/test_mobile_quirks.py::test_css_uses_dvh_not_raw_vh_for_hero_heights` | PASS |

This is the only test in the browser/e2e suite that runs without a live browser — it inspects CSS files statically.

## Failures (12) and errors (2)

All 14 failures/errors are E2E flows blocked by the same DB schema mismatch — not a browser issue. The test DB used by `gateway/tests/e2e/conftest.py` has no `token` column on `sessions`, but `gateway/queries/auth.py:224` writes one. This is the same schema drift logged in commit `7113d69` for the trading_addon run.

### Errors (setup) — 2

| Test | Error |
|---|---|
| `gateway/tests/e2e/test_admin_impersonation_flow.py::test_admin_impersonation_flow` | `sqlite3.OperationalError: table sessions has no column named token` (setup) |
| `gateway/tests/e2e/test_share_flow.py::test_share_flow` | `sqlite3.OperationalError: table sessions has no column named token` (setup) |

### Failures — 12

All fail with `sqlite3.OperationalError: table sessions has no column named token` at `gateway/queries/auth.py:224`:

| Test |
|---|
| `gateway/tests/e2e/test_cancellation_flow.py::test_cancellation_flow` |
| `gateway/tests/e2e/test_data_export_flow.py::test_data_export_flow` |
| `gateway/tests/e2e/test_leaderboard_flow.py::test_leaderboard_flow` |
| `gateway/tests/e2e/test_login_logout_flow.py::test_login_logout_flow` |
| `gateway/tests/e2e/test_offline_flow.py::test_offline_flow` |
| `gateway/tests/e2e/test_onboarding_flow.py::test_onboarding_flow` |
| `gateway/tests/e2e/test_password_reset_flow.py::test_password_reset_flow` |
| `gateway/tests/e2e/test_prediction_submit_flow.py::test_prediction_submit_flow` |
| `gateway/tests/e2e/test_signup_flow.py::test_signup_flow` |
| `gateway/tests/e2e/test_subproduct_access_flow.py::test_subproduct_access_flow` |
| `gateway/tests/e2e/test_subscription_flow.py::test_subscription_flow` |
| `gateway/tests/e2e/test_watchlist_flow.py::test_watchlist_flow` |

## Raw output

```
ssssssss.sssssssssssssssssssssssssssssssssssssssssssssssssssEFFFFFFFFEFF [ 97%]
FF                                                                       [100%]
==================================== ERRORS ====================================
_______________ ERROR at setup of test_admin_impersonation_flow ________________
E   sqlite3.OperationalError: table sessions has no column named token
______________________ ERROR at setup of test_share_flow _______________________
E   sqlite3.OperationalError: table sessions has no column named token
=================================== FAILURES ===================================
/Users/shocakarel/Habbig/gateway/queries/auth.py:224: sqlite3.OperationalError: table sessions has no column named token
  (x12)
=========================== short test summary info ============================
FAILED gateway/tests/e2e/test_cancellation_flow.py::test_cancellation_flow
FAILED gateway/tests/e2e/test_data_export_flow.py::test_data_export_flow
FAILED gateway/tests/e2e/test_leaderboard_flow.py::test_leaderboard_flow
FAILED gateway/tests/e2e/test_login_logout_flow.py::test_login_logout_flow
FAILED gateway/tests/e2e/test_offline_flow.py::test_offline_flow
FAILED gateway/tests/e2e/test_onboarding_flow.py::test_onboarding_flow
FAILED gateway/tests/e2e/test_password_reset_flow.py::test_password_reset_flow
FAILED gateway/tests/e2e/test_prediction_submit_flow.py::test_prediction_submit_flow
FAILED gateway/tests/e2e/test_signup_flow.py::test_signup_flow
FAILED gateway/tests/e2e/test_subproduct_access_flow.py::test_subproduct_access_flow
FAILED gateway/tests/e2e/test_subscription_flow.py::test_subscription_flow
FAILED gateway/tests/e2e/test_watchlist_flow.py::test_watchlist_flow
ERROR gateway/tests/e2e/test_admin_impersonation_flow.py::test_admin_impersonation_flow
ERROR gateway/tests/e2e/test_share_flow.py::test_share_flow
12 failed, 1 passed, 59 skipped, 4 deselected, 2 warnings, 2 errors in 6.11s
```
