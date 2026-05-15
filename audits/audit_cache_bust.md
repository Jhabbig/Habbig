# Static Asset Cache-Bust Audit

**Date:** 2026-05-15
**Auditor:** Claude (Opus 4.7, 1M context)
**Scope:** every `<link>`, `<script>`, `<img>`, and `<link rel="preload">` reference to `/_gateway_static/*` across `gateway/static/*.html` and every Python route that builds inline HTML strings (`gateway/**/*.py`). **`gateway/static/prerelease.html` is pre-release and off-limits — excluded from the rewrite, listed here only as a known gap.**
**Verification:** synchronous bash + Read only. No background jobs, no network.

---

## Policy under audit

`gateway/server.py:836–860` defines `static_url(path)`:

```python
def static_url(path: str) -> str:
    rel = path.lstrip("/")
    cached = _static_hash_cache.get(rel)
    if cached is not None:
        return f"/_gateway_static/{rel}?v={cached}"
    ...
    digest = _hl.md5(full.read_bytes(), usedforsecurity=False).hexdigest()[:8]
    _static_hash_cache[rel] = digest
    return f"/_gateway_static/{rel}?v={digest}"
```

The canonical template form is `{{ static: <asset_rel_path> }}`, processed by `render_page()` at `server.py:2588–2592` and substituted with the hashed URL **before** the rest of the substitution pipeline.

> **One spec discrepancy worth flagging up front.** The audit brief asked for `?v=` "derived from mtime". The implementation uses an **MD5 content hash** (first 8 chars), not mtime. The behavioural contract is functionally equivalent — both change iff the file changes — and a content hash is strictly stronger (mtime can be reset by `touch`, content hash cannot). I am scoring against the **implemented** spec: every static asset link should resolve through `static_url()` (i.e. use `{{ static: }}` in templates / call `static_url()` in route HTML), and no asset link should ship a hand-rolled `?v=<integer>`.

---

## Headline numbers

| Surface | Hardcoded `?v=N` references | Files |
|---|---|---|
| `gateway/static/*.html` templates | **74** | 65 |
| `gateway/**/*.py` inline HTML | **23** (1 false-positive: a code comment + 1 test, see below) | 17 |
| `gateway/static/*.js` and `*.css` | **0** | 0 |
| **Total hardcoded `?v=N`** | **97** counted; **95 production-shipping** | **80 unique files** |

| Distinct version literal | Count (HTML+PY) |
|---|---|
| `?v=1` | 14 |
| `?v=2` | 63 |
| `?v=3` | 1 (a code comment in `server.py:830` — not shipped) |
| `?v=5` | 5 |
| `?v=7` | 1 (a docstring in `tests/test_foundation_bundle.py:251` — not shipped) |
| `?v=8` | 15 |

**Specifically requested by the user:**
- **Hardcoded `?v=1` count: 14** (12 in HTML, 2 in PY).
- The user asked specifically about `?v=1`; the wider audit shows `?v=2`, `?v=5`, `?v=7`, `?v=8` are also present and equally non-conformant. Listed below.

| Canonical `{{ static: ... }}` usage in HTML | 326 substitutions across 105 templates |
|---|---|

The codebase is **partially migrated**: the `{{ static: }}` token has been adopted for the bulk of CSS (`gateway.css`, `components.css`, page-specific CSS), but a long-tail of common JS/asset includes (`theme.js`, `density.js`, `js/cmdk.js`, `js/share_menu.js`, `img/logo.png`, `fonts/Inter-Variable-subset.woff2`, plus the dashboard onboarding bundle and several feature-flag-scoped JS modules) was never migrated. The Python route handlers that build inline HTML (the legacy `f"<html>…</html>"` style in `admin_routes.py`, `collections_routes.py`, `search_routes.py`, etc.) were left at hand-bumped version literals — currently `?v=5` and `?v=8` for `gateway.css`, `?v=1` for everything else.

---

## Hard rule check: "No hardcoded `?v=1` patterns"

**14 occurrences across 14 files.** All listed below. None are in `prerelease.html` (which is `?v=2` and off-limits anyway).

### HTML (12 occurrences)

