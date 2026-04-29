"""URL constants for the QA walks.

Single source of truth for "what pages does the product have, and which
auth bucket does each one belong to". The Playwright walks parametrise
over these lists; the manual checklist in QA_WALKTHROUGH.md mirrors them
loosely so the two surfaces stay in sync as the route table grows.

Adding a route? Drop it into the right bucket here and every walk will
pick it up next run.
"""

from __future__ import annotations


# Public marketing + legal + status surface — must render for an
# anonymous visitor (no gate cookie, no session). The gate page itself
# is included so the gate redirect-loop test can exercise it.
UNAUTH_PAGES: list[str] = [
    "/",
    "/gate",
    "/login",
    "/signup",
    "/forgot-password",
    "/about",
    "/how-it-works",
    "/methodology",
    "/pricing",
    "/faq",
    "/changelog",
    "/team",
    "/press",
    "/impressum",
    "/privacy",
    "/terms",
    "/dpa",
    "/status",
    "/leaderboard",
    "/api/docs",
]


# Logged-in dashboards + settings + tools. Every route here must
# 200 for an authed user and either redirect or 401/403 for an
# anonymous one.
AUTH_PAGES: list[str] = [
    "/dashboards",
    "/saved",
    "/predictions",
    "/predictions/history",
    "/notifications",
    "/billing",
    "/profile",
    "/account",
    "/settings",
    "/settings/billing",
    "/settings/saved-views",
    "/settings/webhooks",
    "/settings/api-keys",
    "/settings/embeds",
    "/settings/privacy",
    "/settings/takes",
    "/signal-search",
    "/intelligence",
    "/calendar",
]


# Admin-only routes. Authed-but-not-admin must be blocked (403/302).
# Super-admin-only routes are NOT split out — the test asserts each
# admin route either accepts or redirects, not the granularity.
ADMIN_PAGES: list[str] = [
    "/admin",
    "/admin/affiliates",
    "/admin/audit-log",
    "/admin/churn",
    "/admin/emails",
    "/admin/equivalences",
    "/admin/flags",
    "/admin/impersonations",
    "/admin/incidents",
    "/admin/moderation",
    "/admin/security/bulk-fetches",
    "/admin/security/forensics",
    "/admin/sharing",
    "/admin/webhooks",
    "/admin/ai-usage",
    "/admin/feedback",
]


# Subset that the heavy walks (style / mobile / dark-mode / Lighthouse)
# run against. Keep it small enough to finish under a minute on a
# laptop while still exercising the canonical chrome variants:
#   /                — public marketing landing
#   /dashboards      — authed app shell with sidebar
#   /predictions     — authed list/table view
#   /admin           — admin shell with admin nav
CANONICAL_PAGES: list[str] = ["/", "/dashboards", "/predictions", "/admin"]
