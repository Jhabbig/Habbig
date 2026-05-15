# Password Reset Tests

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_password*.py gateway/tests/test_reset*.py -q -p no:logging 2>&1 | tail -30
```

**Note:** The `test_reset*.py` glob matched no files in `gateway/tests/`. Only `test_password_reset.py` was collected.

## Result

**7 passed, 1 failed** (~8.7s)

## Files exercised

- `gateway/tests/test_password_reset.py` — 8 tests

## Failure

`TestSessionInvalidationOnReset::test_jwt_invalidated_before_bumped` —
`sqlite3.OperationalError: table sessions has no column named token` at
`gateway/queries/auth.py:224` (`create_session`). Test fixture/schema drift:
the `sessions` table in the test DB is missing the `token` column that
`create_session` writes to.

## Raw tail

```
......F.                                                                 [100%]
=================================== FAILURES ===================================
______ TestSessionInvalidationOnReset.test_jwt_invalidated_before_bumped _______

self = <tests.test_password_reset.TestSessionInvalidationOnReset testMethod=test_jwt_invalidated_before_bumped>

    def test_jwt_invalidated_before_bumped(self):
        uid = db.create_user("jwt@test.com", "InitialPass123!", username="jwtuser")
        # Create a session, then simulate a reset completing.
>       db.create_session(uid)

gateway/tests/test_password_reset.py:109:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

user_id = 4

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
FAILED gateway/tests/test_password_reset.py::TestSessionInvalidationOnReset::test_jwt_invalidated_before_bumped
1 failed, 7 passed in 8.66s
```
