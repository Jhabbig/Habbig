"""Public SEO content pages — /about, /how-it-works, /methodology, /faq,
/team, /press, /changelog.

Each route is a thin wrapper around render_page for a dedicated static
template. The templates carry their own per-page SEO (title, description,
canonical, Open Graph, Twitter card, schema.org JSON-LD) rather than going
through the seo.py builder, so every page can have a hand-tuned metadata
block without a lookup layer in between.

Registered from server.py via ``seo_routes.register(app)``.
"""

from __future__ import annotations

import logging
import sys
from fastapi import Request
from fastapi.responses import HTMLResponse

log = logging.getLogger("gateway.seo_routes")


# ── Deferred lookups into server.py ─────────────────────────────────────
#
# Mirrors admin_routes.py's pattern so we avoid circular imports at startup.


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


def _render(name: str, request, **ctx) -> HTMLResponse:
    return _srv().render_page(name, request=request, **ctx)


def _get_subdomain(request):
    return _srv().get_subdomain(request)


async def _proxy(request, path: str):
    return await _srv().proxy_request(request, path)


# ── Route handlers ──────────────────────────────────────────────────────
#
# Each handler defers to the matching static template. If the request
# arrived on a dashboard subdomain (sports.narve.ai/about, etc.), we
# proxy through to the child app so the same URL works everywhere —
# mirrors the pattern used by prerelease_page / landing_page in server.py.


async def about_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/about")
    return _render("about", request)


async def how_it_works_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/how-it-works")
    return _render("how_it_works", request)


async def methodology_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/methodology")
    return _render("methodology", request)


async def faq_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/faq")
    return _render("faq", request)


async def team_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/team")
    return _render("team", request)


async def press_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/press")
    return _render("press", request)


async def changelog_page(request: Request):
    if _get_subdomain(request):
        return await _proxy(request, "/changelog")
    return _render("changelog", request)


# ── Registration ────────────────────────────────────────────────────────


PATHS: tuple[tuple[str, object], ...] = (
    ("/about",        about_page),
    ("/how-it-works", how_it_works_page),
    ("/methodology",  methodology_page),
    ("/faq",          faq_page),
    ("/team",         team_page),
    ("/press",        press_page),
    ("/changelog",    changelog_page),
)


def register(app) -> None:
    """Wire every content route into the FastAPI app.

    Routes need to be in server._PUBLIC_PATHS so GateMiddleware lets anon
    visitors through; that extension is done in server.py alongside the
    register() call.
    """
    for path, handler in PATHS:
        app.add_api_route(
            path, handler,
            methods=["GET"],
            response_class=HTMLResponse,
            include_in_schema=False,
        )


# Path set that server.py merges into _PUBLIC_PATHS so the gate doesn't
# challenge anonymous SEO crawlers.
PUBLIC_PATHS: frozenset[str] = frozenset(p for p, _ in PATHS)
