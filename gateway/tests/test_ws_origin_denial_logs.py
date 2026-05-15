"""Audit MED FIX (audit_security_dir.md cross-cutting) — pin the
``host`` + ``ip_hash`` columns on every WS Origin denial log line.

Background: ``gateway/server.py`` rejects two classes of WS upgrade
under ``IS_PRODUCTION``:

  * Origin header present but its host doesn't match ``ALLOWED_DOMAINS``
    → "ws origin rejected: ..."
  * Origin header absent in production                → "ws missing origin ..."

Both branches previously logged only ``origin`` + ``host``. The security
feed therefore had no way to pivot from a single denial row to the
offending client across multiple denial events without correlating
free-form log strings against the request log timestamps. The fix
appends ``ip_hash=<hex>`` to both branches using the same ``_hash_ip``
helper that stamps every analytics row, so the feed can group by
``ip_hash`` directly.

This test inspects the source of ``server.websocket_proxy`` to confirm
both log lines reference ``ip_hash`` and ``_get_client_ip``. We use
source inspection rather than firing a real WS handshake because the
TestClient WebSocket flow requires a full ASGI scope and the
``websocket_proxy`` body has hard dependencies on ``ALLOWED_DOMAINS``,
``IS_PRODUCTION``, and the live upstream router. Pinning the source
shape is sufficient to catch a future refactor that drops the
``ip_hash=`` field — the existing rate_limiter / audit tests already
prove ``_hash_ip`` and ``_get_client_ip`` behave correctly.
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestWSOriginDenialLogShape(unittest.TestCase):
    """Both Origin denial branches must log host + ip_hash."""

    def setUp(self):
        import server
        self._src = inspect.getsource(server.websocket_proxy)

    def test_cross_origin_branch_logs_host(self):
        # Locate the "ws origin rejected" log call.
        self.assertIn("ws origin rejected", self._src)
        # The denial branch must reference ``host=`` in the format string.
        # We look for the substring near the reject log to avoid matching
        # the unrelated "host=" used in other branches.
        rejected_idx = self._src.index("ws origin rejected")
        # Look ahead through the next ~400 chars covering the whole
        # log.warning call (the format string + args).
        snippet = self._src[rejected_idx: rejected_idx + 400]
        self.assertIn("host=%s", snippet)

    def test_cross_origin_branch_logs_ip_hash(self):
        rejected_idx = self._src.index("ws origin rejected")
        snippet = self._src[rejected_idx: rejected_idx + 400]
        self.assertIn(
            "ip_hash=%s", snippet,
            "GAP: cross-origin WS denial must include ip_hash so the "
            "security feed can pivot by hashed client.",
        )
        # Confirm the value is produced via _hash_ip(_get_client_ip(ws)).
        self.assertIn("_hash_ip(_get_client_ip(ws))", snippet)

    def test_missing_origin_branch_logs_host(self):
        self.assertIn("ws missing origin", self._src)
        idx = self._src.index("ws missing origin")
        snippet = self._src[idx: idx + 400]
        self.assertIn("host=%s", snippet)

    def test_missing_origin_branch_logs_ip_hash(self):
        idx = self._src.index("ws missing origin")
        snippet = self._src[idx: idx + 400]
        self.assertIn(
            "ip_hash=%s", snippet,
            "GAP: missing-Origin WS denial must include ip_hash for "
            "parity with the cross-origin branch.",
        )
        self.assertIn("_hash_ip(_get_client_ip(ws))", snippet)


class TestIPHashHelperShape(unittest.TestCase):
    """Spot-check ``_hash_ip`` so the WS branches above produce a
    well-formed hash, not e.g. an empty string for a real peer."""

    def test_hash_ip_nonempty_for_real_ip(self):
        from server import _hash_ip
        h = _hash_ip("1.2.3.4")
        self.assertEqual(len(h), 64)  # sha256 hex
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_hash_ip_empty_for_empty_ip(self):
        from server import _hash_ip
        # Empty IP → empty hash. The WS log will surface ``ip_hash=`` with
        # an empty value rather than 64 zeros, which matches the contract
        # in the analytics_events column for unknown peers.
        self.assertEqual(_hash_ip(""), "")

    def test_hash_ip_deterministic(self):
        from server import _hash_ip
        self.assertEqual(_hash_ip("1.2.3.4"), _hash_ip("1.2.3.4"))
        self.assertNotEqual(_hash_ip("1.2.3.4"), _hash_ip("1.2.3.5"))


if __name__ == "__main__":
    unittest.main()
