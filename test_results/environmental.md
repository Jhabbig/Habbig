# Environmental Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_environmental*.py gateway/tests/test_climate*.py -q -p no:logging 2>&1 | tail -30
```

## Summary

- **Passed:** 36
- **Errored:** 15
- **Failed:** 0

## Status: FAIL

`test_environmental.py` (36 tests) all pass. `test_environmental_http.py` (15 tests) all error at `setUpClass`: `sqlite3.OperationalError: table sessions has no column named token` — the test fixture's `_fake_conn` schema is out of sync with `gateway/queries/auth.py::create_session` which expects a `token` column on `sessions`.

No `test_climate*.py` files exist under `gateway/tests/`.

## Coverage

- `gateway/tests/test_environmental.py` — 36 passed
- `gateway/tests/test_environmental_http.py` — 15 errored (setup)

## Raw output (tail -30)

```
user_id = 5

    def create_session(user_id: int) -> str:
        token = secrets.token_urlsafe(48)
        now = int(time.time())
        with db.conn() as c:
>           c.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, now + SESSION_TTL),
            )
E           sqlite3.OperationalError: table sessions has no column named token

gateway/queries/auth.py:224: OperationalError
=========================== short test summary info ============================
ERROR gateway/tests/test_environmental_http.py::TestProTierGating::test_force_refresh_requires_pro
ERROR gateway/tests/test_environmental_http.py::TestProTierGating::test_get_environmental_requires_pro
ERROR gateway/tests/test_environmental_http.py::TestProTierGating::test_top_endpoint_requires_pro
ERROR gateway/tests/test_environmental_http.py::TestForceRefreshRateLimit::test_sixth_force_refresh_returns_429
ERROR gateway/tests/test_environmental_http.py::TestPreferencesPatch::test_patch_unauthenticated_returns_401
ERROR gateway/tests/test_environmental_http.py::TestPreferencesPatch::test_patch_with_invalid_unit_returns_400
ERROR gateway/tests/test_environmental_http.py::TestPreferencesPatch::test_patch_with_valid_unit_returns_200
ERROR gateway/tests/test_environmental_http.py::TestUnifiedMergeWithEnv::test_pro_user_no_cached_row_no_block
ERROR gateway/tests/test_environmental_http.py::TestUnifiedMergeWithEnv::test_pro_user_pref_unit_applies_to_merged_block
ERROR gateway/tests/test_environmental_http.py::TestUnifiedMergeWithEnv::test_pro_user_with_cached_env_sees_merged_block
ERROR gateway/tests/test_environmental_http.py::TestUnifiedMergeWithEnv::test_pro_user_with_env_show_off_no_block
ERROR gateway/tests/test_environmental_http.py::TestUnifiedMergeWithEnv::test_trader_user_never_sees_env_block
ERROR gateway/tests/test_environmental_http.py::TestEnvRelevantFilter::test_env_relevant_filter_with_cache_prunes_other_markets
ERROR gateway/tests/test_environmental_http.py::TestEnvRelevantFilter::test_env_relevant_filter_with_no_cache_returns_all
ERROR gateway/tests/test_environmental_http.py::TestEnvRelevantFilter::test_no_filter_returns_all_markets
```
