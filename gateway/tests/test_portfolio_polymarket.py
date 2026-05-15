"""SIWE-required wallet attach on ``/api/portfolio/polymarket/connect``.

Audit #14 HIGH — the portfolio-namespace polymarket connect endpoint
previously accepted an unsigned ``{wallet_address}`` payload and upserted
the row, fully bypassing the SIWE-required fix on the parallel
``/api/markets/connect/polymarket`` surface (commit e3248d5). This file
pins the contract that BOTH surfaces now demand the same SIWE proof so a
future revert on either path fails the suite loudly.

Surface under test (one file):

  * POST /api/portfolio/polymarket/connect

Pre-requisite endpoint that the test flow exercises but does not pin:

  * GET  /api/markets/connect/polymarket/nonce  — see
    ``test_polymarket_siwe.py`` for its own coverage.

The portfolio route lives in ``gateway/portfolio/routes.py`` and stores
into ``polymarket_connections``. The markets route lives in
``gateway/market_routes.py`` and stores into ``user_market_credentials``.
The audit finding was that the parallel storage table on this route was
invisible to the legacy-removal guard on the markets-route path; both
must now reject the unsigned shape.

Tests use a real ``eth_account`` keypair so the signature path is
end-to-end. ``eth-account==0.10.0`` is a hard test-time dep — same pin
as gateway/requirements.txt.
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

from portfolio import polymarket as _poly_mod  # noqa: E402


client = TestClient(server.app)

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


_user_seq = 0


def _make_trading_user(slug: str) -> tuple[int, str]:
    """Create a user with the Trading Add-on so the gate doesn't 402 us
    out the door. Returns (user_id, narve_session_token).

    Each call gets a unique email/username suffix so the shared in-memory
    DB doesn't collide on the UNIQUE(email) constraint across tests.

    The portfolio routes read ``request.state.user``, which the
    ``SessionMiddleware`` populates from the **hardened**
    ``narve_session`` cookie. ``db.create_user_session`` issues that
    token; the legacy ``pm_gateway_session`` flow does NOT reach those
    handlers.
    """
    global _user_seq
    _user_seq += 1
    uname = f"{slug}_{_user_seq}"[:30]
    uid = db.create_user(f"{uname}@test.local", "TestPass123!", username=uname)
    try:
        db.set_trading_addon(uid, True, int(time.time()) + 30 * 86400)
    except Exception:
        pass
    raw = db.create_user_session(uid)
    return uid, raw


def _sign_personal(message: str, private_key: str) -> str:
    """Produce a personal_sign hex signature over ``message`` — the
    same shape MetaMask hands the browser. The server's
    ``_siwe_recover_signer`` uses ``encode_defunct`` + ``recover_message``
    to reverse this."""
    encoded = encode_defunct(text=message)
    signed = Account.sign_message(encoded, private_key=private_key)
    sig = signed.signature
    hexsig = sig.hex() if isinstance(sig, (bytes, bytearray)) else str(sig)
    if not hexsig.startswith("0x"):
        hexsig = "0x" + hexsig
    return hexsig


class _PortfolioSIWEBase(unittest.TestCase):
    """Shared setup — fresh trading user + cleared rate-limit/nonce state
    per test. Identical pattern to ``test_polymarket_siwe._SIWEBase`` so
    the two suites stay symmetric."""

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
            # Drain any leftover nonces + connection rows so per-test
            # state is deterministic. Both tables — the portfolio path
            # writes ``polymarket_connections`` and the markets path
            # writes ``user_market_credentials``; the cross-account
            # collision check inspects both.
            c.execute("DELETE FROM wallet_connect_nonces")
            c.execute("DELETE FROM polymarket_connections")
            c.execute(
                "DELETE FROM user_market_credentials WHERE source = 'polymarket'"
            )
        slug = f"pf_{self.id().split('.')[-1]}"[:20]
        self.uid, self.token = _make_trading_user(slug)
        self.acct = Account.create()
        # Hardened session cookie + double-submit CSRF pair.
        self.cookies = {"narve_session": self.token, "_csrf": "t"}
        self.csrf_header = {"x-csrf-token": "t"}

    def _get_nonce(self) -> dict:
        # The portfolio connect route reuses the markets-route nonce
        # endpoint — the SIWE message it expects is the canonical one,
        # so the client flow is "GET nonce on the markets route, POST
        # signature to the portfolio route". This is also what an
        # attacker has to do, which keeps the surface narrow.
        r = client.get(
            "/api/markets/connect/polymarket/nonce",
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _post_connect(self, body: dict):
        return client.post(
            "/api/portfolio/polymarket/connect",
            json=body,
            cookies=self.cookies,
            headers=self.csrf_header,
        )


class TestUnsignedRejected(_PortfolioSIWEBase):
    """Audit #14 HIGH — POST {wallet_address} (no signature, no message,
    no address) MUST 400 and MUST NOT side-effect the DB. This is the
    bypass that the fix closes; a regression here re-opens the parallel
    unsigned-attach hole."""

    def test_legacy_wallet_address_only_rejected_400(self):
        """The exact body that was previously accepted is now refused."""
        r = self._post_connect({"wallet_address": self.acct.address})
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        err = (body.get("error") or "").lower()
        # Body must point migrating clients at the SIWE flow — the
        # noun "signature" or "nonce" is the canonical breadcrumb.
        self.assertTrue(
            "signature" in err or "nonce" in err,
            f"400 body should point at SIWE flow, got: {err!r}",
        )

    def test_unsigned_request_does_not_persist_wallet(self):
        """Belt-and-braces — even when the unsigned body is rejected, no
        connection row should have been written. Catches a future
        regression that 400s AFTER the upsert."""
        r = self._post_connect({"wallet_address": self.acct.address})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(_poly_mod.get_connection(self.uid))

    def test_empty_body_400(self):
        """Neither {address, signature, message} nor {wallet_address}
        present → 400 with a hint pointing at the nonce endpoint."""
        r = self._post_connect({})
        self.assertEqual(r.status_code, 400)
        err = (r.json().get("error") or "").lower()
        self.assertTrue("signature" in err or "nonce" in err)

    def test_partial_siwe_body_rejected(self):
        """A body with only some SIWE fields (e.g. just ``address``)
        must be refused — the server must require the full
        ``{address, signature, message}`` triplet."""
        r = self._post_connect({"address": self.acct.address})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(_poly_mod.get_connection(self.uid))


class TestSignedConnect(_PortfolioSIWEBase):
    """A valid SIWE round-trip must succeed and persist the address."""

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
        self.assertEqual(data["wallet_address"], self.acct.address.lower())
        # And the row really is in the portfolio-route's storage table.
        conn = _poly_mod.get_connection(self.uid)
        self.assertIsNotNone(conn)
        self.assertEqual(conn["wallet_address"], self.acct.address.lower())

    def test_mismatched_signature_rejected(self):
        """A spoof attacker signs with key X but claims address Y. The
        recovered signer doesn't match the claimed wallet, so the
        connect must 400 and write nothing."""
        body = self._get_nonce()
        spoofed = Account.create()
        msg = body["message_template"].replace("{address}", spoofed.address)
        # Sign with the WRONG private key while claiming the spoofed
        # address. Recovery returns self.acct.address (the real signer)
        # which does NOT match the claimed `spoofed.address`.
        sig = _sign_personal(msg, self.acct.key.hex())

        r = self._post_connect(
            {"address": spoofed.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("does not match", r.json()["error"].lower())
        self.assertIsNone(_poly_mod.get_connection(self.uid))

    def test_wrong_domain_rejected(self):
        """A tampered URI line must be refused even though the signature
        verifies for the tampered body — the domain pin is the defense
        against signatures collected by a phishing app."""
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        tampered = msg.replace("URI: https://narve.ai", "URI: https://evil.example")
        sig = _sign_personal(tampered, self.acct.key.hex())

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": tampered},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("domain", r.json()["error"].lower())
        self.assertIsNone(_poly_mod.get_connection(self.uid))

    def test_reused_nonce_rejected(self):
        """Replaying the same (msg, sig, address) is refused on the
        second attempt because the nonce row's ``used_at`` is now
        non-null. Prevents an exfiltrated signature from re-attaching
        the wallet."""
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        r1 = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r1.status_code, 200, r1.text)

        r2 = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("already used", r2.json()["error"].lower())


class TestCrossAccountAttach(_PortfolioSIWEBase):
    """Audit consideration — the same proven wallet must not be
    attachable to two narve accounts simultaneously. Even though the
    wallet's *positions* are public on-chain, an attacker who owns
    wallet W can otherwise create N narve accounts and pre-claim the
    address to harvest the victim's signal-following behaviour through
    the portfolio feed on multiple sessions.

    Both storage tables are inspected — ``polymarket_connections``
    (this route) and ``user_market_credentials`` (the markets-route
    sibling) — so the invariant holds regardless of which surface
    attached the wallet first.
    """

    def test_wallet_already_on_portfolio_table_blocks_second_user(self):
        # First user attaches the wallet via the portfolio route.
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())
        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 200, r.text)

        # Second user, same wallet — must be refused with a 409.
        uid2, token2 = _make_trading_user("pf_collide_a")
        self.cookies = {"narve_session": token2, "_csrf": "t"}
        body2 = self._get_nonce()
        msg2 = body2["message_template"].replace("{address}", self.acct.address)
        sig2 = _sign_personal(msg2, self.acct.key.hex())

        r2 = self._post_connect(
            {"address": self.acct.address, "signature": sig2, "message": msg2},
        )
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertIn("already attached", r2.json()["error"].lower())
        # And no row was written for the second user.
        self.assertIsNone(_poly_mod.get_connection(uid2))

    def test_wallet_already_on_market_credentials_blocks_portfolio_route(self):
        """Cross-table check — the markets route writes to
        ``user_market_credentials``. If a wallet is already attached
        there to user A, user B must not be able to attach the same
        wallet via the portfolio route."""
        address = self.acct.address.lower()
        # Plant a row on the markets-route storage table under a
        # different user.
        uid_other = db.create_user(
            "pf_other@test.local", "TestPass123!", username="pf_other_x",
        )
        db.upsert_market_credential(
            uid_other, "polymarket",
            polymarket_wallet_address=address,
        )

        # Now self.uid tries to SIWE-attach the same wallet — must 409.
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())
        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("already attached", r.json()["error"].lower())
        self.assertIsNone(_poly_mod.get_connection(self.uid))


class TestGateBeforeSIWE(_PortfolioSIWEBase):
    """The Trading Add-on gate must fire BEFORE the SIWE checks — a
    free-tier user posting a perfectly valid SIWE body must still see
    402 Payment Required, not 200. Catches a refactor that swaps the
    helper order and silently regresses the monetisation contract."""

    def test_free_user_blocked_even_with_valid_siwe(self):
        # Acquire the nonce while authed (so it gets bound to a user_id
        # the route can match), then drop the trading addon before the
        # POST so the gate trips.
        body = self._get_nonce()
        msg = body["message_template"].replace("{address}", self.acct.address)
        sig = _sign_personal(msg, self.acct.key.hex())

        try:
            db.set_trading_addon(self.uid, False, int(time.time()))
        except Exception:
            pass

        r = self._post_connect(
            {"address": self.acct.address, "signature": sig, "message": msg},
        )
        self.assertEqual(r.status_code, 402, r.text)
        self.assertIsNone(_poly_mod.get_connection(self.uid))


if __name__ == "__main__":
    unittest.main()
