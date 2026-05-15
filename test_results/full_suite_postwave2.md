# Full Suite — Post-Wave 2

- **Date:** 2026-05-15
- **Branch:** feature/platform-build (origin already up to date)
- **Command:** `python3 -m pytest gateway/tests/ --tb=no -q -p no:logging`
- **Environment:** Local macOS (Python 3.9), synchronous run
- **Exit code:** 1

## Totals

| Metric | Count |
|---|---|
| Collected | 3395 |
| Passed | 3069 |
| Failed | 147 |
| Errors | 1 |
| Skipped | 178 |
| xfailed / xpassed | 0 / 0 |

> Final pytest tally line was suppressed by an asyncio task-cleanup race writing to a closed stdout (`ValueError: I/O operation on closed file` for an in-process `InProcessBackend._run` task). Counts above were derived from the per-test progress glyphs and cross-checked against `^FAILED` / `^ERROR` line counts (147 / 1) — both match.

## Baseline comparison

| Snapshot | Fail count | Δ vs. previous |
|---|---|---|
| Start of day | 65 | — |
| After sweep | 38 | -27 |
| Post-Wave 2 (this run) | **147** | **+109 vs. post-sweep, +82 vs. start of day** |

Failures regressed significantly. Wave 2 work appears to have broken (or surfaced previously-skipped) tests across several billing/settings/notification/saved-views surfaces. Worth treating this as a stop-the-line signal before any further wave work.

## Top failing files (clusters worth investigating first)

| Failures | File |
|---:|---|
| 16 | `gateway/tests/test_settings_billing.py` |
| 13 | `gateway/tests/test_gift_subscription.py` |
| 12 | `gateway/tests/test_notifications.py` |
| 11 | `gateway/tests/test_admin_audit_log.py` |
|  9 | `gateway/tests/test_saved_views.py` |
|  8 | `gateway/tests/test_tier_change_cache.py` |
|  8 | `gateway/tests/test_api_public.py` |
|  7 | `gateway/tests/test_admin_users.py` |
|  5 | `gateway/tests/test_subproduct_signup_takeover.py` |
|  5 | `gateway/tests/test_referrals.py` |
|  5 | `gateway/tests/test_api_public_polish.py` |
|  5 | `gateway/tests/test_admin_newsletter.py` |

The single ERROR is in `gateway/tests/test_api_keys_management.py::TestSettingsCRUDRoundTrip::test_list_then_create_then_revoke` (collection/setup, not assertion).

## Notes

- Suite ran cleanly to completion under the harness timeout (no hangs).
- Hard rule respected: synchronous bash only; no background processes; no pre-release tooling touched.
- Raw output available at `/tmp/postwave2_pytest.out` on the runner machine (not committed).