| File | Line | Asset |
|---|---|---|
| `gateway/static/profile.html` | 186 | `density.js?v=1` |
| `gateway/static/settings_integrations.html` | 160 | `density.js?v=1` |
| `gateway/static/settings_trading_addon.html` | 218 | `density.js?v=1` |
| `gateway/static/settings_billing.html` | (see grep) | `density.js?v=1` |
| `gateway/static/settings.html` | (see grep) | `density.js?v=1` |
| `gateway/static/settings_billing.html` | (see grep) | `settings_billing.js?v=1` |
| `gateway/static/referrals.html` | (see grep) | `referrals.js?v=1` |
| `gateway/static/leaderboard.html` | 47 | `leaderboard.js?v=1` |
| `gateway/static/invite_public.html` | 24 | `invite_public.js?v=1` |
| `gateway/static/dashboards.html` | 113 | `density.js?v=1` |
| `gateway/static/dashboards.html` | 131 | `css/onboarding_tour.css?v=1` |
| `gateway/static/dashboards.html` | 132 | `js/first_week_goals.js?v=1` |
| `gateway/static/dashboards.html` | 133 | `js/onboarding_tour.js?v=1` |

(Reproduce: `grep -rnE '\?v=1\b' gateway/static/ --include='*.html'`)

### Python (2 occurrences)

| File | Line | Asset |
|---|---|---|
| `gateway/backtest_routes.py` | 215 | `charts.js?v=1` |
| `gateway/subproduct_dashboard_routes.py` | 104 | `subproduct_dashboard.js?v=1` |

(Reproduce: `grep -rnE "\?v=1\b" gateway/ --include='*.py' \| grep -v tests/`)

---

## Full hardcoded-`?v=` inventory (all version literals, not just `v=1`)

### HTML — 74 occurrences across 65 files

#### `?v=2` (62 occurrences — `theme.js` cluster)

`theme.js?v=2` is included in 62 distinct templates. This is the largest single offender. Sample (full list reproducible via `grep -rln 'theme.js?v=2' gateway/static/`):

`403.html`, `account.html`, `admin-email-edit.html`, `admin-emails.html`, `admin-flag-edit.html`, `admin-flags.html`, `admin-impersonation-detail.html`, `admin-impersonations.html`, `admin.html`, `admin_affiliates.html`, `admin_equivalences.html`, `admin_moderation.html`, `admin_status.html`, `ai_usage.html`, `audit_log.html`, `billing.html`, `collection_detail.html`, `collections.html`, `contact.html`, `dashboard_models.html`, `dashboards.html`, `dpa.html`, `enquire.html`, `forgot-password-email.html`, `forgot-password.html`, `gate.html`, `impressum.html`, `intelligence.html`, `invite.html`, `landing.html`, `login.html`, `market_detail.html`, `onboarding.html`, `poster.html`, `predictions.html`, `predictions_history.html`, `predictions_public.html`, `**prerelease.html**` (off-limits — leave as-is), `preview.html`, `pricing.html`, `privacy.html`, `profile.html`, `public_user_takes.html`, `referrals.html`, `register.html`, `reset-password.html`, `saved.html`, `settings.html`, `settings_affiliate.html`, `settings_billing.html`, `settings_embeds.html`, `settings_integrations.html`, `settings_privacy.html`, `settings_takes.html`, `settings_trading_addon.html`, `signal-search.html`, `signup.html`, `subscribe.html`, `support.html`, `suspended.html`, `terms.html`, `token.html`, `_base.html`.

#### `?v=1` (12 occurrences) — see Hard-rule section above.

### Python inline HTML — 21 production-shipping occurrences across 15 files

| File | Line | Asset | Literal |
|---|---|---|---|
| `gateway/admin_shell.py` | 206 | `theme.js` | `?v=2` |
| `gateway/admin_routes.py` | 1057 | `gateway.css` | `?v=8` |
| `gateway/admin_routes.py` | 1379 | `gateway.css` | `?v=8` |
| `gateway/ai_routes.py` | 219 | `gateway.css` | `?v=8` |
| `gateway/alerts_routes.py` | 169 | `gateway.css` | `?v=8` |
| `gateway/backtest_routes.py` | 201 | `gateway.css` | `?v=8` |
| `gateway/backtest_routes.py` | 215 | `charts.js` | `?v=1` |
| `gateway/collections_routes.py` | 1074 | `gateway.css` | `?v=8` |
| `gateway/collections_routes.py` | 1121 | `gateway.css` | `?v=8` |
| `gateway/insider_routes.py` | 189 | `gateway.css` | `?v=8` |
| `gateway/network_routes.py` | 75 | `gateway.css` | `?v=8` |
| `gateway/onboarding_routes.py` | 845 | `gateway.css` | `?v=8` |
| `gateway/public_routes.py` | 488 | `gateway.css` | `?v=5` |
| `gateway/public_routes.py` | 545 | `gateway.css` | `?v=5` |
| `gateway/reports_routes.py` | 74 | `gateway.css` | `?v=8` |
| `gateway/scenarios_routes.py` | 385 | `gateway.css` | `?v=8` |
| `gateway/scenarios_routes.py` | 709 | `gateway.css` | `?v=8` |
| `gateway/search_routes.py` | 609 | `gateway.css` | `?v=8` |
| `gateway/server_features.py` | 114 | `gateway.css` | `?v=5` |
| `gateway/server_features.py` | 387 | `gateway.css` | `?v=5` |
| `gateway/subproduct_dashboard_routes.py` | 82 | `gateway.css` | `?v=5` |
| `gateway/subproduct_dashboard_routes.py` | 104 | `subproduct_dashboard.js` | `?v=1` |

