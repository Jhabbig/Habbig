#!/usr/bin/env python3
"""Print every public (no-auth) page URL under a given base URL.

Drives the accessibility audit (``A11Y_AUDIT.md``) — each line is fed
to ``npx @axe-core/cli`` to produce a per-page violation report. Also
useful as a smoke-test list for deploy-verification scripts.

Usage:
    python3 scripts/list_public_urls.py [base]

The list is curated rather than discovered from ``server.app.routes``
because (a) many routes need parameters (``/sources/{handle}``) and we
only want a representative example per shape, (b) we explicitly skip
endpoints that only make sense with auth (``/dashboard/*``, ``/admin/*``)
or POST semantics (``/api/newsletter``), and (c) dynamic paths pulled
from the DB (``/sources/<top-handle>``) need the DB to be populated,
which isn't guaranteed in every environment.

Non-HTML URLs (sitemap.xml, robots.txt, favicon.ico) are included so
that a combined wget / curl smoke pass exercises them too; axe-core
harmlessly ignores non-HTML responses.
"""

from __future__ import annotations

import sys


# HTML pages that must pass WCAG 2.1 AA.
# Keep alphabetised within each group so diffs are minimal.
HTML_PAGES: tuple[str, ...] = (
    # Root + landing
    "/",
    "/landing",
    "/narve",
    # Marketing + SEO
    "/about",
    "/calendar",
    "/changelog",
    "/faq",
    "/how-it-works",
    "/methodology",
    "/press",
    "/pricing",
    "/subscribe",
    "/support",
    "/suspended",
    "/team",
    # Legal
    "/dpa",
    "/privacy",
    "/terms",
    # Auth-flow entry points — anonymous rendering only
    "/enquire",
    "/forgot-password",
    "/gate",
    "/login",
    "/register",
    "/signup",
    "/token",
    # Status page (public uptime board)
    "/status",
    # PWA offline fallback (renders with no network)
    "/offline",
    # Public developer API docs
    "/api/docs",
)


# Non-HTML — included so curl smoke-scans cover them, but axe-core skips.
NON_HTML_PATHS: tuple[str, ...] = (
    "/robots.txt",
    # Sitemap lives at an obscure, non-guessable path (server._SITEMAP_PATH);
    # /sitemap.xml is deliberately not served. Submitted via Search Console.
    "/497951413996680578.xml",
    "/manifest.json",
    "/sw.js",
    "/favicon.ico",
    "/.well-known/security.txt",
)


# Dynamic shapes — print one representative example each. The slug/handle
# is documented so a reader knows it's a placeholder, not a real entity.
EXAMPLE_DYNAMIC: tuple[str, ...] = (
    "/sources/fedwatcher",   # any rated source handle
)


def main(base: str = "http://127.0.0.1:7000") -> int:
    base = base.rstrip("/")
    for path in HTML_PAGES:
        print(f"{base}{path}")
    for path in EXAMPLE_DYNAMIC:
        print(f"{base}{path}")
    for path in NON_HTML_PATHS:
        print(f"{base}{path}")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7000"
    sys.exit(main(base))
