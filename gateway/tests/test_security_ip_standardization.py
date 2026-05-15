"""Audit MED FIX (audit_security_dir.md cross-cutting) — pin the
client-IP standardization across the three security helpers.

Before the fix, ``audit._get_ip`` / ``logger._get_ip`` /
``rate_limiter.get_client_ip`` each had a hand-rolled implementation:

  * ``audit._get_ip`` honoured ONLY ``x-forwarded-for`` (no CF header)
  * ``logger._get_ip`` honoured ``cf-connecting-ip`` UNCONDITIONALLY
  * ``rate_limiter.get_client_ip`` honoured ``cf-connecting-ip`` and
    ``x-forwarded-for`` UNCONDITIONALLY

Two of the three trusted spoofable headers from any peer, so an
off-tunnel attacker could forge the IP into the security log, evade
rate limits, and poison the audit trail. ``audit.py`` had the opposite
defect — it never read the CF header, so admin actions on a real
Cloudflare path recorded the loopback peer instead of the real client.

All three now delegate to the canonical ``server._get_client_ip``
which enforces the trusted-peer gate (``_TRUSTED_PROXY_HOSTS``: loopback
addresses). The headers are honoured only when the immediate peer is in
the trusted set — i.e. when the request actually came through the
cloudflared tunnel on 127.0.0.1.

This module covers:

1. All three helpers return the SAME IP for the same request.
2. Loopback peer + CF header → header value (consistent).
3. Off-tunnel peer + spoofed CF header → peer host (header dropped).
4. Off-tunnel peer + spoofed XFF → peer host (header dropped).
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _CIHeaders(dict):
    """Case-insensitive header dict mirroring Starlette behaviour."""

    def __init__(self, src: dict | None = None) -> None:
        super().__init__()
        for k, v in (src or {}).items():
            self[k.lower()] = v

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)


class _FakeRequest:
    def __init__(self, peer: str | None, headers: dict | None = None) -> None:
        self.client = _FakeClient(peer) if peer is not None else None
        self.headers = _CIHeaders(headers)


def _call_audit(req) -> str:
    from security.audit import _get_ip
    return _get_ip(req)


def _call_logger(req) -> str:
    from security.logger import _get_ip
    return _get_ip(req)


def _call_rate_limiter(req) -> str:
    from security.rate_limiter import get_client_ip
    return get_client_ip(req)


_HELPERS = [
    ("audit", _call_audit),
    ("logger", _call_logger),
    ("rate_limiter", _call_rate_limiter),
]


class TestSecurityIPStandardization(unittest.TestCase):
    """Pin the cross-cutting contract: all three helpers return the same
    value for the same request, and that value is the canonical
    ``server._get_client_ip`` output.
    """

    def setUp(self):
        # Make sure server is importable in this test process.
        import server  # noqa: F401
        self._server_get_client_ip = server._get_client_ip

    # ── Loopback peer (trusted) ────────────────────────────────────────

    def test_loopback_peer_cf_header_trusted_all_three(self):
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"cf-connecting-ip": "1.2.3.4"},
        )
        expected = self._server_get_client_ip(req)
        self.assertEqual(expected, "1.2.3.4")
        for name, helper in _HELPERS:
            with self.subTest(helper=name):
                # logger returns "unknown" for no-request, but for a real
                # request all three should match the canonical helper.
                self.assertEqual(
                    helper(req), expected,
                    f"{name} disagrees with server._get_client_ip",
                )

    def test_loopback_peer_xff_trusted_all_three(self):
        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "5.6.7.8, 9.9.9.9"},
        )
        expected = self._server_get_client_ip(req)
        self.assertEqual(expected, "5.6.7.8")
        for name, helper in _HELPERS:
            with self.subTest(helper=name):
                self.assertEqual(helper(req), expected)

    # ── Off-tunnel peer (untrusted) ────────────────────────────────────

    def test_off_tunnel_peer_cf_header_dropped_all_three(self):
        # Direct-origin attacker forging the CF header — must be
        # dropped on the floor by every helper.
        req = _FakeRequest(
            peer="198.51.100.7",
            headers={"cf-connecting-ip": "1.2.3.4"},
        )
        expected = self._server_get_client_ip(req)
        self.assertEqual(expected, "198.51.100.7")
        for name, helper in _HELPERS:
            with self.subTest(helper=name):
                self.assertEqual(
                    helper(req), expected,
                    f"{name} honoured the spoofed CF-Connecting-IP",
                )

    def test_off_tunnel_peer_xff_dropped_all_three(self):
        req = _FakeRequest(
            peer="198.51.100.7",
            headers={"x-forwarded-for": "1.2.3.4"},
        )
        expected = self._server_get_client_ip(req)
        self.assertEqual(expected, "198.51.100.7")
        for name, helper in _HELPERS:
            with self.subTest(helper=name):
                self.assertEqual(
                    helper(req), expected,
                    f"{name} honoured the spoofed X-Forwarded-For",
                )

    # ── Edge cases ─────────────────────────────────────────────────────

    def test_no_client_at_all(self):
        # logger returns "unknown" by historical contract; audit returns
        # "" to match the audit_log column shape; rate_limiter delegates
        # to server which returns "unknown". The contract is: NONE of
        # them raise, and rate_limiter + server return "unknown".
        req = _FakeRequest(peer=None, headers={})
        self.assertEqual(self._server_get_client_ip(req), "unknown")
        # Rate limiter mirrors server.
        self.assertEqual(_call_rate_limiter(req), "unknown")
        # Logger returns "unknown" (its historical default).
        self.assertEqual(_call_logger(req), "unknown")
        # Audit returns "" for the unknown branch (matches its DB column
        # shape — the audit_log stores NULL/empty for unknown peers).
        self.assertEqual(_call_audit(req), "")

    def test_off_tunnel_loopback_string_is_trusted(self):
        # "localhost" is also in the trusted set — the dev TestClient and
        # the cloudflared tunnel both surface as one of the loopback names.
        req = _FakeRequest(
            peer="localhost",
            headers={"cf-connecting-ip": "1.2.3.4"},
        )
        self.assertEqual(self._server_get_client_ip(req), "1.2.3.4")
        for name, helper in _HELPERS:
            with self.subTest(helper=name):
                self.assertEqual(helper(req), "1.2.3.4")


if __name__ == "__main__":
    unittest.main()
