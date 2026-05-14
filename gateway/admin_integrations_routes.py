"""Admin /admin/integrations — single-pane external-integration health.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``admin_jobs_routes``, ``admin_health_monitor_routes`` etc.).

Routes exposed:
    GET  /admin/integrations                       HTML page (admin shell)
    GET  /api/admin/integrations                   JSON snapshot
    POST /api/admin/integrations/{slug}/test       Test connection (where applicable)

Every route goes through ``server._require_admin_user``. POSTs ride the
global CSRF middleware. The "test" handler is intentionally per-slug so
it can short-circuit for integrations that need targeted probes
(Anthropic = SDK ping, Polymarket = HEAD on the public API, etc.).
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Any, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
from admin_shell import render_admin_page
from queries import integrations as integrations_q


log = logging.getLogger("admin_integrations")


# ── Snapshot endpoint ────────────────────────────────────────────────────


@server.app.get("/api/admin/integrations")
async def admin_integrations_api(request: Request) -> JSONResponse:
    """Return ``{integrations: {slug: row, ...}, count, generated_at}``."""
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")
    snapshot = integrations_q.get_integration_status()
    return JSONResponse({
        "integrations": snapshot,
        "count": len(snapshot),
        "generated_at": int(time.time()),
    })


# ── Test connection (per-slug live probes) ───────────────────────────────


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


async def _test_anthropic() -> dict[str, Any]:
    """Live probe — issue a minimal Claude call. Mockable: tests patch
    ``ai.client.get_async_client`` to return a stub.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set", "latency_ms": 0}
    try:
        from ai import client as ai_client
    except Exception as exc:
        return {"ok": False, "error": f"ai.client import failed: {exc}",
                "latency_ms": 0}
    started = _now_ms()
    try:
        sdk = ai_client.get_async_client()
        if sdk is None:
            return {"ok": False, "error": "SDK unavailable",
                    "latency_ms": _now_ms() - started}
        # Smallest possible call. Haiku, 1 token max, throwaway prompt.
        resp = await sdk.messages.create(
            model=ai_client.ANTHROPIC_MODELS.get(
                "extraction", "claude-haiku-4-5-20251001"
            ),
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
        latency = _now_ms() - started
        # We don't care about the body — just that the call returned.
        _ = getattr(resp, "id", None) or "ok"
        return {"ok": True, "latency_ms": latency}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200],
                "latency_ms": _now_ms() - started}


async def _test_polymarket() -> dict[str, Any]:
    base = os.environ.get("POLYMARKET_API_BASE",
                          "https://clob.polymarket.com").rstrip("/")
    return await _http_head(f"{base}/")


async def _test_kalshi() -> dict[str, Any]:
    base = os.environ.get(
        "KALSHI_API_BASE",
        "https://trading-api.kalshi.com/trade-api/v2",
    ).rstrip("/")
    # Kalshi's root sometimes 404s but their /exchange/status is public.
    return await _http_get(f"{base}/exchange/status")


async def _test_stripe() -> dict[str, Any]:
    """Stripe — call api.stripe.com/v1/balance with the secret key.

    No PII risk; balance read is a standard healthcheck pattern.
    """
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "STRIPE_SECRET_KEY not set", "latency_ms": 0}
    started = _now_ms()
    try:
        async with httpx.AsyncClient(timeout=5.0) as cli:
            r = await cli.get(
                "https://api.stripe.com/v1/balance",
                headers={"Authorization": f"Bearer {key}"},
            )
        latency = _now_ms() - started
        if r.status_code == 200:
            return {"ok": True, "latency_ms": latency}
        return {"ok": False, "error": f"status {r.status_code}",
                "latency_ms": latency}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200],
                "latency_ms": _now_ms() - started}


async def _test_cloudflare() -> dict[str, Any]:
    """Cloudflare — HEAD the local gateway /health.

    A working tunnel terminates at this process, so a 2xx on the loopback
    /health is the cheapest confirmation we can make without depending on
    outbound DNS.
    """
    return await _http_head("http://localhost:7000/health")


async def _http_head(url: str) -> dict[str, Any]:
    started = _now_ms()
    try:
        async with httpx.AsyncClient(timeout=5.0) as cli:
            r = await cli.head(url)
        latency = _now_ms() - started
        if 200 <= r.status_code < 400:
            return {"ok": True, "latency_ms": latency}
        return {"ok": False, "error": f"status {r.status_code}",
                "latency_ms": latency}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200],
                "latency_ms": _now_ms() - started}


async def _http_get(url: str) -> dict[str, Any]:
    started = _now_ms()
    try:
        async with httpx.AsyncClient(timeout=5.0) as cli:
            r = await cli.get(url)
        latency = _now_ms() - started
        if 200 <= r.status_code < 400:
            return {"ok": True, "latency_ms": latency}
        return {"ok": False, "error": f"status {r.status_code}",
                "latency_ms": latency}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200],
                "latency_ms": _now_ms() - started}


