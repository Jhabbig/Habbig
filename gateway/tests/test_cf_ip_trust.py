"""Audit HIGH FIX B — regression tests for the CF-Connecting-IP trust boundary.

Both ``middleware.subproduct.SubproductMiddleware`` and
``stripe_webhook_hardening.extract_client_ip`` previously trusted the
``CF-Connecting-IP`` header unconditionally. That meant an attacker who
hit the origin off-tunnel could attach an arbitrary value (e.g. one of
Stripe's published egress IPs) and bypass:

  * the Stripe webhook IP allowlist in ``reject_non_stripe_ip``
  * the prod ``SubproductMiddleware`` direct-origin guard
  * any IP-based audit log entry

The first fix wired both call sites through ``trusted_client_ip``, a
single shared helper that only honoured the header when the immediate
peer (``request.client.host``) was in ``_TRUSTED_PROXY_HOSTS`` (loopback
addresses + the synthetic TestClient host).

The 2026-05-15 revision generalised that gate. Production logs showed
peer hosts are the real end-user IP, NOT loopback — the original
loopback-only check was 403'ing every legitimate request. The header
is now also honoured when the request carries the full CF-ingress trio:

  * ``CF-Connecting-IP`` (the value to read)
  * ``CF-Ray`` (attached by every Cloudflare POP)
  * ``X-Forwarded-Proto: https`` (forced by the WAF)

A direct-origin probe with only a spoofed ``CF-Connecting-IP`` fails
the fingerprint and falls back to the peer host, so the downstream
allowlist still sees the attacker's actual IP.

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

    def test_loopback_peer_xff_only_no_cf_returns_peer(self):
        """Loopback peer with XFF only (no CF-Connecting-IP) — fall back
        to peer.

        Behaviour change in the 2026-05-15 revision: a loopback peer is
        no longer trusted on its own. The on-box cloudflared tunnel ALWAYS
        attaches ``CF-Connecting-IP``; a loopback caller that doesn't is
        either a local probe (health checker, log scraper) or a hostile
        process that can reach 127.0.0.1. Trusting an arbitrary XFF in
        that case would re-open the spoof surface, so we now return the
        peer host.

        Real cloudflared ingress always sets ``CF-Connecting-IP``, so
        legitimate traffic still resolves to the end-user IP — see
        ``test_loopback_peer_trusts_cf_header``.
        """
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="127.0.0.1",
            headers={"x-forwarded-for": "8.8.8.8, 10.0.0.5"},
        )
        self.assertEqual(trusted_client_ip(req), "127.0.0.1")

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


# ── CF-ingress fingerprint (2026-05-15 revision) ───────────────────────────


_CF_FINGERPRINT_HEADERS = {
    "cf-connecting-ip": "8.8.8.8",
    "cf-ray": "8a1b2c3d4e5f6789-EWR",
    "x-forwarded-proto": "https",
}


class TestCFFingerprintTrust(unittest.TestCase):
    """The 2026-05-15 revision honours CF-Connecting-IP when the request
    carries the full CF-edge trio (CF-Connecting-IP + CF-Ray +
    X-Forwarded-Proto: https), even if the peer is the real end-user IP
    rather than loopback. The prod tunnel topology means peer is the
    user IP — the loopback-only gate was 403'ing all legitimate traffic.
    """

    def test_real_cf_request_trusts_cf_connecting_ip(self):
        """Peer is the end-user IP, but the CF fingerprint is intact.

        This is the shape of every real production request: CF terminates
        TLS, forwards to the origin (or via tunnel) with the trio
        attached. ``trusted_client_ip`` must return ``8.8.8.8`` (the
        end-user) and NOT the peer host (which is a CF edge IP or an
        intermediate hop, depending on the topology).
        """
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="185.222.108.123",  # representative CF edge / hop IP
            headers=_CF_FINGERPRINT_HEADERS,
        )
        self.assertEqual(trusted_client_ip(req), "8.8.8.8")

    def test_forged_cf_ip_without_cf_ray_falls_back_to_peer(self):
        """An attacker spoofing only CF-Connecting-IP must be ignored.

        Without ``CF-Ray`` (or ``X-Forwarded-Proto: https``) the request
        doesn't carry a real CF fingerprint, so the function must return
        the actual peer host. The downstream Stripe IP allowlist then
        sees the attacker's IP and rejects.
        """
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={
                "cf-connecting-ip": "3.18.12.63",
                # NB: no cf-ray, no x-forwarded-proto
            },
        )
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_forged_cf_ip_with_cf_ray_but_no_https_falls_back(self):
        """Missing X-Forwarded-Proto: https — fingerprint incomplete."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={
                "cf-connecting-ip": "3.18.12.63",
                "cf-ray": "8a1b2c3d4e5f6789-EWR",
                # x-forwarded-proto missing or http
            },
        )
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_forged_cf_ip_with_xfp_http_falls_back(self):
        """X-Forwarded-Proto: http — not the CF TLS edge."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="203.0.113.7",
            headers={
                "cf-connecting-ip": "3.18.12.63",
                "cf-ray": "8a1b2c3d4e5f6789-EWR",
                "x-forwarded-proto": "http",
            },
        )
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_direct_origin_no_headers_returns_peer(self):
        """No CF headers at all — return the peer host directly."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(peer="203.0.113.7", headers={})
        self.assertEqual(trusted_client_ip(req), "203.0.113.7")

    def test_xfp_with_chained_https_first_hop_is_trusted(self):
        """Some proxies stack X-Forwarded-Proto as a comma list. CF is
        the outermost edge, so the first value should be ``https``."""
        from middleware.subproduct import trusted_client_ip

        req = _FakeRequest(
            peer="185.222.108.123",
            headers={
                "cf-connecting-ip": "8.8.8.8",
                "cf-ray": "8a1b2c3d4e5f6789-EWR",
                "x-forwarded-proto": "https, http",
            },
        )
        self.assertEqual(trusted_client_ip(req), "8.8.8.8")


