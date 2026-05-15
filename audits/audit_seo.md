# Audit — SEO code (gateway/seo.py, gateway/seo_routes.py, server.py, server_features.py)

**Date:** 2026-05-15
**Auditor:** automated review
**Scope:** sitemap correctness, robots.txt enforcement, schema.org JSON-LD validity, canonical URL consistency. Pre-release page contents are **off-limits** per task constraint — observations about prerelease only call out drift, no edits proposed.
**Method:** synchronous bash + file reads against `gateway/seo.py`, `gateway/seo_routes.py`, `gateway/server.py` (robots/sitemap handlers around L3488/L3573), `gateway/server_features.py` (duplicate handlers around L750/L791), all `gateway/static/*.html` templates that declare SEO metadata, and `gateway/tests/test_seo.py`.

There is **no `gateway/seo/` directory** in this repo. SEO code lives in:

- `/Users/shocakarel/Habbig/gateway/seo.py` — builder library (mostly dead code, see HIGH-3)
- `/Users/shocakarel/Habbig/gateway/seo_routes.py` — `/about /how-it-works /methodology /faq /team /press /changelog` route registration
- `/Users/shocakarel/Habbig/gateway/server.py` — live `/robots.txt`, `/sitemap.xml`, `/narve`, `/landing`, `/` (prerelease), and the `_SITEMAP_ENTRIES` table at L3556–3570 + apex `_PUBLIC_PATHS` at L1410–1443
- `/Users/shocakarel/Habbig/gateway/server_features.py` — **second, duplicate** `/robots.txt` and `/sitemap.xml` handlers at L750/L791 (shadowed; see CRITICAL-1)
- `/Users/shocakarel/Habbig/gateway/static/*.html` — per-page `<title>`, `<meta description>`, `<meta property="og:*">`, `<link rel="canonical">`, `<script type="application/ld+json">`
- `/Users/shocakarel/Habbig/gateway/tests/test_seo.py` — only consumer of `seo.py`'s exported helpers other than `build_seo_head`

---

## Severity counts

| Severity | Count |
|---|---|
| CRITICAL | 1 |
| HIGH | 4 |
| MEDIUM | 6 |
| LOW | 5 |
| **Total findings** | **16** |

---

## Top 3 (action-ranked)

1. **CRITICAL-1 — Duplicate `/robots.txt` and `/sitemap.xml` handlers in `server_features.py` shadow nothing today but will silently win if registration order ever flips.** `gateway/server_features.py:750` and `:791` redefine routes that `gateway/server.py:3488` and `:3573` already own. FastAPI's first-match rule means whichever module's decorators run first becomes authoritative. The features module is imported at `server.py:8210` *after* the server handlers register, so today the server.py versions serve. But the two versions disagree on rules (the features version exposes only `/sources/`, `/terms`, `/privacy`, omits `Disallow: /auth/`, `Disallow: /token`, `Disallow: /login`, `Disallow: /signup`, `Disallow: /register`, `Disallow: /settings/`, `Disallow: /embed/`, `Disallow: /invite/`, etc.), and the features sitemap also reads `server.STATIC_DIR / "sitemap.xml"` as a short-circuit cache — if anyone ever drops a file there, the live dynamic sitemap is bypassed entirely. **Fix:** delete both handlers from `server_features.py` (L750–819).

2. **HIGH-1 — Schema.org JSON-LD on `/pricing` advertises £180/mo "narve.ai Pro" with `InStock` availability, but the product is in pre-release and inaccessible to the public.** `gateway/static/pricing.html:21-44` declares a `Product` with `Offer.price=180 GBP, availability=https://schema.org/InStock`. This is the page Google parses for rich-result eligibility. Combined with the pre-release prerelease.html declaring a *different* `SoftwareApplication` price of `75.00 GBP` (L41-46), the schema is internally inconsistent **and** falsely signals InStock to crawlers. Beyond schema correctness, an InStock-marked price for an off-limits product is the kind of misrepresentation Google can demote. **Fix:** (a) drop the `pricing.html` `Product` markup or set `availability=https://schema.org/PreOrder`; (b) reconcile the price across pricing.html and prerelease.html.

