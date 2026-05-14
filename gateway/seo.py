"""SEO head builder, sitemap.xml, and robots.txt for narve.ai.

Renders meta tags (title, description, OpenGraph, Twitter cards, canonical)
and optional schema.org JSON-LD for any public page. Handlers pass a light
context dict; `build_seo_head()` returns an HTML string that `render_page()`
injects before ``</head>``.

The sitemap and robots generators are also here so the rules for
"which paths are public / indexable" live in one place.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Optional

APEX = "https://narve.ai"
SITE_NAME = "narve.ai"
TWITTER_HANDLE = "@narveai"
DEFAULT_DESCRIPTION = (
    "Invite-only intelligence platform for serious Polymarket traders. "
    "Credibility-scored signals from social media, cross-referenced with "
    "live market odds."
)

# Paths excluded from the sitemap AND indexing. Keep this aligned with
# server._PUBLIC_PATHS — anything authed or private lives here.
NOINDEX_PATHS: tuple[str, ...] = (
    "/dashboard", "/dashboards", "/admin", "/api",
    "/login", "/register", "/token", "/gate", "/invite", "/signup",
    "/settings", "/leaderboard", "/embed", "/billing", "/profile",
    "/onboarding", "/account", "/enquire", "/support", "/contact", "/saved",
    "/signal-search", "/suspended", "/subscribe",
    "/forgot-password", "/reset-password",
    "/auth",
)


@dataclass
class SEO:
    """Per-page SEO context.

    ``canonical_path`` is the path (starting with ``/``) used to build the
    canonical URL and og:url. ``og_image_path`` is a path to the OG card
    endpoint (e.g. ``/og/source/fedwatcher``) or an absolute URL.
    """

    title: str = SITE_NAME
    description: str = DEFAULT_DESCRIPTION
    canonical_path: str = "/"
    og_image_path: str = "/og/default"
    og_type: str = "website"
    robots: str = "index, follow"
    jsonld: list[dict] = field(default_factory=list)


def _abs(path_or_url: str) -> str:
    """Return an absolute URL for an app-relative path, or pass through an absolute URL."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return APEX + path_or_url


def build_seo_head(seo: SEO) -> str:
    """Return an HTML string of meta tags for injection into ``<head>``.

    Escapes all user-controlled attributes. Returned string is safe to
    concatenate into the template before ``</head>``.
    """
    title = seo.title if seo.title.endswith(SITE_NAME) else f"{seo.title} · {SITE_NAME}"
    description = seo.description.strip().replace("\n", " ")
    canonical = _abs(seo.canonical_path)
    og_image = _abs(seo.og_image_path)

    # Escape for attribute contexts.
    e = html.escape
    parts: list[str] = [
        "<!--narve-seo-head-->",
        f'<title>{e(title)}</title>',
        f'<meta name="description" content="{e(description)}">',
        f'<meta name="robots" content="{e(seo.robots)}">',
        f'<link rel="canonical" href="{e(canonical)}">',
        # OpenGraph
        f'<meta property="og:site_name" content="{e(SITE_NAME)}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        f'<meta property="og:type" content="{e(seo.og_type)}">',
        f'<meta property="og:url" content="{e(canonical)}">',
        f'<meta property="og:image" content="{e(og_image)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        # Twitter
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:site" content="{e(TWITTER_HANDLE)}">',
        f'<meta name="twitter:title" content="{e(title)}">',
        f'<meta name="twitter:description" content="{e(description)}">',
        f'<meta name="twitter:image" content="{e(og_image)}">',
    ]
    for data in seo.jsonld:
        # json.dumps escapes </ to avoid script-tag breakout.
        body = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
        parts.append(f'<script type="application/ld+json">{body}</script>')
    return "\n".join(parts) + "\n"


# ── Schema.org builders ──────────────────────────────────────────────────────


def organization_schema() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "url": APEX,
        "logo": APEX + "/_gateway_static/img/logo.png",
        "description": DEFAULT_DESCRIPTION,
        "sameAs": [],
    }


def person_schema(handle: str, categories: Optional[list[str]] = None) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": f"@{handle}",
        "description": f"Prediction market source tracked by {SITE_NAME}",
        "url": f"{APEX}/sources/{handle}",
        "knowsAbout": categories or [],
    }


def website_schema() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": APEX,
        "description": DEFAULT_DESCRIPTION,
    }


# ── Sitemap ──────────────────────────────────────────────────────────────────


@dataclass
class SitemapEntry:
    path: str
    priority: float = 0.5
    changefreq: str = "weekly"
    lastmod: Optional[str] = None  # ISO date, optional


STATIC_SITEMAP: tuple[SitemapEntry, ...] = (
    SitemapEntry("/", priority=1.0, changefreq="weekly"),
    SitemapEntry("/pricing", priority=0.8, changefreq="monthly"),
    SitemapEntry("/calendar", priority=0.7, changefreq="daily"),
    SitemapEntry("/terms", priority=0.3, changefreq="yearly"),
    SitemapEntry("/privacy", priority=0.3, changefreq="yearly"),
    SitemapEntry("/dpa", priority=0.3, changefreq="yearly"),
)


def build_sitemap_xml(
    source_handles: Optional[list[str]] = None,
    static_entries: tuple[SitemapEntry, ...] = STATIC_SITEMAP,
) -> str:
    """Return a valid sitemap.xml string."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in static_entries:
        loc = html.escape(_abs(entry.path))
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        if entry.lastmod:
            lines.append(f"    <lastmod>{html.escape(entry.lastmod)}</lastmod>")
        lines.append(f"    <changefreq>{entry.changefreq}</changefreq>")
        lines.append(f"    <priority>{entry.priority:.1f}</priority>")
        lines.append("  </url>")
    for handle in source_handles or []:
        safe_handle = html.escape(handle)
        lines.append("  <url>")
        lines.append(f"    <loc>{_abs('/sources/' + safe_handle)}</loc>")
        lines.append("    <changefreq>weekly</changefreq>")
        lines.append("    <priority>0.6</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


# ── robots.txt ───────────────────────────────────────────────────────────────


ROBOTS_TXT = """\
User-agent: *
Allow: /
Allow: /pricing
Allow: /sources/
Allow: /calendar
Allow: /terms
Allow: /privacy
Allow: /dpa
Disallow: /dashboard/
Disallow: /dashboards
Disallow: /admin/
Disallow: /api/
Disallow: /api/v1/
Disallow: /token
Disallow: /register
Disallow: /login
Disallow: /signup
Disallow: /settings/
Disallow: /leaderboard
Disallow: /embed/
Disallow: /billing
Disallow: /profile
Disallow: /onboarding
Disallow: /account
Disallow: /enquire
Disallow: /support
Disallow: /contact
Disallow: /saved
Disallow: /signal-search
Disallow: /forgot-password
Disallow: /reset-password
Disallow: /gate
Disallow: /invite
Disallow: /auth/

Sitemap: https://narve.ai/sitemap.xml
"""
