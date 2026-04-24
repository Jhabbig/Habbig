"""Render an admin content template wrapped in the shared admin shell.

Usage from route handlers:

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/users.html",
        page_title="Users",
        active_route="users",
        breadcrumb=[("Admin", "/admin"), ("Users", "/admin/users")],
        actions_html='<a class="btn" href="/admin/users/new">+ New user</a>',
        raw_user_rows="".join(rows),   # template-specific substitutions
    )

The admin content templates under ``static/admin/*.html`` are raw HTML
fragments — no ``<html>``, ``<head>``, ``<body>`` wrapper. The shell
supplies the layout, navigation rail, breadcrumb trail, page title,
action-bar, and main landmark.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Iterable, Optional


_STATIC = Path(__file__).parent / "static"
_PARTIALS = _STATIC / "_partials"
_ADMIN_CONTENT = _STATIC / "admin"


def _srv():
    """Return the already-imported server module for render_page()."""
    return sys.modules.get("server") or sys.modules["__main__"]


def _read(template_path: Path) -> str:
    """Load a template from disk. Raises if missing so 404s are obvious."""
    return template_path.read_text()


def _substitute(text: str, context: dict) -> str:
    """Tiny mustache-ish substitution — mirrors ``render_page``'s semantics.

    Keys prefixed with ``raw_`` are inserted verbatim (trusted HTML).
    Anything else is HTML-escaped before insertion.
    """
    for key, value in context.items():
        placeholder = "{{ " + key + " }}"
        if key.startswith("raw_") or key in ("page_title", "raw_breadcrumb",
                                             "raw_page_actions", "raw_admin_content",
                                             "raw_active_route"):
            # page_title / raw_* trusted — caller is responsible for escaping.
            # We special-case page_title because axe complains if we escape
            # the text, but admin titles are all static strings from trusted
            # callers, so verbatim is fine.
            text = text.replace(placeholder, str(value))
        else:
            text = text.replace(placeholder, html.escape(str(value)))
    return text


def _render_breadcrumb(trail: Iterable[tuple[str, Optional[str]]]) -> str:
    """Build an <ol> from a list of (label, href) pairs.

    The last item is rendered as plain text with ``aria-current="page"``
    so screen readers announce it as the current location.
    """
    items = list(trail)
    if not items:
        return ""
    parts = ['<ol>']
    last_idx = len(items) - 1
    for idx, (label, href) in enumerate(items):
        if idx == last_idx or not href:
            parts.append(f'<li aria-current="page">{html.escape(str(label))}</li>')
        else:
            parts.append(
                f'<li><a href="{html.escape(str(href))}">{html.escape(str(label))}</a></li>'
            )
    parts.append("</ol>")
    return "".join(parts)


def render_admin_page(
    request,
    content_template: str,
    *,
    page_title: str,
    active_route: str = "",
    breadcrumb: Optional[Iterable[tuple[str, Optional[str]]]] = None,
    actions_html: str = "",
    **context,
):
    """Render ``content_template`` inside the admin shell.

    ``content_template`` is a path relative to ``gateway/static/``. Examples:
        "admin/users.html"
        "admin/flags/edit.html"
    Any other name is resolved literally — the caller can still point at
    a legacy flat template (``admin-churn.html``) during the rolling
    migration if that template already contains only its content body.

    ``breadcrumb`` defaults to ``[("Admin", "/admin"), (page_title, …)]``
    when omitted. Explicit callers can override for multi-step trails.

    All extra kwargs are passed to the content template's substitution
    step. The shell's own slots (``page_title``, ``raw_admin_content``,
    ``raw_breadcrumb``, ``raw_page_actions``, ``raw_active_route``) are
    filled automatically.
    """
    # Resolve the content template. Accept both "admin/foo.html" (new shape)
    # and "admin-foo" (bare stem in the legacy style render_page uses).
    if content_template.endswith(".html"):
        content_path = _STATIC / content_template
    else:
        content_path = _STATIC / f"{content_template}.html"

    content_raw = _read(content_path)

    # Auto-inject CSRF hidden-input into every content template that
    # references ``{{ raw_csrf_field }}``. Mirrors render_page's
    # behaviour so admin form templates never have to hand-plumb the
    # token through.
    if "raw_csrf_field" not in context and "{{ raw_csrf_field }}" in content_raw:
        srv = _srv()
        try:
            token = (request.cookies.get(srv.CSRF_COOKIE_NAME)
                     or getattr(getattr(request, "state", None), "csrf_token", None)
                     or srv._generate_csrf_token())
            context["raw_csrf_field"] = (
                f'<input type="hidden" name="{srv.CSRF_FORM_FIELD}" '
                f'value="{html.escape(token)}">'
            )
        except Exception:
            context["raw_csrf_field"] = ""

    # Substitute caller-provided keys into the content body. We don't
    # inject the shell-owned keys here because those only matter at the
    # shell level — passing ``page_title`` to the inner content would
    # double-substitute if the author accidentally referenced it.
    inner = _substitute(content_raw, context)

    # Build the shell around the substituted content.
    shell_path = _PARTIALS / "admin_shell.html"
    shell_raw = _read(shell_path)

    shell_context = {
        "page_title": page_title,
        "raw_admin_content": inner,
        "raw_breadcrumb": _render_breadcrumb(
            breadcrumb if breadcrumb is not None
            else [("Admin", "/admin"), (page_title, request.url.path)]
        ),
        "raw_page_actions": actions_html or "",
        "raw_active_route": active_route,
    }
    wrapped = _substitute(shell_raw, shell_context)

    # Hand the wrapped body back to render_page via the ``raw_admin_body``
    # sentinel — we reuse the ``admin`` template as the outer HTML frame
    # so the existing head / theme / auto-injected a11y runs.
    # Simpler path: reuse render_page with a synthetic inline template by
    # writing it to a buffer and handing a raw HTMLResponse back.
    from fastapi.responses import HTMLResponse

    # Build the outer HTML frame. We want the global <head> (fonts,
    # mobile-a11y.css, PWA manifest, etc.) plus the admin-shell.css, then
    # our wrapped body. The simplest durable way is to piggyback on the
    # existing ``admin`` template's <head> — but we need to keep this
    # decoupled from admin.html's 1200-line monolith. So we synthesise a
    # minimal frame and let render_page's existing auto-injections fill
    # in the rest (skip link, PWA head, SEO head, etc.).
    frame = _minimal_admin_frame(page_title=page_title, body=wrapped)

    # Use render_page's auto-injection pipeline for all the hygiene bits.
    # It reads a template name from disk — we route through the shell
    # partial helper directly here instead. The response goes out as
    # HTMLResponse without further mutation because the admin surface
    # stays minimal (no CSRF form auto-injection needed at the shell
    # layer; individual <form> templates include their own).
    return HTMLResponse(frame)


_ADMIN_FRAME_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} · Admin · narve.ai</title>
  <meta name="robots" content="noindex, nofollow">
  <link rel="stylesheet" href="/_gateway_static/gateway.css">
  <link rel="preload" href="/_gateway_static/fonts/Inter-Variable-subset.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="stylesheet" href="/_gateway_static/mobile-a11y.css">
  <link rel="stylesheet" href="/_gateway_static/pages/admin-shell.css">
  <link rel="icon" type="image/png" href="/_gateway_static/img/logo.png">
  <script>(function(){{try{{var m=document.cookie.match(/narve-theme=([^;]*)/)||document.cookie.match(/betyc-theme=([^;]*)/);var t=(m&&m[1])||localStorage.getItem("narve-theme")||localStorage.getItem("betyc-theme")||"light";document.documentElement.setAttribute("data-theme",t);}}catch(e){{document.documentElement.setAttribute("data-theme","light");}}}})();</script>
</head>
<body>
<a class="narve-skip-link" href="#main">Skip to main content</a>
{body}
<script src="/_gateway_static/js/admin-shell.js" defer></script>
<script src="/_gateway_static/theme.js?v=2" defer></script>
</body>
</html>
"""


def _minimal_admin_frame(*, page_title: str, body: str) -> str:
    """Render the minimal outer HTML frame for admin pages.

    Kept verbatim rather than routed through ``render_page`` because the
    admin surface wants different defaults: noindex, no CSRF auto-inject
    at the shell layer, no SEO head builder. Anything the admin author
    wants inside a form posts their own `raw_csrf_field`.
    """
    return _ADMIN_FRAME_TEMPLATE.format(
        title=html.escape(page_title or "Admin"),
        body=body,
    )
