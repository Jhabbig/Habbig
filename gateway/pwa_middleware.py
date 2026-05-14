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

try:
    from gateway.subproduct import subproduct_for_host
except ImportError:  # pragma: no cover — direct import path in tests
    from subproduct import subproduct_for_host  # type: ignore


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


# Critical first-paint CSS. Inlined into the head so the visible chrome
# (app shell + sidebar + page header) can render before gateway.css (~73 KB)
# arrives. External stylesheets still load and override on cascade — this
# block exists only to defeat the FOUC and to put a paint on screen on
# slow networks. Keep it under ~4 KB compressed; if you need to expand
# it, drop something else first or move the rule to the external sheet.
_CRITICAL_CSS = (
    '<style>'
    ':root,:root[data-theme="light"]{'
    '--bg-base:#fff;--bg-surface:#fafafa;'
    '--text-primary:#0d0d0d;--text-secondary:#4a4a4a;--text-tertiary:#6e6e6e;--text-quaternary:#bbb;'
    '--border-ghost:#ebebeb;--border-subtle:#e0e0e0;--border-default:#ccc;--border-strong:#b0b0b0;'
    '--interactive-bg:#0d0d0d;--interactive-text:#fff;--interactive-ghost:rgba(0,0,0,.04);'
    '--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;--space-6:32px;--space-7:48px;--space-8:64px;'
    '--page-pad:24px;'
    '--font-body:"Source Serif 4",Georgia,"Times New Roman",serif;'
    '--font-ui:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",ui-sans-serif,system-ui,sans-serif;'
    '--bg:var(--bg-base);--surface:var(--bg-surface);--border:var(--border-default)'
    '}'
    ':root[data-theme="dark"]{'
    '--bg-base:#0d0d0d;--bg-surface:#111;'
    '--text-primary:#f0f0f0;--text-secondary:#b0b0b0;--text-tertiary:#909090;--text-quaternary:#6e6e6e;'
    '--border-ghost:#141414;--border-subtle:#1f1f1f;--border-default:#2a2a2a;--border-strong:#383838;'
    '--interactive-bg:#f0f0f0;--interactive-text:#0d0d0d;--interactive-ghost:rgba(240,240,240,.06)'
    '}'
    '*{box-sizing:border-box;margin:0;padding:0}'
    'html{color-scheme:light dark}'
    'html,body{font-family:var(--font-body);background:var(--bg-base);color:var(--text-primary);'
    '-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;'
    'min-height:100vh;min-height:100dvh;font-size:15px;line-height:1.5}'
    'a{color:var(--text-secondary);text-decoration:none}'
    '.app-shell{display:grid;grid-template-columns:240px 1fr;grid-template-rows:1fr 28px;'
    'grid-template-areas:"sidebar main" "sidebar status";min-height:100vh;min-height:100dvh;'
    'background:var(--bg-base);color:var(--text-primary)}'
    '.sidebar{grid-area:sidebar;display:flex;flex-direction:column;gap:var(--space-2);'
    'padding:var(--space-5) var(--space-3);background:var(--bg-surface);'
    'border-right:1px solid var(--border-default);min-height:100vh;min-height:100dvh;'
    'position:sticky;top:0}'
    '.main-content{grid-area:main;padding:var(--space-4) var(--page-pad) var(--page-pad);'
    'max-width:100%;overflow-x:hidden}'
    '.page-header{margin-bottom:var(--space-5);padding-bottom:var(--space-4);'
    'border-bottom:1px solid var(--border-default)}'
    '.page-title{font-size:28px;font-weight:500;letter-spacing:-0.02em;'
    'color:var(--text-primary);margin:0 0 var(--space-2)}'
    '.narve-fouc-hide,[hidden]{display:none!important}'
    '</style>\n'
)


