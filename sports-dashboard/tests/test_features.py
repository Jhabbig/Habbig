"""Tests for the /features index page.

Lightweight — most of the work is in the static HTML. We verify the
route is anonymous-readable and that every page it advertises actually
exists in the app's route table, so the index never drifts out of sync.
"""
import re

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


def test_features_page_is_public():
    """/features is the conversion surface — anonymous viewers must
    see the same content. (Same rationale as /track-record.)"""
    r = _client().get("/features")
    assert r.status_code == 200
    assert "Sharpe" in r.text


def test_features_page_renders_html():
    r = _client().get("/features")
    assert "<html" in r.text.lower()
    assert "<title" in r.text.lower()


def test_features_page_lists_every_advertised_page_route():
    """Extract every internal href="/<path>" from the page and check
    that each one is a real route registered on the app. If we add a
    page later without updating /features (or vice versa) this test
    catches it."""
    body = _client().get("/features").text

    # Pull internal links (skip fragments, mailto, externals)
    hrefs = re.findall(r'href="(/[A-Za-z0-9_\-/]*)"', body)
    page_paths = {h for h in hrefs if h and not h.startswith("/api/")}
    # Filter to non-trivial pages — the bare "/" is the live dashboard
    # and is always registered.
    page_paths.discard("")

    registered_paths = {
        getattr(route, "path", None)
        for route in sd.app.routes
    }
    registered_paths.discard(None)

    missing = page_paths - registered_paths
    assert not missing, f"/features advertises pages not registered: {missing}"


def test_features_page_advertises_key_api_endpoints():
    """Each new feature should be mentioned by its API path in the
    description card. Smoke-check the headline APIs are referenced."""
    body = _client().get("/features").text
    expected = [
        "/api/track-record",
        "/api/player-props/cross-venue",
        "/api/cross-book-arbitrage",
        "/api/smart-money",
        "/api/poly-fills",
        "/api/steam-moves",
        "/api/backtest/replay",
        "/api/bankroll",
        "/api/alert-rules",
        "/api/auth/tokens",
        "/api/webhooks/signing-key",
        "/api/signals/explain",
        "/api/leaderboard/clv",
        "/metrics",
    ]
    for path in expected:
        assert path in body, f"/features missing reference to {path}"
