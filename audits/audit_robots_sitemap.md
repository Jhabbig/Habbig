# Audit — robots.txt & sitemap.xml

**Date:** 2026-05-15
**Auditor:** automated review
**Source:** https://narve.ai/robots.txt, https://narve.ai/sitemap.xml
**Scope:** verify robots.txt blocks sensitive paths; verify sitemap.xml exposes only public pages and leaks no admin/private URLs.

---

## 1. robots.txt — live contents

```
User-agent: *
Allow: /
Allow: /pricing
Allow: /terms
Allow: /privacy
Allow: /dpa
Allow: /about
Allow: /how-it-works
Allow: /methodology
Allow: /faq
Allow: /narve
Disallow: /admin/
Disallow: /api/
Disallow: /auth/
Disallow: /dashboards
Disallow: /dashboard/
Disallow: /token
Disallow: /login
Disallow: /signup
Disallow: /register
Disallow: /settings/
Disallow: /embed/
Disallow: /invite/
Sitemap: https://narve.ai/sitemap.xml
```

### Required Disallow checks

| Required path | Status | Evidence |
|---|---|---|
| `/admin` | PASS | `Disallow: /admin/` present (line 12) |
| `/api` | PASS | `Disallow: /api/` present (line 13) |
| `/token` | PASS | `Disallow: /token` present (line 17) |
| `/gate` | **FAIL** | No `Disallow: /gate` line. The pre-release token-gate page at `https://narve.ai/gate` is publicly reachable (HTTP 200, title "Access — narve.ai") and currently crawlable. |

### Additional observations

- `/dashboards` (no trailing slash) and `/dashboard/` (with trailing slash) are both listed. Good — covers both the hub route and child routes. Live `/dashboards` returns 302 (pre-release redirect), but the rule still correctly signals intent to crawlers.
- `/auth/` is disallowed (line 14), covering `/auth/login`, `/auth/callback`, etc. (verified: `/auth/login` returns 404 currently, but the directory pattern is the right block.)
- `/login`, `/signup`, `/register` are individually disallowed. Reasonable belt-and-suspenders, though `/auth/` likely already covers them depending on where they actually live.
- `Allow: /narve` (line 11) is present and the page returns 200 — confirmed public.

---

## 2. sitemap.xml — live contents

13 URLs total:

| URL | Priority | Public? | Live status |
|---|---|---|---|
| `https://narve.ai/` | 1.0 | yes | 200 |
| `https://narve.ai/landing` | 0.9 | **uncertain** | **302** (behind /gate redirect) |
| `https://narve.ai/pricing` | 0.8 | yes | 200 |
| `https://narve.ai/about` | 0.8 | yes | 200 |
| `https://narve.ai/how-it-works` | 0.8 | yes | 200 |
| `https://narve.ai/methodology` | 0.7 | yes | 200 |
| `https://narve.ai/faq` | 0.7 | yes | 200 |
| `https://narve.ai/changelog` | 0.7 | yes | 200 |
| `https://narve.ai/narve` | 0.7 | yes | 200 |
| `https://narve.ai/calendar` | 0.7 | **uncertain** | **302** (behind /gate redirect) |
| `https://narve.ai/terms` | 0.3 | yes | 200 |
| `https://narve.ai/privacy` | 0.3 | yes | 200 |
| `https://narve.ai/dpa` | 0.3 | yes | 200 |

### Admin / private URL leak check

| Pattern searched | Found in sitemap? |
|---|---|
| `/admin` | no |
| `/api` | no |
| `/auth` | no |
| `/dashboard` | no |
| `/dashboards` | no |
| `/token` | no |
| `/gate` | no |
| `/settings` | no |
| `/embed` | no |
| `/invite` | no |
| `/login` / `/signup` / `/register` | no |

**Verdict:** no admin or private URLs are leaked in the sitemap.

### Public-pages completeness check

Pages allowed by robots.txt vs. pages present in sitemap:

| Allowed path (robots.txt) | In sitemap? |
|---|---|
| `/` | yes |
| `/pricing` | yes |
| `/terms` | yes |
| `/privacy` | yes |
| `/dpa` | yes |
| `/about` | yes |
| `/how-it-works` | yes |
| `/methodology` | yes |
| `/faq` | yes |
| `/narve` | yes |

All 10 explicit `Allow` entries are present in the sitemap. The sitemap additionally includes `/`, `/landing`, `/changelog`, `/calendar` (not individually listed in robots.txt but covered by the default `Allow: /`).

---

## 3. Gaps

1. **HIGH — `/gate` is not disallowed.** The pre-release access page is publicly reachable and indexable. While the page itself is harmless, indexing it signals the pre-release state to crawlers and surfaces a token-entry form in search results. **Action:** add `Disallow: /gate` to robots.txt. (Pre-release content remains off-limits per task constraint — recommendation only.)
2. **MEDIUM — sitemap includes routes currently behind the gate.** `/landing` and `/calendar` are advertised at priority 0.9 / 0.7 but return 302 to `/gate` for unauthenticated visitors. Crawlers will hit the redirect and may drop the URLs or get confused about canonical destinations. **Action:** either (a) remove `/landing` and `/calendar` from sitemap until they're public, or (b) make them publicly crawlable. (Pre-release off-limits — recommendation only.)
3. **LOW — robots.txt blocks `/dashboards` (no slash) but sitemap convention would be `/dashboards/`.** Cosmetic; current behaviour is correct because the route doesn't have a trailing-slash variant served.
4. **LOW — no `Disallow:` entry for static error pages or `/health` / `/healthz`.** Not required (they're harmless), but worth confirming none of these expose stack traces.
5. **LOW — no `User-agent`-specific rules.** Acceptable for current scale, but if you later want to block aggressive AI crawlers (GPTBot, CCBot, ClaudeBot, etc.) selectively, the file is the place.

---

## 4. Summary

- robots.txt: **3 of 4** required disallow rules present. Missing: `/gate`.
- sitemap.xml: clean. No admin or private URLs leaked. All 10 public pages declared in `Allow:` are present.
- Two sitemap entries (`/landing`, `/calendar`) currently redirect to `/gate` for the public, which is a pre-release-state inconsistency rather than a security gap.
