# Alert Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_alert*.py -q -p no:logging
```

**Note:** The `test_alert*.py` glob matched no files in `gateway/tests/` — the command as given errors out with `file or directory not found` and `no tests ran in 0.05s`. The only alert-related test file in the suite is `test_admin_cost_alerts.py`. Running that file explicitly is captured below for reference.

## Result (literal command as given)

**0 passed, 0 failed, 0 errors — pytest collection error (no matching files).**

## Result (resolved alert tests: `test_admin_cost_alerts.py`)

**0 passed, 11 errors** — all errors are pre-existing setup failures, not regressions caused by alert logic.

Every test errors inside the test class setup at `gateway/tests/test_admin_cost_alerts.py:57` calling `db.create_session(user_id)`, which raises:

```
sqlite3.OperationalError: table sessions has no column named token
```

The test's schema fixture is out of sync with `gateway/queries/auth.py:224` which expects a `token` column in `sessions`.

## Errored tests (11)

- `test_kill_switch_requires_csrf_token`
- `test_kill_switch_requires_super_admin`
- `test_kill_switch_toggles_with_valid_csrf`
- `test_page_admin_200_renders_sections`
- `test_page_anonymous_denied`
- `test_page_empty_state`
- `test_page_non_admin_403`
- `test_per_feature_breakdown_sums_to_total`
- `test_refresh_api_anonymous_403`
- `test_refresh_api_non_admin_403`
- `test_refresh_api_returns_expected_shape`

## Raw tail

```
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
gateway/tests/test_admin_cost_alerts.py:57: in _create_admin_session
    token = db.create_session(user_id)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

user_id = 1

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
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_kill_switch_requires_csrf_token
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_kill_switch_requires_super_admin
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_kill_switch_toggles_with_valid_csrf
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_page_admin_200_renders_sections
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_page_anonymous_denied
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_page_empty_state
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_page_non_admin_403
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_per_feature_breakdown_sums_to_total
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_refresh_api_anonymous_403
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_refresh_api_non_admin_403
ERROR gateway/tests/test_admin_cost_alerts.py::AdminCostAlertsTestCase::test_refresh_api_returns_expected_shape
```