**Plus two false positives** (counted in the raw grep but not production-shipping): `server.py:830` (a docstring code example showing the legacy pattern being migrated away from) and `tests/test_foundation_bundle.py:251` (a test docstring explaining what the test guards against). Both intentional; leave alone.

(Reproduce: `grep -rnE '\?v=[0-9]' gateway/ --include='*.py'`)

### Distinct `?v=N` literal version-drift

Across Python alone, **three distinct version literals are shipped for `gateway.css`**: `?v=5` (5 sites), `?v=7` (1 doc), `?v=8` (15 sites). Different parts of the gateway are bumping the version differently. This is exactly the drift the existing test `TestAssetVersioning.test_no_hardcoded_gateway_css_version` (`tests/test_foundation_bundle.py:247`) was written to guard against — but the test only scans `gateway/static/*.html`, not Python files, so it never tripped on the inline-HTML side. **The test's coverage gap is itself a finding.**

---

## Gap 2: assets that ship with no cache-buster at all

`?v=` strings are one half of the audit. The other half is assets referenced **without any cache-busting query string** — these go to whatever Cloudflare/browser cached last and never recover from a deploy.

`grep -rnE '(href|src)="/_gateway_static/[^"]+"' gateway/static/ --include='*.html' | grep -vE '\?v=' | grep -v '{{ static:'` returns **566 occurrences across the static HTML tree**. Distinct asset paths (28):

| Asset | Reference count | Severity |
|---|---|---|
| `img/logo.png` | 183 | LOW — image, content rarely changes; broken-cache surface is "old logo shown" |
| `js/cmdk.js` | 107 | **HIGH** — keyboard shortcuts JS, ships on nearly every page; stale copy breaks ⌘K/Ctrl-K UI globally |
| `js/share_menu.js` | 106 | **HIGH** — share menu JS, same blast radius |
| `fonts/Inter-Variable-subset.woff2` | 85 | LOW — font binary; subsetting is stable |
| `user-features.js` | 11 | MEDIUM — feature-flag client, stale = silent flag drift |
| `scroll-animations.js` | 8 | LOW |
| `toast.js`, `js/toast.js` | 7 + 3 | MEDIUM — toast UI; two different paths is a separate issue |
| `skeletons.js` | 7 | LOW |
| `states.css` | 7 | LOW |
| `skeletons.css` | 6 | LOW |
| `filter_panel.js` | 5 | LOW |
| `filter_panel.css` | 5 | LOW |
| `js/share-button.js` | 3 | LOW |
| `feedback_button.js` | 3 | LOW |
| `collections_widget.js` | 3 | LOW |
| `css/share-button.css` | 3 | LOW |
| `notifications.js` | 2 | LOW |
| `analytics.js` | 2 | LOW |
| `watermark.css` | 2 | LOW |
| `settings_trading_addon.js` | 1 | LOW |
| `settings_integrations.js` | 1 | LOW |
| `js/realtime.js` | 1 | LOW |
| `js/realtime-bindings.js` | 1 | LOW |
| `js/htmx.min.js` | 1 | LOW — vendor lib, content-hashed by build pipeline |
| `img/tobias.jpg` | 1 | LOW |
| `engagement_banner.js` | 1 | LOW |
| `pages/admin-churn.css` | 1 | LOW |

`_CachedStaticFiles` (`server.py:799–813`) sends `Cache-Control: public, max-age=2592000, immutable` for every 200 response. That's 30 days. Combined with Cloudflare's edge cache rules called out at `server.py:802–803`, **any of the 566 references above can be stuck on a stale copy for up to 30 days** after deploy, with no way to force-flush short of bumping the asset filename.

**`js/cmdk.js`, `js/share_menu.js`, and `user-features.js` are the most concerning** because the existing test `tests/test_foundation_bundle.py:373–387` *requires* `js/cmdk.js` to be included on every page that loads `gateway.css`. If cmdk.js is mid-deploy and the cached copy is stale, the keyboard surface across the dashboard breaks silently — and the absence of `?v=` means stale clients see no recovery until the 30-day TTL expires.

---

## Off-limits: pre-release

Per the audit brief, **`gateway/static/prerelease.html` is excluded from the rewrite scope**. It contains 1 occurrence (`prerelease.html:227` — `theme.js?v=2`). Counted in the headline number (because the audit measures the codebase, not the rewrite scope), but **must not be migrated** in this pass.