3. **HIGH-2 — Live `/sitemap.xml` (server.py:3556) advertises pages that 302-redirect to `/gate` for the public.** `/landing` (priority 0.9) and `/calendar` (priority 0.7) are publicly enumerated in the sitemap but both gate-redirect for unauthenticated visitors (confirmed in `gateway/audits/audit_robots_sitemap.md` from the previous review). Crawlers either drop the URLs or mark them as soft-404. This is identical to the previous audit's finding #2; **the gap remains open**. Recommendation only (pre-release off-limits): remove `/landing` and `/calendar` from `_SITEMAP_ENTRIES` until they're public, or de-gate them.

---

## Full findings

### CRITICAL

#### CRITICAL-1 — Duplicate `/robots.txt` and `/sitemap.xml` handlers

- **Files:** `gateway/server.py:3488,3573` (authoritative) vs `gateway/server_features.py:750,791` (shadow)
- **Risk:** maintenance hazard + silent override on import-order change. The two implementations have **different** Disallow lists, **different** static URL sets, and the features version checks a `static/sitemap.xml` file on disk that doesn't exist today but would silently take over if it ever did.
- **Evidence:**
  - `server.py:3514–3539` (apex robots) disallows `/auth/`, `/token`, `/login`, `/signup`, `/register`, `/settings/`, `/embed/`, `/invite/` plus `/dashboards`, `/dashboard/`, `/admin/`, `/api/`.
  - `server_features.py:807–818` (apex robots) only disallows `/admin/`, `/api/`, `/dashboard/`, `/gate`. **No `/auth/`, no `/token`, no `/login`, no `/signup`, no `/register`, no `/settings/`, no `/embed/`, no `/invite/`.**
  - `server_features.py:768–770` short-circuits on `gateway/static/sitemap.xml` — file does not exist, but the branch is live.
- **Action:** delete L750–819 from `server_features.py`. No other consumer references those names.

---

### HIGH

#### HIGH-1 — `pricing.html` declares `InStock` `Product` for an off-limits service

- **File:** `gateway/static/pricing.html:21-44`
- **Evidence:** `"availability": "https://schema.org/InStock"` with `priceCurrency: GBP, price: 180`.
- **Risk:** Google rich-results misrepresentation while the service is pre-release; price disagrees with `prerelease.html`'s `SoftwareApplication.offers.price=75.00 GBP`.
- **Action:** set `availability` to `https://schema.org/PreOrder` or remove the Product markup. Reconcile pricing across pricing.html, prerelease.html, and any downstream rich-result fixtures.

#### HIGH-2 — Sitemap advertises gate-redirected URLs (`/landing`, `/calendar`)

- **File:** `gateway/server.py:3557, 3566` in `_SITEMAP_ENTRIES`.
- **Risk:** soft-404 in Search Console; crawl budget waste; canonical confusion (sitemap says `/landing` is at priority 0.9 but actual prerelease canonical is `/`).
- **Action:** drop `/landing` and `/calendar` from `_SITEMAP_ENTRIES` until they're publicly reachable, or remove the pre-release gate from those paths. **(Pre-release off-limits — recommendation only.)**

#### HIGH-3 — `seo.py` exports `ROBOTS_TXT`, `STATIC_SITEMAP`, `build_sitemap_xml`, `NOINDEX_PATHS` that are only used by tests

