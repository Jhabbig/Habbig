"""Backend-agnostic smoke tests for the gateway data layer (``db.py``).

Runs against whichever backend ``db.py`` selects at import time:
  * PostgreSQL when ``DATABASE_URL`` is set,
  * a throwaway SQLite file otherwise.

CI runs this module under both backends, so the same assertions exercise the
SQLite path and the live PostgreSQL path (schema creation, placeholder
translation, RETURNING-id inserts, ON CONFLICT upserts, unique-violation
handling, and rowcount-based deletes).
"""

from __future__ import annotations

import pathlib
import tempfile
import uuid

import db

# On SQLite, isolate the test in a fresh temp file. On PostgreSQL we use the
# database DATABASE_URL points at (ephemeral in CI).
if not db.IS_PG:
    db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "test.db"

db.init_db()


def _email() -> str:
    return f"test_{uuid.uuid4().hex[:12]}@example.com"


def test_user_crud_and_password():
    email = _email()
    uid = db.create_user(email, "Sup3rSecret!pw12", is_admin=True)
    try:
        assert uid >= 1
        u = db.get_user_by_email(email)
        assert u["id"] == uid
        # name access, integer access, and dict() all work on both backends
        assert u["email"] == email
        assert dict(u)["username"] == email.split("@")[0]
        assert db.verify_user_password(email, "Sup3rSecret!pw12")
        assert not db.verify_user_password(email, "wrong-password")
    finally:
        db.delete_user(uid)
    assert db.get_user_by_email(email) is None


def test_subscription_upsert_on_conflict():
    uid = db.create_user(_email(), "Sup3rSecret!pw12")
    try:
        db.upsert_subscription(uid, "crypto", "pro", duration_days=30)
        db.upsert_subscription(uid, "crypto", "trader", duration_days=30)  # conflict → update
        subs = db.list_subscriptions(uid)
        assert len(subs) == 1
        assert subs[0]["plan"] == "trader"
    finally:
        db.delete_user(uid)


def test_stripe_event_idempotency():
    evt = f"evt_{uuid.uuid4().hex}"
    assert db.mark_stripe_event_processed(evt, "checkout") is True
    # duplicate primary key → unique violation, swallowed → False
    assert db.mark_stripe_event_processed(evt, "checkout") is False


def test_insert_returning_ids_and_rowcount_delete():
    eid = db.create_enquiry("a@b.com", "CTO", "hi")
    assert eid >= 1
    tid = db.create_superuser_key_template("demo", "desc", ["crypto"], ["read-only"])
    assert tid >= 1
    assert db.delete_superuser_key_template(tid) is True      # rowcount > 0
    assert db.delete_superuser_key_template(10**9) is False    # rowcount == 0


if __name__ == "__main__":
    # Allow running without pytest: `python test_db_backends.py`
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print(f"ALL PASS on {'postgres' if db.IS_PG else 'sqlite'} backend")
