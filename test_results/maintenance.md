# Maintenance Tests

Command:
```
python3 -m pytest gateway/tests/test_maintenance*.py gateway/tests/test_drill*.py gateway/tests/test_vacuum*.py -q -p no:logging 2>&1 | tail -30
```

Date: 2026-05-15

## Result

**0 passed / 0 failed** — pytest could not match any of the requested file patterns.

The globs `test_maintenance*.py`, `test_drill*.py`, and `test_vacuum*.py` returned no matches under `gateway/tests/`. The shell passed the literal glob strings to pytest, which then reported a file-not-found error. (Note: a related file `gateway/tests/test_db_maintenance.py` exists but does not match the requested `test_maintenance*` prefix.)

## Output

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_maintenance*.py


no tests ran in 0.04s
```
