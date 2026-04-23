"""Preferred-timezone resolution for the rendering layer.

Deliberately schema-free. The edge-case sweep's original constraint
was "no migrations" — so instead of adding a `users.preferred_timezone`
column we lean on:

  1. A non-HTTPOnly cookie (`narve_tz`) that the client-side JS sets
     once from `Intl.DateTimeFormat().resolvedOptions().timeZone`.
  2. A request header (`X-Timezone`) for API clients that don't
     preserve cookies.
  3. The `Cf-Timezone` header Cloudflare can inject when enabled.
  4. UTC fallback so render paths never see `None` for TZ.

The helper stays pure — no I/O, no DB reads. Handlers call it once
per request and pass the resolved TZ string into whatever they're
rendering (jinja filters, JSON date formatters, etc.).

IANA TZ database names ("Europe/London", "America/New_York") are
validated via ``zoneinfo.ZoneInfo`` — any attacker-controlled input
that doesn't resolve to a real TZ gets silently downgraded to UTC so
the render path can never crash on a malformed header.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request


log = logging.getLogger("security.timezones")


# Cookie name shared with static/narve-app.js. HTTP-only = False by
# design: the JS sets it from the browser's resolved TZ, so server
# write + client write both have to work.
COOKIE_NAME = "narve_tz"

# Cap on header / cookie length. Real IANA names max out around 32
# chars ("America/Argentina/Buenos_Aires"); 80 leaves headroom.
_MAX_LEN = 80


def _validate(name: str) -> Optional[str]:
    """Return the name if zoneinfo accepts it, else None.

    zoneinfo.ZoneInfo raises on unknown names; we swallow to keep the
    function side-effect-free. Uses a tiny in-function cache so the
    per-request happy path (the same few TZs over and over) doesn't
    re-load the tz database.
    """
    if not name or len(name) > _MAX_LEN:
        return None
    # Fast reject of obvious garbage before touching zoneinfo.
    if any(c.isspace() or c in "\0<>\"'" for c in name):
        return None
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Pre-3.9 Pythons — accept a short, alphanumeric-plus-slash name
        # on faith. Not great, but the fallback path never runs on the
        # current deploy (Python 3.11 everywhere).
        if all(c.isalnum() or c in "/_-+" for c in name):
            return name
        return None
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return None


def resolve_timezone(
    request: Request,
    *,
    default: str = "UTC",
    override: Optional[str] = None,
) -> str:
    """Return the caller's IANA timezone name.

    Resolution order:
      1. ``override`` argument — caller-supplied (e.g. from a stored
         profile) takes precedence over any header/cookie.
      2. ``X-Timezone`` request header — API clients set this.
      3. ``Cf-Timezone`` request header — Cloudflare can inject it.
      4. ``narve_tz`` cookie — set by JS on first page load.
      5. ``default`` (UTC).

    Every source is validated via ``zoneinfo``; invalid inputs fall
    through to the next source rather than failing the request.
    """
    candidates: list[Optional[str]] = [
        override,
        request.headers.get("x-timezone"),
        request.headers.get("cf-timezone"),
        request.cookies.get(COOKIE_NAME),
    ]
    for raw in candidates:
        if not raw:
            continue
        tz = _validate(raw.strip())
        if tz:
            return tz
    return default


def format_epoch(
    epoch: Optional[int],
    *,
    tz: str,
    fmt: str = "%Y-%m-%d %H:%M",
    placeholder: str = "—",
) -> str:
    """Format a Unix epoch in the given IANA TZ.

    Handlers that render HTML can use this without pulling in the full
    render_page machinery. ``placeholder`` is returned unchanged for
    None / zero / negative epochs — the usual sentinels for "not set".
    """
    if not epoch or epoch <= 0:
        return placeholder
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
    except ImportError:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(epoch).strftime(fmt) + " UTC"
    try:
        dt = _dt.datetime.fromtimestamp(epoch, tz=ZoneInfo(tz))
        return dt.strftime(fmt)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("format_epoch failed for tz=%s: %s", tz, exc)
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(epoch).strftime(fmt) + " UTC"


def set_cookie(response, tz: str) -> None:
    """Write ``narve_tz`` onto a response.

    Used by any handler that accepts a TZ from a form POST or body
    (e.g. a "Set timezone" settings field). Cookie is not HTTPOnly by
    design — the client JS also reads it to short-circuit future
    ``Intl`` resolves.
    """
    if not _validate(tz):
        return
    # 180-day lifetime, path=/ so every subdomain picks it up.
    try:
        response.set_cookie(
            key=COOKIE_NAME,
            value=tz,
            max_age=180 * 86400,
            path="/",
            samesite="Lax",
            httponly=False,
            secure=True,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("set_cookie failed: %s", exc)
