# Push Notification Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_push*.py gateway/tests/test_notification*.py gateway/tests/test_vapid*.py --tb=line -q -p no:logging`

Note: no `test_vapid*.py` files exist in `gateway/tests/`. Resolved to `test_push_routes.py` and `test_notifications.py`.

## Summary

- **Passed:** 14
- **Skipped:** 29
- **Failed:** 8
- **Total:** 51
- **Duration:** ~2.7s

## Tests

### gateway/tests/test_push_routes.py

| Test | Result |
|---|---|
| `TestIsAllowedPushHost::test_android_fcm_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_apple_api_push_accepted` | PASS |
| `TestIsAllowedPushHost::test_apple_webpush_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_fcm_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_http_scheme_rejected` | PASS |
| `TestIsAllowedPushHost::test_lookalike_host_rejected` | PASS |
| `TestIsAllowedPushHost::test_malformed_url_rejected` | PASS |
| `TestIsAllowedPushHost::test_mozilla_autopush_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_mozilla_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_push_googleapis_endpoint_accepted` | PASS |
| `TestIsAllowedPushHost::test_random_https_host_rejected` | PASS |
| `TestIsAllowedPushHost::test_wns_bare_suffix_rejected` | PASS |
| `TestIsAllowedPushHost::test_wns_wildcard_suffix_accepted` | PASS |
| `TestSubscribeRouteAllowlist::test_http_scheme_rejected_with_400` | FAIL |
| `TestSubscribeRouteAllowlist::test_malformed_endpoint_rejected` | FAIL |
| `TestSubscribeRouteAllowlist::test_random_https_host_rejected_with_422` | FAIL |
| `TestPushTestRoute::test_missing_csrf_token_rejected` | FAIL |
| `TestPushTestRoute::test_no_subscriptions_returns_zero_counts` | FAIL |
| `TestPushTestRoute::test_push_not_available_returns_503` | FAIL |
| `TestPushTestRoute::test_unauthenticated_rejected` | PASS |
| `TestPushTestRoute::test_unexpected_exception_returns_500` | FAIL |
| `TestPushTestRoute::test_with_mock_subscription_builds_payload_without_error` | FAIL |

### gateway/tests/test_notifications.py

| Test | Result |
|---|---|
| `TestMigration::test_notifications_table_exists` | SKIP |
| `TestMigration::test_preferences_table_exists` | SKIP |
| `TestMigration::test_unread_partial_index_exists` | SKIP |
| `TestDbHelpers::test_archive_hides_from_main_view` | SKIP |
| `TestDbHelpers::test_create_and_get` | SKIP |
| `TestDbHelpers::test_delete_scoped_to_owner` | SKIP |
| `TestDbHelpers::test_pagination_before_id` | SKIP |
| `TestDbHelpers::test_unknown_type_coerced_to_system` | SKIP |
| `TestDbHelpers::test_unread_count_and_mark_read` | SKIP |
| `TestDbHelpers::test_user_isolation` | SKIP |
| `TestPreferences::test_defaults_all_on` | SKIP |
| `TestPreferences::test_inapp_off_blocks_all` | SKIP |
| `TestPreferences::test_patch_merge` | SKIP |
| `TestPreferences::test_type_gate_blocks_insert` | SKIP |
| `TestSseBroadcast::test_broadcast_reaches_subscriber` | SKIP |
| `TestSseBroadcast::test_no_broadcast_when_opted_out` | SKIP |
| `TestSseBroadcast::test_other_users_subscription_not_hit` | SKIP |
| `TestHttpRoutes::test_archive_hides_from_list` | SKIP |
| `TestHttpRoutes::test_delete_scoped_to_owner` | SKIP |
| `TestHttpRoutes::test_list_requires_auth` | SKIP |
| `TestHttpRoutes::test_list_returns_users_rows` | SKIP |
| `TestHttpRoutes::test_mark_all_read` | SKIP |
| `TestHttpRoutes::test_mark_read_flow` | SKIP |
| `TestHttpRoutes::test_notifications_page_renders_for_authed_user` | SKIP |
| `TestHttpRoutes::test_notifications_page_requires_auth` | SKIP |
| `TestHttpRoutes::test_preferences_get_and_patch` | SKIP |
| `TestHttpRoutes::test_unread_count_endpoint` | SKIP |
| `TestHttpRoutes::test_user_cannot_see_others_notifications` | SKIP |
| `TestMarketResolutionIntegration::test_sends_inapp_alongside_email` | SKIP |

## Failure root cause

All 8 failures share the same error at `gateway/queries/auth.py:232`:

```
sqlite3.OperationalError: table sessions has no column named token
```

The failing tests are the ones that hit authenticated routes (subscribe allowlist and push-test); the tests in `TestIsAllowedPushHost` (pure-helper tests) all pass. The auth helper appears to insert into a `sessions.token` column that the test-fixture schema doesn't have, suggesting a migration mismatch between the test DB and `queries/auth.py`.

A first run (cold cache) reported 22 passed / 29 skipped / 0 failed, but every subsequent run produced 8 failed / 14 passed / 29 skipped. The 14/29/8 numbers above are the stable steady state.

## Raw output

```
.............FFFFFF.FF                 [ 43%]
sssssssssssssssssssssssssssss        [100%]

=================================== FAILURES ===================================
/Users/shocakarel/Habbig/gateway/queries/auth.py:232: sqlite3.OperationalError: table sessions has no column named token
(x8)
=================== 8 failed, 14 passed, 29 skipped in 2.73s ===================
```
