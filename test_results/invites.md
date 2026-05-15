# Invite Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_invite*.py -q -p no:logging
```

**Note:** The `test_invite*.py` glob matched no files in `gateway/tests/`. Invite functionality exists in the codebase (`gateway/static/invite*.html`, `gateway/jobs/invite_replenish.py`, migrations `113_user_invite_tokens.py` and `188_fix_users_invite_token_fk.py`) but has no dedicated test module yet. Invite-token behaviour is exercised indirectly via the referral suite (see `test_results/profile_referrals.md`).

## Result

**0 passed, 0 failed, 0 skipped** — no tests collected (pytest exited with "file or directory not found").

## Files exercised

None — no test files matched the glob.

## Raw tail

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session"

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
ERROR: file or directory not found: gateway/tests/test_invite*.py


no tests ran in 0.08s
```

Recommend adding a `test_invite_tokens.py` suite covering token generation (`113_user_invite_tokens.py` schema), the public landing flow (`invite_public.html` / `invite_public.js`), and the `invite_replenish.py` job before this surface is shipped.
</content>
</invoke>