- **Files:** `gateway/seo.py:30,157,167,199`; only consumer outside the module is `gateway/tests/test_seo.py:12`.
- **Risk:** the docstring at the top of `seo.py` claims it owns "which paths are public / indexable", but production reads from `server.py:_SITEMAP_ENTRIES` and `server.py:3514` instead. Tests validate dead code; production rules drift untested. The robots audit (`audit_robots_sitemap.md`) already flagged that production lacks `Disallow: /gate` — `seo.py:ROBOTS_TXT` likewise lacks it. The two sources of truth disagree on which paths are NOINDEX:
  - `seo.py:NOINDEX_PATHS` includes `/dashboards`, `/admin`, `/api`, `/login`, `/register`, `/token`, `/gate`, `/invite`, `/signup`, `/settings`, `/leaderboard`, `/embed`, `/billing`, `/profile`, `/onboarding`, `/account`, `/enquire`, `/support`, `/contact`, `/saved`, `/signal-search`, `/suspended`, `/subscribe`, `/forgot-password`, `/reset-password`, `/auth`.
  - `server.py` robots blocks `/admin/`, `/api/`, `/auth/`, `/dashboards`, `/dashboard/`, `/token`, `/login`, `/signup`, `/register`, `/settings/`, `/embed/`, `/invite/` — **missing**: `/gate`, `/enquire`, `/support`, `/contact`, `/saved`, `/signal-search`, `/suspended`, `/subscribe`, `/forgot-password`, `/reset-password`, `/leaderboard`, `/billing`, `/profile`, `/onboarding`, `/account`.
- **Action:** either (a) refactor `server.py` to call `seo.build_sitemap_xml()` + serve `seo.ROBOTS_TXT`, then update one canonical source; or (b) delete the dead exports + their tests and document `server.py` as the live source. Option (a) is the path the existing module docstring implies.

#### HIGH-4 — Six core public pages have no Open Graph image (or any OG meta tags at all)

- **Files (zero OG tags):**
  - `gateway/static/pricing.html`
  - `gateway/static/terms.html`
  - `gateway/static/privacy.html`
  - `gateway/static/dpa.html`
  - `gateway/static/enquire.html`
  - `gateway/static/status.html`
- **Files (3 OG tags, missing `og:image`):**
  - `gateway/static/prerelease.html` (the apex root — share previews on Twitter/Slack/Discord render a generic card or fall back to the apex domain) **(pre-release off-limits — flagged only)**
  - `gateway/static/landing.html`
  - `gateway/static/narve-brand.html`
- **Risk:** share-card previews collapse to the destination domain + first heading, with no image. The platform already has `/og/default` and per-page OG endpoints in `gateway/og_routes.py` — they're just not wired here.
- **Action:** add `<meta property="og:image" content="https://narve.ai/og/default">` plus `og:image:width/height=1200/630` to every page lacking it. For non-prerelease pages, also add the full OG quartet (`og:type`, `og:title`, `og:description`, `og:url`).

---

### MEDIUM

#### MEDIUM-1 — `_SITEMAP_ENTRIES` omits `/team`, `/press`, `/api/docs`, `/sources`, `/status` even though they're public + indexable

- **File:** `gateway/server.py:3556-3570`
- **Evidence:** `/team` and `/press` are registered via `seo_routes.PUBLIC_PATHS` and the `_PUBLIC_PATHS` set at server.py:1438; `/api/docs` is at L1440; `/sources` is reachable via `_PUBLIC_PREFIXES` `/sources/` at L1447; `/status` is at L1430. None appear in the sitemap.
- **Risk:** Google has to discover these through internal linking only; sitemap completeness suffers; team/press are exactly the pages that benefit most from being explicitly listed.
- **Action:** add entries for `/team`, `/press`, `/api/docs`, `/sources`, `/status` with appropriate `changefreq` + `priority`.

#### MEDIUM-2 — Sub-brand subdomain sitemap relies on `SUBPRODUCTS[sub].get("sitemap_pages", ())` but no SUBPRODUCT row defines `sitemap_pages`