_TESTERS: dict[str, Any] = {
    "anthropic": _test_anthropic,
    "polymarket": _test_polymarket,
    "kalshi": _test_kalshi,
    "stripe": _test_stripe,
    "cloudflare": _test_cloudflare,
}


@server.app.post("/api/admin/integrations/{slug}/test")
async def admin_integrations_test(request: Request, slug: str) -> JSONResponse:
    """Run a live probe for the named integration.

    Returns ``{ok: bool, latency_ms: int, error?: str}``. Tests POST to
    this endpoint; the page wires it up to per-row buttons.
    """
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover
        raise HTTPException(status_code=403, detail="Admin required")
    tester = _TESTERS.get(slug)
    if tester is None:
        return JSONResponse(
            {"ok": False, "error": f"no test runner for {slug!r}",
             "latency_ms": 0},
            status_code=400,
        )
    result = await tester()
    result["slug"] = slug
    return JSONResponse(result)


# ── Renderer ─────────────────────────────────────────────────────────────


_GLYPHS = {
    integrations_q.STATUS_CONNECTED: ("●", "CONNECTED"),  # solid dot
    integrations_q.STATUS_DEGRADED:  ("○", "DEGRADED"),   # hollow dot
    integrations_q.STATUS_DOWN:      ("×", "DOWN"),       # ×
}


def _fmt_ts(t: Optional[int]) -> str:
    if not t:
        return "never"
    try:
        delta = int(time.time()) - int(t)
    except Exception:
        return "—"
    if delta < 60: return f"{delta}s ago"
    if delta < 3600: return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _render_details(details: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, value in details.items():
        if isinstance(value, int) and value > 1_000_000_000:
            # Likely a unix-second timestamp.
            display = _fmt_ts(value)
        else:
            display = "—" if value is None else str(value)
        parts.append(
            '<div class="int-detail">'
            f'<dt>{html.escape(str(label))}</dt>'
            f'<dd>{html.escape(display)}</dd>'
            '</div>'
        )
    return "".join(parts)


def _render_row(row: dict[str, Any]) -> str:
    status = row.get("status", integrations_q.STATUS_DOWN)
    glyph, label = _GLYPHS.get(status, _GLYPHS[integrations_q.STATUS_DOWN])
    slug = row.get("slug", "")
    testable = bool(row.get("testable"))
    test_btn = (
        f'<button type="button" class="int-test-btn" '
        f'data-slug="{html.escape(slug)}" aria-label="Test {html.escape(row.get("name",""))}">'
        f'Test connection</button>'
        if testable else ""
    )
    return (
        f'<article class="int-row" data-slug="{html.escape(slug)}" '
        f'data-status="{html.escape(status)}" id="int-row-{html.escape(slug)}">'
        '<header class="int-row-head">'
        f'<span class="int-glyph" aria-hidden="true">{glyph}</span>'
        f'<span class="int-status-label">{label}</span>'
        f'<h3 class="int-name">{html.escape(row.get("name",""))}</h3>'
        f'<span class="int-summary">{html.escape(str(row.get("summary","")))}</span>'
        f'<span class="int-actions">{test_btn}'
        f'<span class="int-test-result" data-slug="{html.escape(slug)}" aria-live="polite"></span>'
        '</span>'
        '</header>'
        '<dl class="int-details">'
        f'{_render_details(row.get("details") or {})}'
        '</dl>'
        '</article>'
    )


def _render_rows(snapshot: dict[str, dict[str, Any]]) -> str:
    # Render in a stable, intentional order — matches the spec order so
    # tests and humans see the same hierarchy.
    order = ("stripe", "anthropic", "polymarket", "kalshi",
             "smtp", "sentry", "betterstack", "cloudflare")
    return "".join(
        _render_row(snapshot[slug]) for slug in order if slug in snapshot
    )


# ── HTML page ────────────────────────────────────────────────────────────


@server.app.get("/admin/integrations", response_class=HTMLResponse)
async def admin_integrations_page(request: Request):
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    try:
        snapshot = integrations_q.get_integration_status()
    except Exception:  # pragma: no cover
        log.exception("admin_integrations_page: snapshot failed")
        snapshot = {}

    # Summary tally (connected / degraded / down counts).
    tally = {"connected": 0, "degraded": 0, "down": 0}
    for row in snapshot.values():
        tally[row.get("status", "down")] = tally.get(
            row.get("status", "down"), 0
        ) + 1

    return render_admin_page(
        request,
        "admin/integrations.html",
        page_title="Integrations",
        active_route="integrations",
        breadcrumb=[("Admin", "/admin"),
                    ("Integrations", "/admin/integrations")],
        raw_tally_connected=str(tally["connected"]),
        raw_tally_degraded=str(tally["degraded"]),
        raw_tally_down=str(tally["down"]),
        raw_integration_rows=_render_rows(snapshot),
        raw_integration_count=str(len(snapshot)),
    )
