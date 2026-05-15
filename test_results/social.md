# Social (Telegram + Discord) Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_telegram*.py gateway/tests/test_discord*.py -q -p no:logging`
**Working dir:** `/Users/shocakarel/Habbig`

## Result

**No tests ran — test files do not exist.**

- Passed: 0
- Failed: 0
- Errors: 1 (collection error: file or directory not found)
- Total: 0

## Output (tail -30)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_telegram*.py


no tests ran in 0.47s
```

## Notes

- `find /Users/shocakarel/Habbig -type f -name "test_telegram*" -o -name "test_discord*"` returned no results.
- No telegram or discord test modules currently exist in `gateway/tests/`.
- If social-channel coverage is required, new test files (e.g., `test_telegram_bot.py`, `test_discord_bot.py`) need to be added.
