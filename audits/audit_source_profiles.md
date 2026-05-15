# Adversarial audit — source profiles routes

- Date: 2026-05-15
- Auditor: automated adversarial review (no code changes)
- Scope: source-profile, source-credibility, source-follow, and shared-source HTTP surface

## 0. Important file-discrepancy note

The audit was requested against `gateway/source_profiles_routes.py`. **That file does not exist** in this repository (verified against working tree and full git history on every branch — no add/delete event has ever touched a `source_profiles_routes.py`). The only file with a closely matching name is the test file `gateway/tests/test_source_profiles.py`.

The actual source-profile, source-credibility, source-follow, and shared-source HTTP surface lives in:

- `/Users/shocakarel/Habbig/gateway/server_features.py` — `/sources/{handle}`, `/api/sources/{handle}/follow` (POST/DELETE/PATCH), `/api/sources/following`, `/api/search`, `/sitemap.xml`, `/robots.txt`
- `/Users/shocakarel/Habbig/gateway/og_routes.py` — `/og/source/{handle}` OG-image card
- `/Users/shocakarel/Habbig/gateway/api_v1.py` — `/v1/sources`, `/v1/sources/{handle}` (Bearer-key auth)
- `/Users/shocakarel/Habbig/gateway/api_public/routes.py` — `/api/public/v1/sources`, `/sources/{handle}`, `/sources/{handle}/predictions`, `/sources/{handle}/history`
- `/Users/shocakarel/Habbig/gateway/routes_sharing.py` — `/s/s/{token}`, `POST /api/share/source`
- `/Users/shocakarel/Habbig/gateway/embed_routes.py` — `source_credibility` embed widget
- `/Users/shocakarel/Habbig/gateway/queries/sources.py` — credibility / category / FTS5 helpers
- `/Users/shocakarel/Habbig/gateway/queries/watchlist.py` — follow_source / unfollow_source / is_following_source / list_followed_sources
- `/Users/shocakarel/Habbig/gateway/db.py` (schema) — `source_credibility`, `followed_sources` tables

The four threat-model headings the user asked for are addressed against this combined surface.

## 1. Severity counts

| Severity | Count |
|---|---|
| Critical | 0 |
| High     | 1 |
| Medium   | 4 |
| Low      | 4 |
| Info     | 2 |