_PWA_HEAD = (
    '<!--narve-pwa-head-->\n'
    + _CRITICAL_CSS +
    '<link rel="manifest" href="/manifest.json">\n'
    '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#0d0d0d" media="(prefers-color-scheme: dark)">\n'
    '<meta name="color-scheme" content="light dark">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-title" content="narve.ai">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="format-detection" content="telephone=no">\n'
    # Geist Mono is referenced by tokens.css via @font-face but isn't
    # preloaded anywhere else — declaring it here means the browser can
    # start the fetch in parallel with the CSS parse instead of waiting
    # until it discovers the @font-face rule. Inter preloads stay in
    # per-page heads (most templates already declare them); Instrument
    # Serif is loaded via Google Fonts on the handful of editorial pages
    # that need it and isn't worth a universal preload.
    '<link rel="preload" href="/_gateway_static/fonts/GeistMono-Variable.woff2" '
    'as="font" type="font/woff2" crossorigin>\n'
    # Hoist Instrument Serif Italic (display) + Source Serif 4 (body) to
    # site-wide so every page's --font-display and --font-body actually
    # render the right webfont instead of silently falling back to Georgia.
    # Per the 2026-05-14 redesign-review: previously only 4/14 redesigned
    # pages loaded these via per-page links.
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@1'
    '&family=Source+Serif+4:opsz,wght@8..60,200..900&display=swap" rel="stylesheet">\n'
    f'<link rel="stylesheet" href="/_gateway_static/mobile-a11y.css?v={_MOBILE_A11Y_VER}">\n'
    # narve-polish: site-wide refinement layer (motion + focus rhythm).
    f'<link rel="stylesheet" href="/_gateway_static/narve-polish.css?v={_NARVE_POLISH_VER}">\n'
    # narve-redesign: substantive visual refresh — page-header type,
    # hub cards, sidebar density, hero scale, table chrome, auth
    # funnel, long-form pages. Loaded LAST so it wins on selector
    # specificity. See narve-redesign.css head comment.
    f'<link rel="stylesheet" href="/_gateway_static/narve-redesign.css?v={_NARVE_REDESIGN_VER}">\n'
)

# Default social card. Only injected when the response HTML doesn't
# already declare an og:image (per-page cards on profile_public,
# shared_*, _base.html-rendered pages keep their own). 1200×630 static
# PNG lives at gateway/static/og/default.png; the dynamic /og/default
# endpoint remains for templates that prefer the rendered card.
_OG_DEFAULT = (
    '<!--narve-og-default-->\n'
    '<meta property="og:image" content="https://narve.ai/_gateway_static/og/default.png" />\n'
    '<meta property="og:image:width" content="1200" />\n'
    '<meta property="og:image:height" content="630" />\n'
    '<meta name="twitter:card" content="summary_large_image" />\n'
    '<meta name="twitter:image" content="https://narve.ai/_gateway_static/og/default.png" />\n'
)

# Subproduct subdomains that have their own monochrome OG card under
# gateway/static/og/<dashboard_key>.png. When the request host matches one
# of these, we swap _OG_DEFAULT for a subproduct-specific block. The set is
# derived at import time from SUBPRODUCTS, filtered to keys that actually
# have a PNG on disk — so a new subproduct without a card silently falls
# back to default.png instead of 404'ing the og:image.
_OG_DIR = Path(__file__).parent / "static" / "og"


def _og_block_for_key(key: str) -> str:
    url = f"https://narve.ai/_gateway_static/og/{key}.png"
    return (
        f'<!--narve-og-{key}-->\n'
        f'<meta property="og:image" content="{url}" />\n'
        f'<meta property="og:image:width" content="1200" />\n'
        f'<meta property="og:image:height" content="630" />\n'
        f'<meta name="twitter:card" content="summary_large_image" />\n'
        f'<meta name="twitter:image" content="{url}" />\n'
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


def _inject_into_html(body: bytes, host: str | None = None) -> bytes:
    """Apply the six PWA/a11y transforms to an HTML body. Idempotent."""
    # 1. PWA head block (before </head>)
    if b'narve-pwa-head' not in body:
        idx = body.rfind(b'</head>')
        if idx != -1:
            body = body[:idx] + _PWA_HEAD.encode() + body[idx:]

    # 1b. og:image — only when the page doesn't already declare one. Per-page
    # cards (profile_public, shared_*, render_page _base head) keep theirs.
    # When the request host is a subproduct subdomain AND we have a PNG on
    # disk for it, inject the subproduct-specific block; otherwise fall back
    # to the apex default card.
    if b'og:image' not in body and b'narve-og-' not in body:
        og_block = _OG_DEFAULT
        if host:
            sub = subproduct_for_host(host)
            if sub:
                # Prefer the subdomain slug as filename (sports.png,
                # traders.png, voters.png …) and fall back to the
                # dashboard_key for the two products whose subdomain
                # and key disagree on disk (cb → centralbank.png,
                # health → world_health.png). Without the slug-first
                # check, `traders` looked for `top_traders.png` and
                # silently 404'd into default.png.
                slug = sub.get("slug")
                key = sub.get("dashboard_key")
                if slug and (_OG_DIR / f"{slug}.png").exists():
                    og_block = _og_block_for_key(slug)
                elif key and (_OG_DIR / f"{key}.png").exists():
                    og_block = _og_block_for_key(key)
        idx = body.rfind(b'</head>')
        if idx != -1:
            body = body[:idx] + og_block.encode() + body[idx:]

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
        host = request.headers.get("host") or (request.url.hostname or "")
        new_body = _inject_into_html(body, host=host)

        headers = dict(response.headers)
        # Content-Length has to be recomputed; let Starlette do it.
        headers.pop("content-length", None)

        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type=ctype,
        )
