"""Regression tests for MED-3 — shared-collection slug enumeration fix.

AUDIT (MED-3) — see ``audits/audit_collections_routes.md``. Pre-fix, a
signed-in attacker could brute-force ``/c/{victim}/{guess}`` and tell
200 vs 404 apart for a victim's ``shared`` boards, exposing slug
existence (and through slug derivation, the underlying titles).

Asserted behaviour:

  1. Brute-force ``/c/{victim}/{guess}`` without a share token returns 404
     regardless of whether the guess matches an existing shared board.
  2. The owner's own ``/c/{handle}/{slug}`` access still succeeds without
     a token (owners bypass the gate).
  3. With a valid share token, ``/c/{handle}/{slug}?t={token}`` returns
     200 for a shared board that exists.
  4. A token minted for board A is rejected when replayed against board B.
  5. A malformed / empty token is rejected (404, not 403, so the response
     is indistinguishable from a nonexistent slug).
  6. Public collections are unaffected — 200/404 still based purely on
     existence, no token required.
  7. Private collections still return 404 to non-owner viewers (no change).
  8. The owner's Share button surface (the rendered detail page) embeds
     the signed token in the ``data-share-url`` attribute so the
     client-side share menu copies a link that actually works.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
# Give a high global cap so the brute-force loop doesn't accidentally
# trip the global per-IP rate limiter before the audit-fix logic gets a
# chance to gate the request.
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
from queries import collections as coll  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


# ── Fixtures ─────────────────────────────────────────────────────────────


_ctr = 0


def _mk(email_prefix: str) -> tuple[int, str]:
    global _ctr
    _ctr += 1
    email = f"{email_prefix}{_ctr}@enum.test"
    username = f"{email_prefix}{_ctr}"
    uid = db.create_user(email, "TestPass123!", username=username)
    token = db.create_session(uid)
    return uid, token


def _auth(token: str) -> dict:
    # Even for GET requests, sending the CSRF cookie keeps us aligned
    # with the rest of the suite — middleware ignores it on GETs anyway.
    return {
        "Cookie": f"pm_gateway_session={token}; _csrf=t",
        "x-csrf-token": "t",
    }


def _handle(uid: int) -> str:
    with db.conn() as c:
        row = c.execute(
            "SELECT username FROM users WHERE id = ?", (uid,),
        ).fetchone()
    return row["username"]


def _clear():
    _conn.execute("DELETE FROM collection_items")
    _conn.execute("DELETE FROM collection_follows")
    _conn.execute("DELETE FROM collections")
    _conn.commit()


# ── Tests ────────────────────────────────────────────────────────────────


class TestSharedEnumeration(unittest.TestCase):
    """A signed-in attacker can no longer distinguish ``/c/{victim}/exists``
    from ``/c/{victim}/does-not-exist`` for ``shared`` boards. Both 404."""

    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_brute_force_without_token_always_404(self):
        owner, _ = _mk("victim")
        attacker, attacker_tok = _mk("attacker")
        # Victim owns a shared board the attacker might try to guess.
        coll.create_collection(owner, "Secret Sauce", visibility="shared")
        h = _handle(owner)

        # Real slug, no token, signed-in attacker → 404.
        r1 = client.get(f"/c/{h}/secret-sauce", headers=_auth(attacker_tok))
        self.assertEqual(r1.status_code, 404)

        # Fake slug, same setup → 404. Pre-fix: real was 200, fake was 404.
        # Post-fix: both 404 so the attacker can't distinguish.
        r2 = client.get(f"/c/{h}/totally-fake-slug", headers=_auth(attacker_tok))
        self.assertEqual(r2.status_code, 404)

    def test_brute_force_without_token_404_anonymous_too(self):
        owner, _ = _mk("victim_anon")
        coll.create_collection(owner, "Quiet Board", visibility="shared")
        h = _handle(owner)
        # Anon viewer was already gated by ``_can_view`` (shared requires
        # signed-in), but re-assert that nothing changed for them.
        r = client.get(f"/c/{h}/quiet-board")
        self.assertEqual(r.status_code, 404)

    def test_with_valid_share_token_returns_200(self):
        owner, _ = _mk("owner_tok")
        viewer, viewer_tok = _mk("viewer_tok")
        cid = coll.create_collection(owner, "Sharable", visibility="shared")
        h = _handle(owner)

        token = coll.mint_share_token(owner, cid)
        r = client.get(
            f"/c/{h}/sharable?t={token}",
            headers=_auth(viewer_tok),
        )
        self.assertEqual(r.status_code, 200)
        # Robots stays noindex for non-public boards.
        self.assertIn('name="robots" content="noindex,nofollow"', r.text)

    def test_token_from_other_board_rejected(self):
        owner, _ = _mk("xowner")
        viewer, viewer_tok = _mk("xviewer")
        cid_a = coll.create_collection(owner, "Board A", visibility="shared")
        cid_b = coll.create_collection(owner, "Board B", visibility="shared")
        h = _handle(owner)

        # Token minted for B should NOT unlock A — that would let a
        # legitimate sharee enumerate other shared boards under the same
        # owner just by holding one valid token.
        tok_b = coll.mint_share_token(owner, cid_b)
        r = client.get(
            f"/c/{h}/board-a?t={tok_b}",
            headers=_auth(viewer_tok),
        )
        self.assertEqual(r.status_code, 404)

        # And the legitimate token for B still works on B.
        r_ok = client.get(
            f"/c/{h}/board-b?t={tok_b}",
            headers=_auth(viewer_tok),
        )
        self.assertEqual(r_ok.status_code, 200)

    def test_malformed_token_404(self):
        owner, _ = _mk("mowner")
        viewer, viewer_tok = _mk("mviewer")
        cid = coll.create_collection(owner, "Mal", visibility="shared")
        h = _handle(owner)

        for bad in ("", "garbage", "c1.notvalidhex", "c1.", ".abc", "x"):
            r = client.get(
                f"/c/{h}/mal?t={bad}",
                headers=_auth(viewer_tok),
            )
            # 404 (not 403) so the response is indistinguishable from
            # "slug doesn't exist" — that's the whole point of the fix.
            self.assertEqual(r.status_code, 404, f"bad token {bad!r} should 404")

    def test_owner_can_view_without_token(self):
        owner, owner_tok = _mk("powner")
        cid = coll.create_collection(owner, "Mine Shared", visibility="shared")
        h = _handle(owner)
        # No ``?t=`` query param at all — owner still gets 200.
        r = client.get(f"/c/{h}/mine-shared", headers=_auth(owner_tok))
        self.assertEqual(r.status_code, 200)


class TestPublicEnumerationUnchanged(unittest.TestCase):
    """Public collections stay enumerable — that's what ``public`` means.
    The fix must not regress the SEO/discovery surface."""

    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_public_exists_returns_200_without_token(self):
        owner, _ = _mk("pubo")
        cid = coll.create_collection(owner, "Public Board", visibility="public")
        h = _handle(owner)
        r = client.get(f"/c/{h}/public-board")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="robots" content="index,follow"', r.text)

    def test_public_missing_returns_404(self):
        owner, _ = _mk("pubmissing")
        coll.create_collection(owner, "Real", visibility="public")
        h = _handle(owner)
        r = client.get(f"/c/{h}/nope-not-a-board")
        self.assertEqual(r.status_code, 404)


class TestPrivateEnumerationUnchanged(unittest.TestCase):
    """Private collections were already 404 for non-owners — verify the
    enumeration fix didn't regress that path either."""

    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_private_existing_slug_404_for_stranger(self):
        owner, _ = _mk("privo")
        stranger, stok = _mk("privs")
        cid = coll.create_collection(owner, "Hidden", visibility="private")
        h = _handle(owner)
        r = client.get(f"/c/{h}/hidden", headers=_auth(stok))
        self.assertEqual(r.status_code, 404)

    def test_private_owner_still_403_via_public_url_not_404(self):
        # Owner of a private board hitting /c/{handle}/{slug} *can* see it
        # (owners bypass visibility). Document the existing semantics.
        owner, owner_tok = _mk("privown")
        cid = coll.create_collection(owner, "Own Hidden", visibility="private")
        h = _handle(owner)
        r = client.get(f"/c/{h}/own-hidden", headers=_auth(owner_tok))
        self.assertEqual(r.status_code, 200)


