# Follow + Share Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_follow*.py gateway/tests/test_share*.py -q -p no:logging`
**Working dir:** `/Users/shocakarel/Habbig`

## Result

**No tests ran — test files do not exist.**

- Passed: 0
- Failed: 0
- Errors: 1 (collection error: file or directory not found)
- Total: 0
- pytest exit code: 4 (usage error)

## Output (tail -30)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_follow*.py


no tests ran in 0.02s
```

## Notes

- `find /Users/shocakarel/Habbig -name "test_follow*.py" -o -name "test_share*.py"` returned only `gateway/tests/e2e/test_share_flow.py`, which is outside the requested glob (`gateway/tests/test_share*.py` does not recurse into `e2e/`).
- No `test_follow*.py` files exist anywhere in the repo.
- If follow/share unit coverage is required, new test modules need to be added at `gateway/tests/test_follow_*.py` and `gateway/tests/test_share_*.py`.