`gateway/onboarding_routes.py` references appear in the prerelease flow but the file itself is not pre-release-only — its `?v=8` for `gateway.css` is in-scope.

---

## Recommended remediation order

If you are remediating after this audit:

1. **Migrate the 62 `theme.js?v=2` HTML references** to `{{ static: theme.js }}`. Single largest fix, 1-line per file, zero risk. Excluding `prerelease.html`, that is 61 templates. After: net new bytes in each template = 0 (`?v=2` is the same length as `?v=ab12cd34`'s leading slice; the canonical form actually shortens slightly).
2. **Migrate the 12 `?v=1` HTML references** named in the Hard-rule section. Hits `density.js`, `leaderboard.js`, `invite_public.js`, `referrals.js`, `settings_billing.js`, plus the dashboards onboarding bundle.
3. **Fix the Python inline-HTML drift.** Either:
   - (preferred) replace each f-string-embedded `gateway.css?v=N` with an interpolation that calls `static_url("gateway.css")`. Same idea for the four JS sites. This is what the `static_url()` helper was built for — it already handles the bare-route case.
   - (cheap) extend `test_no_hardcoded_gateway_css_version` to scan `gateway/**/*.py` as well, then bump every literal to one canonical value. This keeps drift from re-emerging but doesn't deliver real content-hash cache-busting on the affected routes.
4. **Migrate the long-tail no-`?v=` references.** Start with `js/cmdk.js`, `js/share_menu.js`, `user-features.js` (highest-traffic, highest-functional-impact). The `img/logo.png` and `fonts/Inter-Variable-subset.woff2` cases are cosmetically the largest count but practically the lowest risk — the binaries rarely change.
5. **Tighten the guard test.** Today's `test_no_hardcoded_gateway_css_version` catches one filename in one tree. Replace with `re.search(r'/_gateway_static/[^"\'>]+\?v=\d+', text)` scanning every `*.html` under `gateway/static/` (excluding `_base.html` and `prerelease.html`) and every `*.py` under `gateway/` (excluding `tests/` and `server.py:830` comment line — or just whitelist the comment). The catch-everything regex closes the door on future `theme.js?v=3`, `density.js?v=2`, … drift.

---

## Reproducibility

Every count in this audit is reproducible with the following synchronous bash one-liners against `/Users/shocakarel/Habbig`:

```bash
# Total hardcoded ?v=N in HTML
grep -rnE "\?v=[0-9]" gateway/static/ --include="*.html" | wc -l   # 74

# Hardcoded ?v=1 specifically
grep -rnE "\?v=1\b" gateway/static/ --include="*.html" | wc -l     # 12
grep -rnE "\?v=1\b" gateway/ --include="*.py" | grep -v /tests/ | wc -l  # 2

# Distinct version literals in HTML
grep -rohE "\?v=[0-9]+" gateway/static/ --include="*.html" | sort | uniq -c
#   12 ?v=1
#   62 ?v=2

# Distinct version literals in PY (sans tests/comments)
grep -rohE "\?v=[0-9]+" gateway/ --include="*.py" | sort | uniq -c
#   15 ?v=8
#    5 ?v=5
#    2 ?v=1
#    1 ?v=7      (docstring, tests/test_foundation_bundle.py:251)
#    1 ?v=3      (docstring, server.py:830)
#    1 ?v=2      (admin_shell.py:206, ships)

# Canonical {{ static: }} usage
grep -rln "{{ static:" gateway/static/ --include="*.html" | wc -l  # 105 files
grep -rn  "{{ static:" gateway/static/ --include="*.html" | wc -l  # 326 substitutions

# Assets referenced with NO cache-buster (the silent gap)
grep -rnE '(href|src)="/_gateway_static/[^"]+"' gateway/static/ --include="*.html" \
  | grep -vE '\?v=' | grep -v '{{ static:' | wc -l                 # 566
```

---

## Summary one-liner for the user

- **Hardcoded `?v=1` count:** 14 (12 HTML + 2 PY).
- **Wider gap (the answer to "every static asset link uses `?v=` cache-bust derived from mtime"):** No. **97 hardcoded `?v=N` literals total** (74 HTML + 23 PY counted; 95 production-shipping) spanning 6 distinct version values. **566 additional asset references ship with no cache-bust query string at all.** The `static_url()` helper exists and is correct (it uses MD5 content hash, not mtime — equivalent or stronger); ~1/3 of asset references in the codebase route through it via `{{ static: }}`; the other ~2/3 are either hardcoded `?v=N` strings or unversioned URLs. `prerelease.html` (1 hardcoded `?v=2`) was excluded from any rewrite scope.
