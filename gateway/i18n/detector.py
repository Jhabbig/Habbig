"""Resolve the display language for a request.

Priority order (first match wins):

  1. ``?lang=es`` query-string override (also sets the ``lang`` cookie so
     the preference persists across page loads for unauthenticated users).
  2. ``users.preferred_language`` column on the authed session user.
  3. ``lang`` cookie set by a previous switch or query override.
  4. The ``Accept-Language`` header — the client's preferred ordering.
  5. ``DEFAULT`` (``"en"``).

The detector never raises. Every unsupported value falls through to the
next source.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from .translator import DEFAULT, SUPPORTED


log = logging.getLogger("i18n.detector")

LANG_COOKIE_NAME = "lang"


def normalise_lang(raw: Optional[str]) -> Optional[str]:
    """Lowercase + convert "pt_BR" / "pt_br" → "pt-br" style. Returns
    None for anything not in :data:`SUPPORTED`."""
    if not raw:
        return None
    s = str(raw).strip().lower().replace("_", "-")
    if s in SUPPORTED:
        return s
    # Match on primary tag. "pt-br" in the client's header wins over
    # "pt"; "pt" alone maps to "pt-br" because that's the only pt we
    # ship. If we add pt-pt later, extend this table.
    primary = s.split("-", 1)[0]
    if primary == "pt" and "pt-br" in SUPPORTED:
        return "pt-br"
    if primary in SUPPORTED:
        return primary
    return None


def parse_accept_language(header: str) -> list[tuple[str, float]]:
    """Parse an RFC-7231 Accept-Language header into ``[(tag, q), ...]``
    sorted by descending quality. Malformed entries are skipped.

    Example::

        >>> parse_accept_language("en-US,en;q=0.9,es;q=0.8")
        [('en-us', 1.0), ('en', 0.9), ('es', 0.8)]
    """
    if not header:
        return []
    items: list[tuple[str, float]] = []
    for part in header.split(","):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip().lower()
        if not tag:
            continue
        q = 1.0
        for p in params.split(";"):
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        items.append((tag, q))
    items.sort(key=lambda x: x[1], reverse=True)
    return items


def _first_supported(candidates: Iterable[Optional[str]]) -> Optional[str]:
    for c in candidates:
        norm = normalise_lang(c)
        if norm:
            return norm
    return None


def detect_language(request) -> str:
    """Resolve the language for *request*. Never raises.

    ``request`` is a Starlette/FastAPI Request. The detector reads
    query params, cookies, and Accept-Language without mutating the
    request. The caller is responsible for setting the ``lang``
    cookie when a query-param override comes in (see the
    ``/api/set-language`` route)."""
    # 1. Query override
    try:
        q = request.query_params.get("lang")
    except Exception:
        q = None
    norm = normalise_lang(q)
    if norm:
        return norm

    # 2. User preference (if authed session carries the hint)
    try:
        user = getattr(getattr(request, "state", None), "user", None)
        if isinstance(user, dict):
            norm = normalise_lang(user.get("preferred_language"))
            if norm:
                return norm
    except Exception:
        pass

    # 3. Cookie
    try:
        cookie = request.cookies.get(LANG_COOKIE_NAME)
    except Exception:
        cookie = None
    norm = normalise_lang(cookie)
    if norm:
        return norm

    # 4. Accept-Language header
    try:
        header = request.headers.get("accept-language") or ""
    except Exception:
        header = ""
    for tag, _q in parse_accept_language(header):
        norm = normalise_lang(tag)
        if norm:
            return norm

    # 5. Fallback
    return DEFAULT
