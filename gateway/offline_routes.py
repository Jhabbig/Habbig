"""Offline & PWA settings routes.

Two routes:

    GET /offline           public standalone HTML shell the service worker
                           falls back to on cold-start network failures.
    GET /settings/offline  authed settings page with toggles for offline
                           mode + push notifications + cache/storage UI.

Registered via reload-safe late-import at the bottom of server.py (same
pattern as status_routes / push_routes). Keeps module free of top-level
side effects beyond route registration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

import server
from server import app, render_page, current_user


log = logging.getLogger("offline_routes")


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_OFFLINE_HTML_PATH = _STATIC_DIR / "offline.html"


@app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
async def offline_shell(request: Request) -> HTMLResponse:
    """Public offline shell. No auth, no gate, no PWA-middleware injection.

    The service worker pre-caches this response on install. If a
    navigation request fails with no cache hit, the SW falls back here.
    The page itself reads `caches` at runtime and lists what's available
    for the user to navigate to offline.
    """
    try:
        body = _OFFLINE_HTML_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover — static file missing
        log.warning("offline.html missing: %s", exc)
        body = (
            "<!doctype html><meta charset='utf-8'><title>Offline</title>"
            "<body style='font-family:system-ui;padding:40px;text-align:center'>"
            "<h1>You're offline</h1>"
            "<p>Reconnect and refresh.</p></body>"
        )
    # Short cache — the SW owns the real caching, but a browser without
    # SW still gets a sane offline page.
    headers = {"Cache-Control": "public, max-age=300"}
    return HTMLResponse(body, headers=headers)


@app.get("/settings/offline", response_class=HTMLResponse, include_in_schema=False)
async def settings_offline_page(request: Request):
    """Settings page: offline mode + push notifications + storage.

    Requires a logged-in user. Pushes through render_page so the PWA
    middleware injects the header, skip-link, and narve-app.js (which
    the page's inline script relies on for narve.push.*).
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return render_page(
        "settings_offline",
        request=request,
        username=user.get("username") or user.get("email", ""),
        email=user.get("email", ""),
    )
