"""End-to-end smoke test: walk every registered route and verify it
returns a non-5xx response.

This is a regression net — when someone adds a new endpoint that
crashes on first request, this test catches it without needing
endpoint-specific knowledge.

Routes that take path params, require POST bodies, or hit external
APIs are skipped or stubbed explicitly. Everything else is hit with
a default GET and expected to return < 500.
"""
import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

import sports_dashboard as sd


# Routes that require complex inputs or POST bodies — exercised by
# dedicated tests in their own files. Skip in the universal smoke test.
SKIP_PATHS = {
    # WebSocket — needs a real WS client
    "/ws",
    # External API hits that 5xx in sandbox (Polymarket, Odds API)
    "/api/orderbook/{token_id}",
    "/api/orderbook-depth/{token_id}",
    # Path-parametrized routes — exercised by feature-specific tests
    "/api/sport/{sport_key}",
    "/api/market-history/{event_name}",
    "/api/trades/{trade_id}/resolve",
    "/api/trades/{trade_id}",
    "/api/watchlist/{item_id}",
    "/api/alert-rules/{rule_id}",
    "/api/auth/tokens/{token_id}",
    # Mounted static files — not Sharpe code
    "/static",
}

# Routes that legitimately return a non-2xx response on a bare GET.
# Each value is the SET of accepted status codes for that path.
# Listed explicitly so the smoke test is deliberate about what it
# considers acceptable.
EXPECTED_NON_2XX = {
    "/api/admin/set-tier": {405},     # POST-only
    "/api/admin/users": {403, 200},
    "/api/admin/stats": {403, 200},
    "/admin": {200, 403, 404},
    "/users": {200, 403, 404},
    # POST-only endpoints
    "/api/push/test": {405},
    "/api/webhooks/test": {405, 400},
    "/api/push/subscribe": {405},
    "/api/leaderboard/optin": {405, 200},
    "/api/bankroll/suggest-stake": {405},
    "/api/signals/explain": {405},
    "/api/backtest/replay": {405},
    "/api/alerts/test": {405},
    "/api/flag-match": {405},
    "/api/trades": {200, 405},
    "/api/alert-rules": {200, 405},
    "/api/auth/tokens": {200, 405},
    "/api/watchlist": {200, 405},
    "/api/webhooks/signing-key": {405},
    "/api/bankroll": {200, 405},
    "/api/logout": {200, 302},
    "/api/me": {200, 401},
    # Auth + readiness routes that legitimately 302 / 503
    "/login": {302},                                # redirects to gateway login
    "/readyz": {200, 503},                          # 503 if data_updater hasn't run
    "/api/push/vapid-public-key": {200, 503},       # 503 when VAPID unset (sandbox)
}


def _path_takes_params(path: str) -> bool:
    """True if the path contains a {param} placeholder."""
    return "{" in path


def test_every_get_route_responds_without_5xx():
    """Walk every registered GET route. Each should return a non-5xx
    response on a bare anonymous-or-DEV-MODE request.

    Catches:
      - Routes whose handlers raise on first call (typo, import error)
      - Routes that depend on uninitialized state
      - Routes that try to hit an external API at request time without
        an env var configured (those should 503 cleanly, not 500)
    """
    # Disable redirect-following: a 302 should be visible as a 302,
    # not chased until max-redirects (some routes redirect to external
    # hosts like the gateway login page).
    client = TestClient(sd.app, follow_redirects=False)
    paths_checked = 0
    failures: list[str] = []

    for route in sd.app.routes:
        # Skip WebSockets — need a different client
        if isinstance(route, WebSocketRoute):
            continue
        # Skip non-API routes (e.g. static mounts)
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if path in SKIP_PATHS:
            continue
        if _path_takes_params(path):
            continue
        # Only test routes that accept GET
        if "GET" not in route.methods:
            continue

        try:
            r = client.get(path)
        except Exception as e:
            failures.append(f"{path}: raised {type(e).__name__}: {e}")
            continue

        expected = EXPECTED_NON_2XX.get(path)
        if expected and r.status_code in expected:
            paths_checked += 1
            continue

        if r.status_code >= 500:
            try:
                snippet = r.text[:200]
            except Exception:
                snippet = "<no body>"
            failures.append(f"{path}: HTTP {r.status_code} — {snippet!r}")
        else:
            paths_checked += 1

    assert not failures, (
        f"{len(failures)} route(s) returned 5xx or raised:\n"
        + "\n".join(failures)
    )
    # Sanity: we should have actually checked a meaningful number of routes
    assert paths_checked >= 20, (
        f"smoke test only checked {paths_checked} routes — "
        "did the route registry shrink unexpectedly?"
    )


def test_no_route_path_is_dangerously_generic():
    """Catch accidental routes like / catching everything because of a
    typo elsewhere. We have a known list of root-level paths; anything
    else at root should be deliberate."""
    KNOWN_ROOT_PATHS = {
        "/", "/login", "/logout", "/healthz", "/readyz", "/metrics",
        "/manifest.json", "/sw.js", "/favicon.png", "/settings",
        "/admin", "/users", "/features", "/changelog",
        "/track-record", "/leaderboard", "/signal-history",
        "/player-props", "/cross-book-arbitrage", "/smart-money",
        "/poly-fills", "/steam-moves", "/trades", "/backtest",
    }
    root_paths = set()
    for route in sd.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        # Top-level (not /api/...) and not parametrized
        if path.count("/") == 1 and "{" not in path:
            root_paths.add(path)

    unexpected = root_paths - KNOWN_ROOT_PATHS
    assert not unexpected, (
        f"Unexpected root-level routes: {unexpected}. "
        "If new, add to KNOWN_ROOT_PATHS in this test."
    )


def test_route_count_in_expected_range():
    """Lock route count in a band so adding routes is intentional.
    If you intentionally add a route, bump this assert. If a route
    disappears without being intentionally removed, this fails."""
    n = sum(1 for r in sd.app.routes if isinstance(r, APIRoute))
    assert 70 <= n <= 200, (
        f"unexpected route count {n} — adjust the band if intentional"
    )