class TestExtractClientIPCFFingerprint(unittest.TestCase):
    """The Stripe webhook helper inherits the new fingerprint logic
    because it delegates to ``trusted_client_ip``."""

    def test_real_cf_request_extracts_cf_connecting_ip(self):
        """End-to-end: CF trio attached, end-user IP surfaces."""
        from stripe_webhook_hardening import extract_client_ip

        req = _FakeRequest(
            peer="185.222.108.123",
            headers=_CF_FINGERPRINT_HEADERS,
        )
        self.assertEqual(extract_client_ip(req), "8.8.8.8")

    def test_real_cf_stripe_egress_passes_allowlist(self):
        """Genuine Stripe webhook through CF: peer is a CF hop, CF-
        Connecting-IP is a Stripe egress, and the allowlist accepts.

        This is the prod path the 2026-05-15 revision unblocks — the
        previous loopback-only gate would have 403'd this request.
        """
        from stripe_webhook_hardening import extract_client_ip, reject_non_stripe_ip

        os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "1"
        try:
            req = _FakeRequest(
                peer="185.222.108.123",
                headers={
                    "cf-connecting-ip": "3.18.12.63",  # real Stripe egress
                    "cf-ray": "8a1b2c3d4e5f6789-EWR",
                    "x-forwarded-proto": "https",
                },
            )
            ip = extract_client_ip(req)
            self.assertEqual(ip, "3.18.12.63")
            self.assertIsNone(
                reject_non_stripe_ip(ip),
                "legitimate CF-fronted Stripe webhook must pass allowlist",
            )
        finally:
            os.environ.pop("STRIPE_IP_ALLOWLIST_ENFORCE", None)

    def test_forged_cf_ip_no_fingerprint_blocked_by_allowlist(self):
        """Forged CF-Connecting-IP without the trio still rejected.

        This is the same threat model as
        TestExtractClientIPDelegates.test_off_tunnel_blocks_stripe_webhook_allowlist
        but in the new fingerprint regime: missing CF-Ray means the
        fingerprint check fails and the peer host (TEST-NET-3) flows
        through to ``reject_non_stripe_ip``, which 403s.
        """
        from stripe_webhook_hardening import extract_client_ip, reject_non_stripe_ip

        os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "1"
        try:
            req = _FakeRequest(
                peer="203.0.113.7",
                headers={
                    "cf-connecting-ip": "3.18.12.63",
                    # cf-ray missing → fingerprint incomplete
                },
            )
            ip = extract_client_ip(req)
            self.assertEqual(ip, "203.0.113.7")
            response = reject_non_stripe_ip(ip)
            self.assertIsNotNone(response)
            self.assertEqual(getattr(response, "status_code", None), 403)
        finally:
            os.environ.pop("STRIPE_IP_ALLOWLIST_ENFORCE", None)


# ── SubproductMiddleware direct-origin guard ──────────────────────────────


