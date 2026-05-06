"""Tests for the encrypted credential vault."""

import os
import tempfile
from pathlib import Path

import pytest

import credential_vault as vault
from tests._trade_fixtures import make_in_memory_conn_factory, make_test_rsa_key


@pytest.fixture(autouse=True)
def isolated_secret_key(tmp_path, monkeypatch):
    """Each test gets its own .secret_key so encrypted blobs from
    other tests can't decrypt under this test's key."""
    secret_path = tmp_path / "secret.key"
    monkeypatch.setenv("WEATHER_SECRET_KEY_PATH", str(secret_path))
    yield


def test_store_and_load_round_trip():
    factory, _ = make_in_memory_conn_factory()
    pem, pk = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "key-id-abc", pem)
    creds = vault.load_credentials(factory, "user-1")
    assert creds is not None
    assert creds.user_id == "user-1"
    assert creds.key_id == "key-id-abc"
    assert creds.is_demo is False
    # The decrypted private_key should sign things our test key can verify
    from kalshi_signing import sign_message, verify_signature
    sig = sign_message(creds.private_key, b"hello")
    assert verify_signature(pk.public_key(), b"hello", sig)


def test_load_returns_none_for_unknown_user():
    factory, _ = make_in_memory_conn_factory()
    assert vault.load_credentials(factory, "nobody") is None


def test_disable_then_load_returns_none():
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "k", pem)
    assert vault.disable_credentials(factory, "user-1") is True
    assert vault.load_credentials(factory, "user-1") is None
    # Idempotent — second call returns False (no row to update)
    assert vault.disable_credentials(factory, "user-1") is False


def test_credentials_exist_does_not_decrypt():
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "k", pem)
    assert vault.credentials_exist(factory, "user-1") is True
    vault.disable_credentials(factory, "user-1")
    assert vault.credentials_exist(factory, "user-1") is False


def test_store_replaces_on_re_enroll():
    factory, _ = make_in_memory_conn_factory()
    pem1, _ = make_test_rsa_key()
    pem2, _ = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "key-old", pem1)
    vault.store_credentials(factory, "user-1", "key-new", pem2)
    creds = vault.load_credentials(factory, "user-1")
    assert creds is not None
    assert creds.key_id == "key-new"


def test_re_enroll_clears_disabled_flag():
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "k", pem)
    vault.disable_credentials(factory, "user-1")
    vault.store_credentials(factory, "user-1", "k", pem)  # rotate
    assert vault.credentials_exist(factory, "user-1") is True


def test_store_rejects_garbage_pem():
    factory, _ = make_in_memory_conn_factory()
    with pytest.raises(ValueError):
        vault.store_credentials(factory, "user-1", "k", b"not-a-pem")


def test_store_rejects_missing_user_or_key():
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    with pytest.raises(ValueError):
        vault.store_credentials(factory, "", "k", pem)
    with pytest.raises(ValueError):
        vault.store_credentials(factory, "u", "", pem)


def test_two_users_isolated(tmp_path, monkeypatch):
    """User A's encrypted blob does not unlock under user B's queries."""
    factory, conn = make_in_memory_conn_factory()
    pem_a, _ = make_test_rsa_key()
    pem_b, _ = make_test_rsa_key()
    vault.store_credentials(factory, "alice", "key-a", pem_a)
    vault.store_credentials(factory, "bob", "key-b", pem_b)
    a = vault.load_credentials(factory, "alice")
    b = vault.load_credentials(factory, "bob")
    assert a.key_id == "key-a"
    assert b.key_id == "key-b"
    assert a.user_id == "alice"
    assert b.user_id == "bob"


def test_demo_flag_round_trips():
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "user-1", "k", pem, is_demo=True)
    creds = vault.load_credentials(factory, "user-1")
    assert creds.is_demo is True
