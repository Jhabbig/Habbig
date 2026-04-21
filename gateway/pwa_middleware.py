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

import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


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
    '<link rel="stylesheet" href="/_gateway_static/mobile-a11y.css">\n'
)

_BODY_INJECT = (
    '<a class="narve-skip-link" href="#main">Skip to main content</a>\n'
    '<script src="/_gateway_static/narve-app.js" defer></script>\n'
    '<script src="/_gateway_static/shortcuts.js" defer></script>\n'
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