- **Files:** `gateway/server.py:3605`; `gateway/subproduct.py:42-300+` (12 subproducts, zero have a `sitemap_pages` key).
- **Risk:** sub-brand sitemaps are always just `<base>/` — there is no surface for `/about`, `/pricing`, `/api/docs` etc. on subdomains even though they exist via `seo_routes.py` proxy. Each sub-brand is supposed to be its own Google property; with a one-URL sitemap, that property's crawl surface is one page.
- **Action:** either populate `sitemap_pages` per-subproduct (e.g. `[("/about", "monthly", "0.7"), ("/pricing", "monthly", "0.8")]`) or fall back to a shared default set for sub-brands.

#### MEDIUM-3 — `enquire.html` JSON-LD `name` and `<title>` disagree

- **File:** `gateway/static/enquire.html:6,21`
- **Evidence:** `<title>Request Access — narve.ai</title>` vs `"name":"Request invite — narve.ai"` in the schema.org ContactPage payload.
- **Risk:** schema.org name mismatches the page title — Google's rich-result heuristics expect them aligned; minor demotion risk and confusing search snippets.
- **Action:** align the strings.

#### MEDIUM-4 — `faq.html` JSON-LD references `/api/v1/docs` as canonical docs URL but the canonical is `/api/docs`

- **File:** `gateway/static/faq.html:30` ("Canonical docs are at /api/v1/docs.")
- **Evidence:** `_PUBLIC_PATHS` at `server.py:1440` lists `/api/docs`, and `gateway/static/api_docs.html:8` declares canonical `https://narve.ai/api/docs`. There is no `/api/v1/docs` route in `server.py` or `api_v1.py`.
- **Risk:** FAQ answer (which becomes a Google FAQ rich result) sends users / crawlers to a non-existent URL.
- **Action:** correct the FAQ answer to `/api/docs`.

#### MEDIUM-5 — `pricing.html` description claims "13 subproducts" but `subproduct.py` defines fewer

- **File:** `gateway/static/pricing.html:7`
- **Evidence:** the canonical count per `memory/narve_subproducts.md` is 12 subproducts. The pricing page declares 13.
- **Risk:** factual drift between marketing copy and product config — Google may quote the wrong count in snippets.
- **Action:** update the meta description (and the page body if it claims 13 too) to match `subproduct.py:SUBPRODUCTS`.

#### MEDIUM-6 — `prerelease.html` `SoftwareApplication` schema missing `applicationCategory`

- **File:** `gateway/static/prerelease.html:38-46`
- **Evidence:** Google's `SoftwareApplication` rich-result spec lists `applicationCategory` as a required field. The block declares `"@type":"SoftwareApplication","name":"narve.ai","operatingSystem":"Web"` but no `applicationCategory`. Compare to `changelog.html:34` which correctly sets `applicationCategory:"FinanceApplication"`.
- **Risk:** rich-result eligibility for app cards is lost; structured-data testing tool will surface a warning. **(Pre-release off-limits — flagged only.)**

---

### LOW

#### LOW-1 — `seo.py:_abs()` does not URL-escape paths; `build_sitemap_xml()` html-escapes but does not URL-encode source handles

- **File:** `gateway/seo.py:59-65, 186`
- **Evidence:** `html.escape(handle)` handles `<>&"'` but does not percent-encode characters like space, `?`, `#`, `/`, `%` that could appear in malformed handle data. Production `server.py:3654` has the same shape (`/sources/{handle}` with no encoding). Source handles are constrained to `[A-Za-z0-9_]` in practice but the safety net is weak; a bad row could emit an invalid `<loc>`.
- **Action:** wrap handle in `urllib.parse.quote(handle, safe="")` before interpolation.

#### LOW-2 — `seo.py` is hardcoded to `https://narve.ai` (`APEX`) while production sitemap uses request-aware `_request_apex`

- **File:** `gateway/seo.py:19`
- **Evidence:** the live `/sitemap.xml` at `server.py:3615` correctly derives `apex` from the request so an alternate domain (e.g. `habbig.com`) gets correct canonical URLs. `seo.py` always emits `https://narve.ai`. Currently invisible because production calls `server.py`, not `seo.build_sitemap_xml()`, but the divergence is a footgun if HIGH-3 is fixed by routing through the library.
- **Action:** if HIGH-3 is fixed via consolidation, pass `apex` into `build_sitemap_xml` and `build_seo_head` as a parameter.

