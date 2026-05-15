# Profile + Referrals Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_profile*.py gateway/tests/test_handle*.py gateway/tests/test_referral*.py --tb=line -q -p no:logging
```

## Note on glob

`gateway/tests/test_handle*.py` matched no files (no handle test suite exists in
`gateway/tests/`). The glob was expanded by the shell to the actual files:

- `gateway/tests/test_profile.py`
- `gateway/tests/test_source_profiles.py`
- `gateway/tests/test_referrals.py`

## Summary

- **Passed:** 68
- **Failed:** 5
- **Total:** 73
- **Duration:** ~24s

## Failures

All 5 failures share the same root cause — schema drift in the test DB:

```
/Users/shocakarel/Habbig/gateway/queries/auth.py:224:
sqlite3.OperationalError: table sessions has no column named token
```

Failing tests (all in `gateway/tests/test_referrals.py`):

1. `TestReferralsApi::test_api_me_returns_code_and_stats`
2. `TestLeaderboardApi::test_api_leaderboard_returns_empty_when_no_participants_visible`
3. `TestLeaderboardApi::test_bad_display_name_returns_400`
4. `TestLeaderboardApi::test_participate_then_opt_out`
5. `TestLeaderboardApi::test_period_param_defaults_to_all_on_invalid`

## Diagnosis

The `sessions` table created by the test fixture is missing the `token`
column that `gateway/queries/auth.py:224` writes to. Likely a missed
migration in the test setup for these referral tests. Profile tests
(`test_profile.py`, `test_source_profiles.py`) are unaffected and all pass.
