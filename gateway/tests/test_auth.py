import time

import db


# ── Password hashing ─────────────────────────────────────────────────────────


def test_password_hash_roundtrip():
    pwd_hash, salt = db._hash_password("correct horse battery staple")
    assert db.verify_password("correct horse battery staple", pwd_hash, salt)


def test_wrong_password_rejected():
    pwd_hash, salt = db._hash_password("hunter2hunter2")
    assert not db.verify_password("hunter3hunter3", pwd_hash, salt)


def test_tampered_hash_rejected():
    pwd_hash, salt = db._hash_password("hunter2hunter2")
    tampered = ("0" if pwd_hash[0] != "0" else "1") + pwd_hash[1:]
    assert not db.verify_password("hunter2hunter2", tampered, salt)


def test_tampered_salt_rejected():
    pwd_hash, salt = db._hash_password("hunter2hunter2")
    tampered_salt = ("0" if salt[0] != "0" else "1") + salt[1:]
    assert not db.verify_password("hunter2hunter2", pwd_hash, tampered_salt)


def test_verify_user_password_against_db(fresh_db):
    db.create_user("alice@example.com", "s3cret-password")
    assert db.verify_user_password("alice@example.com", "s3cret-password")
    assert not db.verify_user_password("alice@example.com", "wrong-password")
    assert not db.verify_user_password("nobody@example.com", "s3cret-password")


# ── Subscription expiry boundaries ───────────────────────────────────────────


def _make_sub(user_email: str, expires_at) -> int:
    uid = db.create_user(user_email, "pw-123456789")
    db.upsert_subscription(uid, "crypto", "monthly", duration_days=30, source="stripe", stripe_sub_id=f"sub_{uid}")
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET expires_at = ? WHERE user_id = ?",
            (expires_at, uid),
        )
    return uid


def test_subscription_just_valid(fresh_db):
    uid = _make_sub("valid@example.com", int(time.time()) + 5)
    assert db.has_active_subscription(uid, "crypto")


def test_subscription_just_expired(fresh_db):
    uid = _make_sub("expired@example.com", int(time.time()) - 1)
    assert not db.has_active_subscription(uid, "crypto")


def test_subscription_expiring_exactly_now_is_inactive(fresh_db):
    uid = _make_sub("boundary@example.com", int(time.time()))
    assert not db.has_active_subscription(uid, "crypto")


def test_subscription_without_expiry_is_active(fresh_db):
    uid = _make_sub("forever@example.com", None)
    assert db.has_active_subscription(uid, "crypto")


def test_cancelled_subscription_is_inactive(fresh_db):
    uid = _make_sub("cancelled@example.com", int(time.time()) + 86400)
    db.cancel_subscription(uid, "crypto")
    assert not db.has_active_subscription(uid, "crypto")


def test_other_dashboard_not_covered(fresh_db):
    uid = _make_sub("scoped@example.com", int(time.time()) + 86400)
    assert not db.has_active_subscription(uid, "sports")
