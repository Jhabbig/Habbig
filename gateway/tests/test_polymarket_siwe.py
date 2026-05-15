"""Tests for the SIWE (EIP-4361) wallet-connect proof of ownership
on the Polymarket connect path.

Surface under test:

  * GET  /api/markets/connect/polymarket/nonce  — issues a nonce row
  * POST /api/markets/connect/polymarket        — verifies signature +
                                                  consumes nonce

AUDIT 2026-05-15 (MED #3) — the legacy unsigned
``{wallet_address: ...}`` body shape was removed inside the 30-day
deprecation window. Requests using it now get a **410 Gone** with a
pointer at the SIWE flow; ``TestLegacyRemoval`` below pins that
contract so a future revert can't quietly re-enable the unsigned
attach.

Tests use a real ``eth_account`` keypair so the signature path is
end-to-end. ``eth-account==0.10.0`` is a hard test-time dep — same
pin as gateway/requirements.txt.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import server  # noqa: E402
import market_routes  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402


client = TestClient(server.app)

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


_user_seq = 0


def _make_trading_user(slug: str) -> tuple[int, str]:
    """Create a user with the Trading Add-on so the markets routes
    don't 403 us out the door. Returns (user_id, session_token).

    Each call gets a unique email/username suffix — the shared
    in-memory DB persists rows across tests in the same module, so a
    bare ``siwe_foo@test.local`` collides on the UNIQUE(email)
    constraint on the second test.
    """
    global _user_seq
    _user_seq += 1
    uname = f"{slug}_{_user_seq}"[:30]
    uid = db.create_user(f"{uname}@test.local", "TestPass123!", username=uname)
    try:
        db.set_trading_addon(uid, True, int(time.time()) + 30 * 86400)
    except Exception:
        pass
    token = db.create_session(uid)
    return uid, token


def _sign_personal(message: str, private_key: str) -> str:
    """Produce a personal_sign hex signature over ``message`` — the
    same shape MetaMask hands the browser. The server's
    ``_siwe_recover_signer`` uses ``encode_defunct`` + ``recover_message``
    to reverse this; matching shape ensures round-trip parity."""
    encoded = encode_defunct(text=message)
    signed = Account.sign_message(encoded, private_key=private_key)
    sig = signed.signature
    # eth_account < 0.11 returns hex bytes; .hex() is the canonical
    # form MetaMask hands back. Prefix 0x if missing.
    hexsig = sig.hex() if isinstance(sig, (bytes, bytearray)) else str(sig)
    if not hexsig.startswith("0x"):
        hexsig = "0x" + hexsig
    return hexsig


class _SIWEBase(unittest.TestCase):
    """Shared setup — fresh trading user + cleared rate limits per test."""

    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        try:
            client.cookies.clear()
        except Exception:
            pass
        try:
            server._rate_store.clear()
        except Exception:
            pass
        with db.conn() as c:
            # Drain any leftover nonces so per-test counts are deterministic.
            c.execute("DELETE FROM wallet_connect_nonces")
        # Fresh user + new keypair per test so a state-bleed from a
        # sibling test can't make a "should be rejected" assertion
        # pass spuriously.
        # Username is keyed off the test id but capped at 24 chars —
        # the auth layer accepts up to 32 and a long generated name
        # crowds the UNIQUE index search.
        slug = f"siwe_{self.id().split('.')[-1]}"[:24]
        self.uid, self.token = _make_trading_user(slug)
        self.acct = Account.create()
        # Double-submit CSRF: cookie + matching header. Anything with
        # the same value pair on both passes the middleware; tests use
        # the literal "t" so we don't need to derive a real token.
        self.cookies = {server.COOKIE_NAME: self.token, "_csrf": "t"}
        self.csrf_header = {"x-csrf-token": "t"}

    def _get_nonce(self) -> dict:
        # GET is CSRF-exempt but the auth + add-on cookies still need
        # to ride along.
        r = client.get(
            "/api/markets/connect/polymarket/nonce",
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _post_connect(self, body: dict):
        return client.post(
            "/api/markets/connect/polymarket",
            json=body,
            cookies=self.cookies,
            headers=self.csrf_header,
        )


class TestNonceIssue(_SIWEBase):
    def test_nonce_endpoint_returns_template(self):
        body = self._get_nonce()
        # Server must surface every field the client needs to build
        # the exact string it will sign — template, domain, chain id,
        # nonce, issued-at timestamp.
        self.assertIn("nonce", body)
        self.assertIn("message_template", body)
        self.assertEqual(body["domain"], "narve.ai")
        self.assertEqual(body["uri"], "https://narve.ai")
        self.assertEqual(body["chain_id"], 1)
        self.assertEqual(body["version"], "1")
        self.assertIn("{address}", body["message_template"])

    def test_nonce_is_persisted_unused(self):
        body = self._get_nonce()
        with db.conn() as c:
            row = c.execute(
                "SELECT user_id, used_at FROM wallet_connect_nonces WHERE nonce = ?",
                (body["nonce"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], self.uid)
        self.assertIsNone(row["used_at"])

    def test_nonce_endpoint_requires_auth(self):
        try:
            client.cookies.clear()
        except Exception:
            pass
        r = client.get("/api/markets/connect/polymarket/nonce")
        # Either 401 (not authed) or 403 (no trading add-on) is fine —
        # the gate is in place either way.
        self.assertIn(r.status_code, (401, 403))


class TestSignedConnect(_SIWEBase):
    def test_valid_signature_accepted(self):
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["connected"])
        self.assertTrue(data["verified"])
        # Persisted address is lowercased — keep parity with the rest
        # of the codebase.
        self.assertEqual(data["address"], self.acct.address.lower())

    def test_wrong_signer_rejected(self):
        """A spoof attacker signs with key X but claims address Y."""
        body = self._get_nonce()
        spoofed = Account.create()  # different keypair
        msg = body["message_template"].replace("{address}", spoofed.address)
        # Sign with the WRONG private key while claiming the spoofed
        # address. Recovery returns self.acct.address (the real signer)
        # which does NOT match the claimed `spoofed.address`.
        sig = _sign_personal(msg, self.acct.key.hex())

        r = self._post_connect(
            {"address": spoofed.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("does not match", r.json()["error"].lower())

    def test_reused_nonce_rejected(self):
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        r1 = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r1.status_code, 200, r1.text)

        # Replay the same (msg, sig, address) — server must reject
        # because the nonce row's ``used_at`` is now non-null.
        r2 = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("already used", r2.json()["error"].lower())

    def test_stale_nonce_rejected(self):
        """A nonce older than SIWE_NONCE_TTL is refused even though
        the signature still verifies cryptographically."""
        body = self._get_nonce()
        nonce = body["nonce"]
        # Backdate the row past the TTL.
        with db.conn() as c:
            c.execute(
                "UPDATE wallet_connect_nonces SET created_at = ? WHERE nonce = ?",
                (int(time.time()) - market_routes.SIWE_NONCE_TTL - 60, nonce),
            )
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.json()["error"].lower())

    def test_wrong_domain_rejected(self):
        """A message that swaps URI: https://narve.ai for an attacker
        domain must be refused even if the signature is valid for that
        tampered body."""
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        tampered = msg.replace("URI: https://narve.ai", "URI: https://evil.example")
        sig = _sign_personal(tampered, self.acct.key.hex())

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": tampered},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("domain", r.json()["error"].lower())

    def test_missing_nonce_in_message_400(self):
        """A signed message with no Nonce: line must 400 — the parser
        sees ``nonce=None`` and bails before signature recovery."""
        # Build a message manually without a Nonce line.
        bad = (
            "narve.ai wants you to sign in with your Ethereum account:\n"
            f"{self.acct.address}\n"
            "\n"
            "Verify wallet ownership for narve.ai portfolio sync.\n"
            "\n"
            "URI: https://narve.ai\n"
            "Version: 1\n"
            "Chain ID: 1\n"
            "Issued At: 2026-05-14T12:00:00Z"
        )
        sig = _sign_personal(bad, self.acct.key.hex())
        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": bad},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("nonce", r.json()["error"].lower())


class TestLegacyRemoval(_SIWEBase):
    """Audit MED #3 — the legacy unsigned ``wallet_address`` body shape
    is gone. Clients still POSTing it must get **410 Gone**, never a
    200, so a regression that re-enables the unverified attach fails
    the suite loudly. The DB must NOT carry a side-effect row from the
    request: a 410 is a contract refusal, not a soft-accept."""

    def test_legacy_unsigned_returns_410(self):
        addr = self.acct.address
        r = self._post_connect({"wallet_address": addr})
        self.assertEqual(r.status_code, 410, r.text)
        err = (r.json().get("error") or "").lower()
        # Body must point migrating clients at the SIWE surface — the
        # noun "siwe" or the nonce sub-path is the canonical breadcrumb.
        self.assertTrue(
            "siwe" in err or "nonce" in err,
            f"410 body should point at SIWE flow, got: {err!r}",
        )

    def test_legacy_unsigned_does_not_persist_wallet(self):
        """Belt-and-braces — even when the legacy body is rejected, no
        credential row should have been written. Prevents a future
        regression where the 410 ships AFTER the upsert."""
        addr = self.acct.address
        r = self._post_connect({"wallet_address": addr})
        self.assertEqual(r.status_code, 410, r.text)
        cred = db.get_market_credential(self.uid, "polymarket")
        # Either no row at all, or a row with no wallet attached.
        if cred is not None:
            self.assertFalse(
                cred.get("polymarket_wallet_address"),
                f"legacy 410 must not persist wallet, got: {cred!r}",
            )

    def test_no_body_400(self):
        """Neither {address, signature, message} nor {wallet_address}
        present → 400 with a hint pointing at the nonce endpoint."""
        r = self._post_connect({})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nonce", r.json()["error"].lower())

    def test_siwe_path_still_works_after_legacy_removal(self):
        """Regression guard — removing the legacy branch must not
        affect the signed path. Mirrors
        TestSignedConnect.test_valid_signature_accepted but lives here
        so a future refactor that breaks both can't quietly pass by
        deleting only the legacy class."""
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["connected"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["address"], self.acct.address.lower())


if __name__ == "__main__":
    unittest.main()
