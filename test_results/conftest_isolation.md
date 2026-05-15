# Conftest Isolation Sanity Check

**Date:** 2026-05-15
**Scope:** Verify pytest fixture isolation in `gateway/tests/conftest.py`

## Test Run

Command:

```bash
python3 -m pytest gateway/tests/test_conftest_isolation.py gateway/tests/test_fixtures*.py --tb=line -q -p no:logging
```

**Result:** Target test files do **not** exist.

- `gateway/tests/test_conftest_isolation.py` — not present
- `gateway/tests/test_fixtures*.py` — no matches (shell glob expansion failed)

Only `gateway/tests/conftest.py` exists in the tests directory; there is no
dedicated `test_conftest_isolation.py` or `test_fixtures_*.py` module.

Pass / Fail / Error counts: **N/A — no tests collected** (0 passed / 0 failed / 0 errored).

## Fixture Verification (Fallback Per Instruction)

Per the task's fallback rule, verified the autouse `SITE_ACCESS_TOKEN` scrub
fixture in `/Users/shocakarel/Habbig/gateway/tests/conftest.py`:

**Status:** PRESENT and correctly scoped.

### Fixture Details

- **Name:** `_scrub_site_access_token`
- **Location:** `gateway/tests/conftest.py`, lines 73-100
- **Decorator:** `@pytest.fixture(autouse=True)` (function-scope; default)
- **Purpose:** Forces `server.SITE_ACCESS_TOKEN = ""` for every non-e2e test
  so the gate middleware does not 200-redirect admin/api requests that should
  be returning 403 from CSRF/auth middleware.

### Behavior

- Detects e2e tests via `request.node.module.__name__.startswith("tests.e2e")`.
- For non-e2e tests:
  - Saves `_prev = server.SITE_ACCESS_TOKEN`.
  - Sets `server.SITE_ACCESS_TOKEN = ""` before the test runs.
  - `yield` -> test executes.
  - Restores `server.SITE_ACCESS_TOKEN = _prev` after the test.
- For e2e tests: fixture yields without mutating; e2e suite's own
  `pass_gate` helper manages the token.

### Defense-in-Depth

In addition to the autouse fixture, `conftest.py` also pops the env var
at import time (line 266):

```python
_os.environ.pop("SITE_ACCESS_TOKEN", None)
```

This ensures the token is unset before `server.py` is imported, so the
module-level constant initialises to empty regardless of the host shell's
environment.

### Related Autouse Fixtures (Isolation Stack)

| Line | Fixture | Scope | Purpose |
| --- | --- | --- | --- |
| 48 | `_maybe_force_shared_testdb_class` | class | Re-pin shared in-memory DB before `setUpClass`. |
| 63 | `_maybe_force_shared_testdb` | function | Re-apply shared-conn patch for `_testdb` users. |
| 73 | `_scrub_site_access_token` | function | **SITE_ACCESS_TOKEN scrub (verified).** |
| 103 | `_clear_module_testclient_cookies` | function | Clear module-level `TestClient` cookies between tests. |
| 191 | (autouse) | function | Cross-test state hygiene (see file). |

## Conclusion

- The intended sanity tests are not in the repo.
- The autouse `_scrub_site_access_token` fixture is present, correctly
  decorated, properly scoped, and complemented by an import-time env pop.
- No isolation regression detected in `conftest.py` for the SITE_ACCESS_TOKEN
  surface.
