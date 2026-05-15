"""Audit HIGH FIX B — regression tests for the CF-Connecting-IP trust boundary.

Both ``middleware.subproduct.SubproductMiddleware`` and
``stripe_webhook_hardening.extract_client_ip`` previously trusted the
``CF-Connecting-IP`` header unconditionally. That meant an attacker who
hit the origin off-tunnel could attach an arbitrary value (e.g. one of
Stripe's published egress IPs) and bypass:

  * the Stripe webhook IP allowlist in ``reject_non_stripe_ip``
  * the prod ``SubproductMiddleware`` direct-origin guard
  * any IP-based audit log entry

The fix wires both call sites through ``trusted_client_ip``, a single
shared helper that only honours the header when the immediate peer
(``request.client.host``) is in ``_TRUSTED_PROXY_HOSTS`` (loopback
addresses + the synthetic TestClient host). Anything else returns the
actual peer address — NEVER the spoofable header — so the downstream
allowlist sees the off-tunnel IP and rejects.

These tests pin both halves of the contract.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ────────────────────────────────────────────────────────────────


class _FakeClient:
    """Mimics ``starlette.requests.Request.client`` (NamedTuple with .host)."""

    def __init__(self, host: str) -> None:
        self.host = host


class _CIHeaders(dict):
    """Case-insensitive header dict mirroring Starlette behaviour.

    Both call sites look up ``cf-connecting-ip`` (lowercase) while
    real-world HTTP libraries and the audit examples use the camel-cased
    ``CF-Connecting-IP``. Normalise on insert so tests work either way.
    """

    def __init__(self, src: dict | None = None) -> None:
        super().__init__()
        for k, v in (src or {}).items():
            self[k.lower()] = v

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)


class _FakeRequest:
    """Minimal Request stand-in: just ``client`` and ``headers``."""

    def __init__(self, peer: str | None, headers: dict | None = None) -> None:
        self.client = _FakeClient(peer) if peer is not None else None
        self.headers = _CIHeaders(headers)


# ── trusted_client_ip — the shared helper ──────────────────────────────────


class TestTrustedClientIPHelper(unittest.TestCase):
    """The single source of truth — both call sites delegate to this."""

    def test_off_tunnel_peer_ignores_spoofed_cf_header(self):
        """Audit scenario 1: direct-origin hit with a forged header.

        Peer ``203.0.113.7`` is a TEST-NET-3 documentation address. It's
        NOT in the trusted set, so the attacker's
        ``CF-Connecting-IP: 3.18.12.63`` (a real Stripe egress IP) must
        be ignored and the function must return the actual peer host.
        """
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"cf-connecting-ip": "3.18.12.63"},
        )
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_off_tunnel_peer_ignores_spoofed_xff_header(self):
        """X-Forwarded-For is the same trust class as CF-Connecting-IP."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"x-forwarded-for": "3.18.12.63, 10.0.0.5"},
        )
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_loopback_peer_trusts_cf_header(self):
        """Audit scenario 2: real cloudflared ingress. Peer is loopback,
        the CF header carries the actual end-user IP — return that."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"cf-connecting-ip": "8.8.8.8"},
        )
        self.assertEqual(trusted_client_ip(req), "8.8.8.8")

    def test_testclient_peer_is_trusted(self):
        """Starlette TestClient uses ``testclient`` as the synthetic peer
        — include it so unit tests can simulate Cloudflare ingress."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="testclient",
            headers={"cf-connecting-ip": "8.8.8.8"},
        )
        self.assertEqual(trusted_client_ip(req), "8.8.8.8")

    def test_loopback_peer_no_cf_falls_back_to_xff(self):
        """If CF is missing but XFF is present on a trusted peer, use XFF
        (leftmost entry — the original client per the standard)."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "8.8.8.8, 10.0.0.5"},
        )
        self.assertEqual(trusted_client_ip(req), "8.8.8.8")

    def test_loopback_peer_no_headers_returns_peer(self):
        """Trusted peer with no forwarded headers — peer host wins."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(peer="127.0.0.1", headers={})
        self.assertEqual(trusted_client_ip(req), "127.0.0.1")

    def test_missing_client_returns_empty(self):
        """``request.client = None`` → empty string, no header read."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer=None,
            headers={"cf-connecting-ip": "8.8.8.8"},
        )
        self.assertEqual(trusted_client_ip(req), "")


# ── extract_client_ip (Stripe webhook) — delegation path ───────────────────


class TestExtractClientIPDelegates(unittest.TestCase):
    """The Stripe webhook helper must route through trusted_client_ip."""

    def test_off_tunnel_peer_ignores_spoofed_header(self):
        """Forged CF-Connecting-IP must NOT pass the Stripe allowlist.

        Pre-fix this returned the forged header verbatim; the downstream
        ``reject_non_stripe_ip`` then accepted it (since the spoof was a
        real Stripe egress IP). Post-fix the function returns the off-
        tunnel peer host, which the allowlist correctly rejects.
        """
        from stripe_webhook_hardening import extract_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={"cf-connecting-ip": "3.18.12.63"},
        )
        self.assertEqual(extract_client_ip(req), "203.0.113.7")

    def test_loopback_peer_trusts_cf_header(self):
        """Real cloudflared ingress still resolves to the end-user IP."""
        from stripe_webhook_hardening import extract_client_ip

        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"cf-connecting-ip": "3.18.12.63"},
        )
        self.assertEqual(extract_client_ip(req), "3.18.12.63")

    def test_off_tunnel_blocks_stripe_webhook_allowlist(self):
        """End-to-end audit scenario: forged Stripe IP doesn't pass.

        Wires both halves of the fix together — the IP allowlist sees
        the actual off-tunnel peer (a TEST-NET-3 address) and rejects.
        """
        from stripe_webhook_hardening import extract_client_ip, reject_non_stripe_ip

        # Force the allowlist enforcement on for this test.
        os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "1"
        try:
            req = _FakeRequest(
                peer="203.0.113.7",
                headers={"cf-connecting-ip": "3.18.12.63"},
            )
            ip = extract_client_ip(req)
            response = reject_non_stripe_ip(ip)
            self.assertIsNotNone(
                response,
                "off-tunnel forged Stripe IP must be rejected by allowlist",
            )
            self.assertEqual(getattr(response, "status_code", None), 403)
        finally:
            os.environ.pop("STRIPE_IP_ALLOWLIST_ENFORCE", None)


if __name__ == "__main__":
    unittest.main()
