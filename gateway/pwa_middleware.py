"""PWA + a11y HTML injection middleware.

Intercepts text/html responses and injects:
  - manifest link, theme-color, apple-mobile-web-app-* meta tags
  - mobile-a11y.css (loaded after gateway.css so it can override vars)
  - skip-to-content link + narve-app.js + shortcuts.js after <body>
  - <div class="main-content"> → <main id="main" role="main" tabindex="-1">

This sits in a middleware rather than ``render_page`` because the latter
is big and churns frequently. A middleware injection survives any
upstream refactor of render_page and works uniformly for every HTML
endpoint, whether it's SSR, static file, or generated inline.

Idempotent via sentinel comments so re-proxied responses don't double up.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


# Cache-bust query for the assets we inject. We compute it once at import
# time from the file's mtime — a bytewise mtime change (any edit) bumps
# the version automatically without anyone having to remember a
# `?v=N` ratchet. Cloudflare keys its cache on the full URL incl. query
# string, so a new value forces a fresh fetch on the next deploy.
def _asset_version(rel_path: str) -> str:
    try:
        p = Path(__file__).parent / "static" / rel_path
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"


_MOBILE_A11Y_VER = _asset_version("mobile-a11y.css")
_NARVE_POLISH_VER = _asset_version("narve-polish.css")
_NARVE_REDESIGN_VER = _asset_version("narve-redesign.css")
_NARVE_APP_VER = _asset_version("narve-app.js")
_SHORTCUTS_VER = _asset_version("shortcuts.js")
_FEEDBACK_BTN_VER = _asset_version("feedback_button.js")
_SHORTCUTS_DISC_VER = _asset_version("js/shortcuts-discovery.js")


_PWA_HEAD = (
    '<!--narve-pwa-head-->\n'
    '<link rel="manifest" href="/manifest.json">\n'
    '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#0d0d0d" media="(prefers-color-scheme: dark)">\n'
    '<meta name="color-scheme" content="light dark">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-title" content="narve.ai">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="format-detection" content="telephone=no">\n'
    f'<link rel="stylesheet" href="/_gateway_static/mobile-a11y.css?v={_MOBILE_A11Y_VER}">\n'
    # narve-polish: site-wide refinement layer (motion + focus rhythm).
    f'<link rel="stylesheet" href="/_gateway_static/narve-polish.css?v={_NARVE_POLISH_VER}">\n'
    # narve-redesign: substantive visual refresh — page-header type,
    # hub cards, sidebar density, hero scale, table chrome, auth
    # funnel, long-form pages. Loaded LAST so it wins on selector
    # specificity. See narve-redesign.css head comment.
    f'<link rel="stylesheet" href="/_gateway_static/narve-redesign.css?v={_NARVE_REDESIGN_VER}">\n'
)

_BODY_INJECT = (
    '<a class="narve-skip-link" href="#main">Skip to main content</a>\n'
    # Mobile sidebar drawer affordances. The hamburger + backdrop are
    # always rendered but CSS hides them whenever the page has no
    # `.sidebar` (public landings) or the viewport is desktop-wide.
    # narve-app.js wires the toggle handlers (click hamburger, click
    # backdrop, Escape) to flip ``.sidebar.open``. The matching CSS
    # in mobile-a11y.css is what actually slides the drawer in.
    '<button type="button" class="narve-hamburger" data-narve-hamburger '
    'aria-label="Open menu" aria-controls="narve-sidebar-drawer" aria-expanded="false">'
    '<svg width="20" height="20" viewBox="0 0 24 24" stroke="currentColor" '
    'stroke-width="2" fill="none" aria-hidden="true">'
    '<line x1="3" y1="6" x2="21" y2="6"/>'
    '<line x1="3" y1="12" x2="21" y2="12"/>'
    '<line x1="3" y1="18" x2="21" y2="18"/>'
    '</svg></button>\n'
    '<div class="narve-sidebar-backdrop" data-narve-sidebar-backdrop hidden></div>\n'
    f'<script src="/_gateway_static/narve-app.js?v={_NARVE_APP_VER}" defer></script>\n'
    f'<script src="/_gateway_static/shortcuts.js?v={_SHORTCUTS_VER}" defer></script>\n'
    # First-time discovery hint for the keyboard shortcut overlay. Loaded
    # AFTER shortcuts.js so window.narve.shortcuts is populated; the
    # discovery module bails immediately if the user already dismissed
    # the hint (localStorage flag).
    f'<script src="/_gateway_static/js/shortcuts-discovery.js?v={_SHORTCUTS_DISC_VER}" defer></script>\n'
    # Floating 💬 Feedback button — the script itself suppresses on
    # /token, /login, /admin, and /feedback so unauthed + redundant
    # surfaces don't render the FAB. Mounting here gets us site-wide
    # coverage without having to edit every template.
    f'<script src="/_gateway_static/feedback_button.js?v={_FEEDBACK_BTN_VER}" defer></script>\n'
)

_VIEWPORT_RE = re.compile(br'<meta\s+name="viewport"[^>]*>', re.IGNORECASE)
_BODY_OPEN_RE = re.compile(br'(<body[^>]*>)', re.IGNORECASE)
_MAIN_OPEN = b'<div class="main-content">'
_MAIN_OPEN_REPLACE = b'<main class="main-content" id="main" tabindex="-1" role="main">'
_MAIN_CLOSE_RE = re.compile(
    br'</div>(\s*(?:<!--[^-]*Status bar[^-]*-->\s*)?<(?:footer|div) class="status-bar")',
    re.IGNORECASE,
)


def _inject_into_html(body: bytes) -> bytes:
    """Apply the six PWA/a11y transforms to an HTML body. Idempotent."""
    # 1. PWA head block (before </head>)
    if b'narve-pwa-head' not in body:
        idx = body.rfind(b'</head>')
        if idx != -1:
            body = body[:idx] + _PWA_HEAD.encode() + body[idx:]

    # 2. Viewport normalisation
    body = _VIEWPORT_RE.sub(
        b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        body,
        count=1,
    )

    # 3. Skip link + shared runtime scripts right after <body>
    if b'narve-skip-link' not in body:
        def _body_repl(m: re.Match) -> bytes:
            return m.group(1) + b'\n' + _BODY_INJECT.encode()
        body = _BODY_OPEN_RE.sub(_body_repl, body, count=1)

    # 4. Promote main-content div → semantic <main>
    if _MAIN_OPEN in body:
        body = body.replace(_MAIN_OPEN, _MAIN_OPEN_REPLACE, 1)
        body = _MAIN_CLOSE_RE.sub(br'</main>\1', body, count=1)

    return body


class PWAInjectionMiddleware(BaseHTTPMiddleware):
    """Inject PWA + a11y glue into every HTML response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        # Only touch HTML; short-circuit everything else.
        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response

        # Streaming responses lose their body here — reassemble to a plain
        # Response so we can mutate it. Preserve status + headers.
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)
        new_body = _inject_into_html(body)

        headers = dict(response.headers)
        # Content-Length has to be recomputed; let Starlette do it.
        headers.pop("content-length", None)

        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type=ctype,
        )