(11 findings total; "Info" used for hardening/observation items that aren't directly exploitable.)

## 2. Top three findings

1. **H-1 — Sub-threshold source credibility leaks via `/og/source/{handle}`, `/api/public/v1/sources/{handle}`, `/api/search`, and the source-card share path.** The HTML profile at `/sources/{handle}` correctly 404s sources with `accuracy_unlocked=0`, but four parallel surfaces serve the score, decay-weighted accuracy, and prediction counts for those same sub-threshold rows. Effectively defeats the "10-prediction minimum before public exposure" rule.
2. **M-1 — Follow mutation endpoints rely on a soft-warn CSRF mode for PATCH/DELETE.** `DELETE /api/sources/{handle}/follow` and `PATCH /api/sources/{handle}/follow` are inspected by `CSRFMiddleware` but only enforced when `CSRF_PATCH_DELETE_ENFORCE` env flag is true (default false, per `gateway/server.py:1114`). In default config a forged same-site request can unfollow any source for the victim user or silently lower their notification credibility floor.
3. **M-2 — No bound on user-controlled `source_handle` length, character set, or existence in the follow API.** `POST /api/sources/{handle}/follow` accepts any handle that survives `.strip()` and is non-empty, with no max length, no charset check, and no existence check against `source_credibility`. An authenticated user can write arbitrary attacker-controlled strings into `followed_sources.source_handle` (rows index `idx_follow_handle`), pollute cache keys like `source_profile:{handle}`, and create dangling follows that the notification job will repeatedly scan.

## 3. Findings (detail)

### H-1 — Sub-threshold source-credibility leak (multi-surface)

**Severity:** High
**Threat tag:** credibility-score tampering via API / IDOR on private-source views
**Files:**
- `gateway/og_routes.py:75-115` (`/og/source/{handle}`)
- `gateway/api_public/routes.py:164-176` (`/api/public/v1/sources/{handle}` — Bearer auth, but any paid API key works)
- `gateway/server_features.py:976-985` (`/api/search` — authenticated user)
- `gateway/embed_routes.py:305-332` (`source_credibility` widget)
- `gateway/routes_sharing.py:160-200` (`/s/s/{token}` shared card)

**Issue:** The public HTML profile (`gateway/server_features.py:608-655`) deliberately 404s on `not _cred or not _cred["accuracy_unlocked"]`, with the test suite (`gateway/tests/test_source_profiles.py:28-38`) explicitly asserting that contract. But every other surface listed above performs only `if not row:` / `if cred:` and serves the score regardless of `accuracy_unlocked`. `/og/source/{handle}` in particular returns a PNG card with `global_credibility` baked in for any source in the table — no auth required, no rate limit.

**Why it matters:** The `accuracy_unlocked` flag exists to keep sources with fewer than 10 resolved predictions out of public view (regressions to the 0.5 prior dominate small samples, so the score is meaningless and rankable noise). Attackers can:
- Enumerate every tracked handle via `GET /og/source/<guess>.png` and dump partial scores (no 404 vs 200 ambiguity here — the cards are cached so probe latency stays flat).
- Use `/api/search` from any authenticated session to fetch sub-threshold sources by FTS match.
- Use `/api/public/v1/sources/{handle}` with a self-issued API key to dump scores in bulk.
- Mint a share-card via `POST /api/share/source` (paid required, rate-limited per `_mint_rate_limited`) — `create_shared_source` in `db_sharing.py:124` writes any handle the caller supplies, and `/s/s/{token}` then renders the score from `get_source_credibility` with no unlock check.

**Reproduction:**
- `curl https://narve.ai/og/source/<sub-threshold-handle>` returns 200 with the score.
- `curl -H "Authorization: Bearer <any-key>" https://narve.ai/api/public/v1/sources/<sub-threshold-handle>` returns the full row dict.

**Recommendation:** Either gate each surface on `accuracy_unlocked`, OR drop the unlock check on the HTML page (decide once whether sub-threshold scores are public). Treat the four surfaces as the source of truth and align them.

### M-1 — CSRF soft-warn on follow mutations

**Severity:** Medium
**Threat tag:** follow-list visibility (write side)
**Files:**
- `gateway/server.py:1098-1117` (rollout flag definition)
- `gateway/server.py:1282-1352` (middleware enforcement)
- `gateway/server_features.py:1189-1216` (`DELETE` / `PATCH /api/sources/{handle}/follow`)

**Issue:** `CSRF_PATCH_DELETE_ENFORCE` is read from env at module load and defaults to `false`. With the flag off (production default per the comment block at `server.py:1098`), `_validate_csrf` failure on a PATCH/DELETE only emits `log.warning("CSRF soft-warn: …")` and lets the request through. POST is fully enforced.

**Why it matters for source profiles specifically:** The two non-POST follow verbs are mutating but skip enforcement. The Origin/Referer check (`server.py:1325-1338`) only fires when `IS_PRODUCTION` is true *and* the Origin header is present; missing-Origin requests (any non-fetch issuer — `<form>`, `<iframe>`, native clients) slip past. A same-site XSS or a malicious browser extension can unfollow every source the victim follows, or silently rewrite `notify_min_credibility=1.0` so the user stops receiving notifications.

**Recommendation:** Flip `CSRF_PATCH_DELETE_ENFORCE=true` in production env. Comment in the code already signals this is Phase 1 of a planned rollout — the source-profile surface is one of the things waiting on that flip.

### M-2 — No validation of user-supplied `source_handle` in follow API

**Severity:** Medium
**Threat tag:** source-handle uniqueness / data integrity
**Files:**
- `gateway/server_features.py:1158-1187` (`POST /api/sources/{handle}/follow`)
- `gateway/queries/watchlist.py:153-177` (`follow_source`)
- `gateway/db.py:158-169` (`source_credibility` schema, `UNIQUE(source_handle)`)
- `gateway/db.py:460-473` (`followed_sources` schema, `UNIQUE(user_id, source_handle)`)

**Issue:** The follow API performs no validation on the path param beyond `.strip()` inside `follow_source`. The handle is:
- Not length-capped (the table column is plain `TEXT`).
- Not charset-restricted (any Unicode, including zero-width / RTL override / NULs).
- Not checked for existence in `source_credibility`.
- Not normalised for case (the DB column uses default BINARY collation — `Alice` and `alice` are two distinct rows everywhere in the surface).

**Why it matters:**
- An authenticated attacker can `POST /api/sources/<2-MiB-string>/follow` repeatedly to bloat the `followed_sources` table and the per-handle cache (`source_profile:{handle}`, `og:source:{handle}`).
- Dangling follows pollute the `send_market_resolution_notifications` / followed-source notification jobs (`gateway/jobs/email_jobs.py:223`), forcing every job tick to repeatedly process handles that will never resolve.
- Case-sensitivity means a recompute that produces `FedWatcher` does not match a follow row for `fedwatcher`, silently dropping notifications.
- Zero-width / RTL chars in a handle allow visual squatting (the profile page escapes `handle` via the auto-escape path in `render_page`, so XSS is bounded — but the *displayed* handle string still confuses humans).

**Recommendation:** Reject handles >64 chars; restrict to `[A-Za-z0-9_.-]`; reject handles with no matching `source_credibility` row (return 404). Add `COLLATE NOCASE` to both tables for `source_handle`, or lowercase on write and on lookup. (Code change — flagged, not applied per audit constraints.)

### M-3 — `accuracy_unlocked` "not_found" cache poisoning by enumeration

**Severity:** Medium
**Threat tag:** credibility tampering (cache layer) / IDOR on private-source views
**Files:** `gateway/server_features.py:617-647`

**Issue:** The public profile route caches its result, including the `{"not_found": True}` sentinel, under `f"source_profile:{handle}"` for 300 s, before any check on what `handle` actually is. Path-param `handle` is user-controlled (anonymous, no auth). An attacker that probes `/sources/<random>` 10 000 times in 5 minutes spins up 10 000 cache entries that each survive for the TTL, with no eviction back-pressure visible at this call site.

**Why it matters:** Anonymous DoS on the cache backend — the cache layer is shared across the entire app (see `gateway/cache/service.py`). Combined with no rate limit on the route itself, an attacker can starve every other cached resource. The cache key namespace is also not prefixed with a request-scoped salt, so cross-handle key collisions are possible if a handle happens to contain a literal `":"` (FastAPI passes through Unicode-decoded path segments — colons survive).

**Recommendation:** Bound the cache key to a sanitised handle (regex-restricted before caching), and either (a) skip caching `not_found` sentinels for handles not present in `source_credibility`, or (b) add a global cap on entries per prefix.

### M-4 — `POST /api/share/source` mints a public card for any caller-supplied handle

**Severity:** Medium
**Threat tag:** IDOR on private-source views (via share-mint side channel)
**Files:**
- `gateway/routes_sharing.py:464-491`
- `gateway/db_sharing.py:124-157`

**Issue:** A paid user can supply any `source_handle` in the JSON body and receive a public share token. `create_shared_source` writes the row with no check that the handle exists in `source_credibility`, no check on `accuracy_unlocked`, and no check that the caller has interacted with that source. The resulting `/s/s/{token}` URL is public, indexable, and renders the score (see H-1 — same code path).

**Why it matters:** Even if H-1's surfaces are locked down later, the share-mint endpoint provides a paid-tier-only bypass to publish any source's score to the open web, including handles the system has marked sub-threshold.

**Recommendation:** Reject the mint if `get_source_credibility(handle) is None` or `accuracy_unlocked != 1`. Rate-limit per source_handle in addition to per-user (the existing limiter is per-user-id only).

### L-1 — Public profile and OG card endpoints have no anonymous rate limit

**Severity:** Low
**Threat tag:** enumeration / scraping
**Files:**
- `gateway/server_features.py:608` (`/sources/{handle}`)
- `gateway/og_routes.py:75` (`/og/source/{handle}`)

**Issue:** Neither endpoint calls `server._is_rate_limited`. The full set of rated sources is published in `/sitemap.xml` (intentional for SEO), but combined with M-3 the lack of an anonymous limit makes opportunistic scraping cheap. The 300 s cache absorbs repeat hits per handle, but unique handles get a fresh DB query each time.

**Recommendation:** Add `_is_rate_limited(f"src:{ip}", limit=120, window=60)` to both. (No-op for legitimate viewers; bounds enumerators.)

### L-2 — `accuracy_unlocked` not enforced in the source-credibility embed widget

**Severity:** Low
**Threat tag:** IDOR on private-source views
**Files:** `gateway/embed_routes.py:305-332`

**Issue:** Owner of a paid account can configure a `source_credibility` embed widget targeting any handle, including sub-threshold ones. The embed page returns the score with `accuracy_unlocked: false` exposed to the caller, but still returns the numeric `credibility`.

**Why it matters:** Same shape as H-1, narrower surface (widget owner only). Folded here because the widget owner is by definition paid and the data is already exposed via M-4 / H-1.

**Recommendation:** Apply the same unlock gate as the HTML page.

### L-3 — `/api/sources/following` page param accepts up to 500 with no per-user history limit

**Severity:** Low
**Threat tag:** follow-list visibility (self-DoS / response-size)
**Files:** `gateway/server_features.py:1219-1264`

**Issue:** `list_followed_sources` runs an unbounded LEFT JOIN with no SQL-level LIMIT, then slices in Python. The endpoint pages with `per_page` clamped to 500, but the underlying query still loads the full set every time. A user who has followed 50 000 handles (no cap on follow count anywhere) hits a 50 000-row LEFT JOIN per page request.

**Why it matters:** Per-user self-DoS / cost amplification, not a privilege escalation. Audit-relevant because the comment at line 1226-1231 already flags this as a known limit.

**Recommendation:** Push pagination into SQL (`LIMIT ? OFFSET ?` in `list_followed_sources`). Cap follow count per user (e.g. 5 000).

### L-4 — `_forensic_sign` response wrapping is silently dropped on error

**Severity:** Low
**Threat tag:** follow-list visibility / audit trail
**Files:** `gateway/server.py:2238-2258`, applied at `gateway/server_features.py:1264`

**Issue:** `/api/sources/following` is the only follow endpoint that wraps its payload with a forensic signature. The other follow verbs (POST / PATCH / DELETE) and the `is_following_source` lookup don't sign their responses. If watermark / signed-response is part of the leak-tracking story for follow data, the surface is inconsistent.

**Recommendation:** Either sign all four endpoints or document explicitly that only GET is signed (informational only).

### I-1 — No `accuracy_unlocked` filter on the sitemap fallback path

**Severity:** Info
**Files:** `gateway/server_features.py:777-786`

**Issue:** The disk-cached `sitemap.xml` produced by `generate_sitemap` filters out `accuracy_unlocked=0` (test `test_sitemap_excludes_unrated_sources` asserts this). The live-generated fallback at lines 777-786 also filters correctly. **No defect — recording for completeness.**

### I-2 — Credibility-tampering API surface

**Severity:** Info
**Files:** `gateway/queries/sources.py:56-89` (`upsert_source_credibility`)

**Issue:** `upsert_source_credibility` is **not exposed via any HTTP route**. The only writers are the `recompute_all_credibilities` job and the test suite. There is no `POST /api/sources/{handle}/credibility` or admin override that lets an external caller pump scores. **No defect — the "credibility-score tampering via API" threat is N/A in the current surface, modulo M-3 (cache poisoning of the read path).**

## 4. Threats explicitly checked and ruled clean

- **Source-handle uniqueness in the DB:** `source_credibility(source_handle)` and `followed_sources(user_id, source_handle)` both have `UNIQUE` constraints. The follow insert is wrapped in `try / IntegrityError → SELECT id` (`watchlist.py:163-177`), so concurrent inserts can't duplicate. *Schema-level uniqueness is sound; case-collation flaw is M-2.*
- **Direct write to `source_credibility` via API:** Not reachable — see I-2.
- **Cross-user follow list access:** Every read-side helper takes `user_id` from the validated session (`_require_auth(request)["user_id"]`), no path param. *No IDOR on the read.*
- **FTS5 injection via handle:** `_fts_sanitize_query` (`db.py:603-622`) double-quotes terms and escapes embedded `"`. *No SQLi or FTS injection.*
- **Shared-source token IDOR:** Tokens are HMAC-signed (`share_tokens.encode`, 16-byte URL-safe random + signature). Brute-force not feasible. *Token IDOR not viable; concern is mint-side, M-4.*
- **XSS in profile page via handle:** `render_page` HTML-escapes all non-`raw_` keys (`server.py:2564-2570`); handle is passed as a plain key. *No HTML/JS injection on the public profile.*

## 5. Recommendations summary

1. Decide once whether sub-threshold sources are public; align all six surfaces. (H-1)
2. Flip `CSRF_PATCH_DELETE_ENFORCE=true` in production env. (M-1)
3. Validate `source_handle` (length, charset, existence, COLLATE NOCASE) at the API boundary. (M-2)
4. Sanitise + cap cache-key entries on `/sources/{handle}`; skip `not_found` cache for non-existent handles. (M-3)
5. Gate `POST /api/share/source` on `accuracy_unlocked = 1`. (M-4)
6. Anonymous rate-limit `/sources/{handle}` and `/og/source/{handle}`. (L-1)
7. Push `list_followed_sources` pagination into SQL; cap follow count per user. (L-3)

— End of audit —