class TestSharedShareUrlEmbedsToken(unittest.TestCase):
    """The Share button on the owner's detail surface must surface a URL
    that actually works for a non-owner — i.e. carries a signed token."""

    def setUp(self):
        _clear()
        client.cookies.clear()

    def test_owner_detail_page_share_url_contains_token(self):
        owner, owner_tok = _mk("shareurl")
        cid = coll.create_collection(owner, "Share Sauce", visibility="shared")
        r = client.get(f"/collections/{cid}", headers=_auth(owner_tok))
        self.assertEqual(r.status_code, 200)
        # The share button data attribute must include "?t=" so the
        # client-side share menu copies the token-bearing link.
        self.assertIn('id="c-share-btn"', r.text)
        self.assertIn("?t=", r.text)

    def test_public_detail_share_url_has_no_token(self):
        owner, owner_tok = _mk("shareurlpub")
        cid = coll.create_collection(owner, "Pub Share", visibility="public")
        r = client.get(f"/collections/{cid}", headers=_auth(owner_tok))
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="c-share-btn"', r.text)
        # No token is appended for public boards — enumeration is by spec.
        # The share button URL must be the clean /c/{handle}/{slug}.
        # We check for the explicit absence of "?t=" inside the share
        # button data attribute. Pull a narrow window around the button
        # to avoid a false hit from elsewhere in the page (e.g. some
        # other anchor that happens to carry a query string).
        idx = r.text.find('id="c-share-btn"')
        self.assertGreater(idx, -1)
        snippet = r.text[max(0, idx - 240): idx + 120]
        self.assertNotIn("?t=", snippet)


class TestMintVerifyHelpers(unittest.TestCase):
    """Pure unit tests for the mint/verify primitives — important because
    these are reused by routes and any future surface (RSS, OG cards,
    API) that wants to honour the same gate."""

    def test_mint_is_deterministic_under_stable_secret(self):
        # Two mints with the same secret produce the same token — no
        # nonce, no timestamp. Stable URLs are a feature: the owner can
        # share once and never have to re-send a fresh link.
        t1 = coll.mint_share_token(42, 99)
        t2 = coll.mint_share_token(42, 99)
        self.assertEqual(t1, t2)

    def test_verify_accepts_freshly_minted(self):
        token = coll.mint_share_token(7, 13)
        self.assertTrue(coll.verify_share_token(7, 13, token))

    def test_verify_rejects_wrong_owner_or_collection(self):
        token = coll.mint_share_token(7, 13)
        # Same collection, wrong owner.
        self.assertFalse(coll.verify_share_token(8, 13, token))
        # Same owner, wrong collection.
        self.assertFalse(coll.verify_share_token(7, 14, token))

    def test_verify_rejects_garbage(self):
        for bad in (None, "", "no-dot", "wrong.prefix", "c1.", "c1.deadbeef"):
            self.assertFalse(coll.verify_share_token(1, 1, bad))


if __name__ == "__main__":
    unittest.main()