async def _run_middleware(request, *, production: bool):
    """Drive SubproductMiddleware.dispatch directly with a fake Request.

    The standard ``TestClient`` always synthesises ``client=("testclient",
    …)`` so the trusted-peer fast path masks the fingerprint check. We
    want to exercise the rejection branch with a non-loopback peer, so
    we build a Starlette ``Request`` from a raw ASGI scope where the
    client tuple is whatever we want, then await ``dispatch`` directly.
    """
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse

    from middleware.subproduct import SubproductMiddleware

    prev = os.environ.get("PRODUCTION")
    os.environ["PRODUCTION"] = "1" if production else "0"
    try:
        async def _ok(_req):
            slug = getattr(_req.state, "subproduct", None)
            return PlainTextResponse(f"ok:{slug or '-'}")

        mw = SubproductMiddleware(app=_ok)
        # ``call_next`` is normally ``self.app`` wrapped by
        # ``BaseHTTPMiddleware``; for a unit test we can pass the route
        # callable directly — it takes a Request and returns a Response.
        return await mw.dispatch(request, _ok)
    finally:
        if prev is None:
            os.environ.pop("PRODUCTION", None)
        else:
            os.environ["PRODUCTION"] = prev


def _make_request(*, host: str, peer: str, headers: dict | None = None):
    """Construct a real Starlette ``Request`` with the given peer + host."""
    from starlette.requests import Request

    raw_headers = [(b"host", host.encode("ascii"))]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("ascii"), v.encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/__probe",
        "raw_path": b"/__probe",
        "query_string": b"",
        "headers": raw_headers,
        "client": (peer, 12345),
        "server": ("origin", 8000),
    }
    return Request(scope)


class TestSubproductMiddlewareDirectOriginGuard(unittest.TestCase):
    """The middleware-level direct-origin guard must apply the same
    CF-ingress logic as ``trusted_client_ip`` — otherwise prod 403s
    on every real request again.

    These tests drive the real ``SubproductMiddleware.dispatch`` against
    a synthesised Starlette ``Request`` so we can control the peer host
    (TestClient hardcodes loopback) and cover the rejection branch.
    """

    def setUp(self) -> None:
        self._orig_production = os.environ.get("PRODUCTION")

    def tearDown(self) -> None:
        if self._orig_production is None:
            os.environ.pop("PRODUCTION", None)
        else:
            os.environ["PRODUCTION"] = self._orig_production

    def _dispatch(self, request, production: bool):
        import asyncio
        return asyncio.run(_run_middleware(request, production=production))

    def test_prod_request_with_cf_fingerprint_passes(self):
        """Real CF traffic with full trio: non-loopback peer is fine."""
        req = _make_request(
            host="narve.ai",
            peer="185.222.108.123",  # CF edge / hop, NOT loopback
            headers=_CF_FINGERPRINT_HEADERS,
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 200)

    def test_prod_request_forged_cf_ip_only_is_rejected(self):
        """Direct-origin probe: only CF-Connecting-IP set, peer is
        attacker host, no CF-Ray, no XFP — 403."""
        req = _make_request(
            host="narve.ai",
            peer="203.0.113.7",  # TEST-NET-3, definitely not loopback
            headers={"cf-connecting-ip": "3.18.12.63"},
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 403)

    def test_prod_request_no_headers_at_all_rejected(self):
        """No CF headers, non-loopback peer → 403."""
        req = _make_request(
            host="narve.ai",
            peer="203.0.113.7",
            headers={},
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 403)

    def test_prod_request_partial_fingerprint_rejected(self):
        """CF-Connecting-IP + CF-Ray but no X-Forwarded-Proto: https → 403.

        Pins the requirement that ALL THREE headers must be present —
        an attacker who knows about CF-Ray but not XFP shouldn't be
        able to slip through.
        """
        req = _make_request(
            host="narve.ai",
            peer="203.0.113.7",
            headers={
                "cf-connecting-ip": "8.8.8.8",
                "cf-ray": "8a1b2c3d4e5f6789-EWR",
                # x-forwarded-proto absent
            },
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 403)

    def test_prod_request_loopback_peer_with_cf_header_passes(self):
        """Legacy on-box cloudflared path: loopback peer + CF header,
        no CF-Ray needed — the trusted-peer fast path accepts."""
        req = _make_request(
            host="narve.ai",
            peer="127.0.0.1",
            headers={"cf-connecting-ip": "8.8.8.8"},
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 200)

    def test_non_prod_request_no_headers_passes(self):
        """Outside production the guard is off entirely (dev / staging)."""
        req = _make_request(
            host="narve.ai",
            peer="203.0.113.7",
            headers={},
        )
        resp = self._dispatch(req, production=False)
        self.assertEqual(resp.status_code, 200)

    def test_prod_request_attaches_subproduct_state(self):
        """Happy-path also sets request.state.subproduct correctly."""
        req = _make_request(
            host="narve.ai",
            peer="185.222.108.123",
            headers=_CF_FINGERPRINT_HEADERS,
        )
        resp = self._dispatch(req, production=True)
        self.assertEqual(resp.status_code, 200)
        # apex narve.ai → subproduct is None per subproduct_for_host
        self.assertIsNone(getattr(req.state, "subproduct", "sentinel"))


if __name__ == "__main__":
    unittest.main()