#### LOW-3 — `seo.py:STATIC_SITEMAP` lists `/calendar` at priority 0.7 and `/dpa` at 0.3 — diverges from the live `_SITEMAP_ENTRIES`

- **File:** `gateway/seo.py:157-164`
- **Evidence:** `seo.py:STATIC_SITEMAP` omits `/landing`, `/about`, `/how-it-works`, `/methodology`, `/faq`, `/changelog`, `/narve` that production has at server.py:3556-3570.
- **Action:** part of HIGH-3 consolidation; remove `STATIC_SITEMAP` if route is via server.py.

#### LOW-4 — Both robots.txt variants miss several authed paths actually listed in `seo.NOINDEX_PATHS`

- **File:** `gateway/server.py:3514-3538` vs `gateway/seo.py:30-38`.
- **Evidence (paths in `seo.NOINDEX_PATHS` but NOT in production robots.txt):** `/gate`, `/enquire`, `/support`, `/contact`, `/saved`, `/signal-search`, `/suspended`, `/subscribe`, `/forgot-password`, `/reset-password`, `/leaderboard`, `/billing`, `/profile`, `/onboarding`, `/account`.
- **Risk:** crawlable auth/account/transaction surfaces; not a security issue (the pages are auth-gated server-side) but they may show up in Google as login forms or "Sign in" results — bad UX.
- **Action:** add `Disallow:` for each of the above. `/gate` is the same gap the previous audit's HIGH-1 already flagged. **(Pre-release off-limits for `/gate` — recommendation only.)**

#### LOW-5 — `subproduct_landing.html` canonical points at `https://{{ subproduct_slug }}.narve.ai/` regardless of whether the request came from apex or subdomain

- **File:** `gateway/static/subproduct_landing.html:8`
- **Evidence:** `<link rel="canonical" href="https://{{ subproduct_slug }}.narve.ai/">` — the canonical correctly points at the sub-brand's own subdomain. If a future change ever serves this template at `narve.ai/preview/<slug>` (a path that exists for marketing previews), the canonical would still point cross-domain. Today this is a foot-gun, not a live bug.
- **Action:** parameterize canonical via render context (`canonical_url`) rather than hardcoding.

---

## Coverage summary

| Surface | Covered by audit | Status |
|---|---|---|
| `gateway/seo.py` | yes | mostly dead code (HIGH-3) |
| `gateway/seo_routes.py` | yes | clean — thin proxy + register |
| `gateway/server.py` robots/sitemap | yes | rules drift vs library; missing entries |
| `gateway/server_features.py` shadow handlers | yes | CRITICAL — delete |
| Static page `<title>` / `<meta>` / canonical | yes | LOW-5 + MEDIUM-3/4/5 + HIGH-4 |
| JSON-LD validity | yes | HIGH-1, MEDIUM-6 |
| OG / Twitter card coverage | yes | HIGH-4 (6 pages with no OG) |
| Pre-release pages | observation only | flagged, no edits |
| Tests | yes | tests cover dead code (HIGH-3) |

---

## Notes on the previous robots/sitemap audit

`audits/audit_robots_sitemap.md` (2026-05-15) noted 3 issues. **All remain open:**
- HIGH there → covered here under LOW-4 (missing `/gate` Disallow). Pre-release off-limits.
- MEDIUM there → covered here under HIGH-2 (gate-redirected sitemap URLs). Pre-release off-limits.
- LOW there (trailing slash on `/dashboards`) → cosmetic; not re-flagged.

This audit additionally surfaces:
- CRITICAL-1 (duplicate route handlers)
- HIGH-1 (pricing InStock false signal)
- HIGH-3 (library/production divergence)
- HIGH-4 (OG image coverage)
- 6 mediums + 5 lows.
