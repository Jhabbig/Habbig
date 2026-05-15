# Adversarial Audit — Cookie scope

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Target: every `response.set_cookie(...)` call across the `gateway/` Python
tree.

## Scope

Audit brief: verify `domain=.narve.ai` is only set on cookies that **need**
cross-subdomain access (SSO, theme); auth-bearing cookies should be scoped
per host (no `Domain` attribute, so the browser stores under the exact
hostname only).

Hard rules:
- synchronous bash only (no `Bash run_in_background`, no async monitors)
- pre-release surface (`/`, `prerelease_page`, `/api/newsletter`) is
  **off-limits** — no edits or recommended edits there

This is a read-only audit. No code is changed; recommendations are recorded
below for the next maintenance pass.

## Inventory

Every cookie set or deleted from a non-test path under `gateway/`:

| # | Cookie name              | Defined in                                                    | Helper / call site                                  | Domain attribute (prod)             |
|---|--------------------------|---------------------------------------------------------------|-----------------------------------------------------|--------------------------------------|
| 1 | `pending_token`          | `gateway/auth/cookies.py:30`                                  | `set_pending_token_cookie` (`:92`)                  | `.narve.ai` via `_cookie_domain_for` |
| 2 | `narve_session`          | `gateway/auth/cookies.py:31`                                  | `set_session_cookie_hardened` (`:122`)              | `.narve.ai` via `_cookie_domain_for` |
| 3 | `pm_gateway_session`     | `gateway/server.py:236` (`COOKIE_NAME`)                       | `set_session_cookie` (`:2176`)                      | `.narve.ai` via `cookie_domain_for`  |
| 4 | `narve_gate_access`      | `gateway/server.py:237` (`GATE_COOKIE_NAME`)                  | `set_gate_cookie` (`:2239`)                         | `.narve.ai` via `cookie_domain_for`  |
| 5 | `narve_impersonation`    | `gateway/server.py:254` (`IMPERSONATION_COOKIE_NAME`)         | `_set_impersonation_cookie` (`:1609`)               | `.narve.ai` via `cookie_domain_for`  |
| 6 | `_csrf`                  | `gateway/security/csrf.py:34` / `gateway/server.py:1117`     | `set_csrf_cookie` (`csrf.py:88`) and `_set_csrf_cookie` (`server.py:1260`) | `.narve.ai` via `cookie_domain_for` |
| 7 | `affiliate_code`         | `gateway/data_affiliate.py` (`AFFILIATE_COOKIE_NAME`)         | `_set_affiliate_cookie` (`gateway/affiliate_routes.py:65`) | **no Domain — exact host**           |
| 8 | `narve_share_attribution`| literal in `gateway/routes_sharing.py:154,197,237`            | three handlers `public_shared_*`                    | **no Domain — exact host**           |
| 9 | `narve_shared_view`      | literal in `gateway/saved_views_routes.py:346`                | shared-view redirect (`:345`)                       | **no Domain — exact host**           |
|10 | `narve_lang`             | `_LANG_COOKIE` in `gateway/server_features.py`                | `/set-language` handler (`:214`)                    | **no Domain — exact host**           |
|11 | `narve_tz`               | `gateway/security/timezones.py:38`                            | `set_cookie` (`:138`)                               | **no Domain — exact host**           |
|12 | `narve-theme` / `betyc-theme` | not set server-side                                       | written by browser JS (`gateway/admin_shell.py:200`, `gateway/server.py:2789`) | n/a (client `document.cookie`)       |

**Total cookies set by the gateway: 11 (1–11 above).** Twelfth row included
for completeness — the theme cookie is JavaScript-only and has no
server-side `Set-Cookie` to audit.

For reference, `_cookie_domain_for` (`auth/cookies.py:42`) and
`cookie_domain_for` (`server.py:263`) both return `.<apex>` when the
request host matches an entry in `ALLOWED_DOMAINS`, and `None` otherwise.
Both helpers gate on production: dev/localhost responses never carry a
Domain attribute, so all of the below applies to the production
deployment only.

## Classification

Per the audit brief, cookies fall into two buckets:

**Need cross-subdomain access (Domain attribute justified):**
- SSO / single-login carriers — a logged-in user on `narve.ai` must be
  recognised on `crypto.narve.ai`, etc.
- Theme / locale / timezone — pure UI prefs that benefit from being
  picked up by subdomain dashboards on first paint.

**Per-host only (no Domain attribute):**
- Anything tied to short-lived state on a specific surface (attribution
  metric, flash banners).
- Anything whose value would let an attacker on a subdomain elevate
  privileges if exfiltrated.

## Findings

### F1. Session cookies are apex-scoped — by design and required
Severity: informational (not a bug, but the brief flags it)

Cookies: `narve_session` (#2), `pm_gateway_session` (#3).

Both auth cookies set `Domain=.narve.ai` in production via
`cookie_domain_for` / `_cookie_domain_for`. The module docstrings at
`auth/cookies.py:15-17` and `server.py:10` state this is intentional so
one login covers every subdomain dashboard.

**The brief asks us to verify auth cookies are scoped per host.** That
contradicts the deployed architecture: subdomain dashboards
(crypto.narve.ai, sports.narve.ai, etc.) are reverse-proxied through the
gateway and rely on the session cookie being sent on the subdomain
request. Removing the Domain attribute would break SSO across every
dashboard. **No change recommended without a parallel design for
per-subdomain session passing** (e.g. a short-lived signed handoff token
inserted by the proxy, replacing the cookie role).

This is the single highest-risk item in the audit — apex-scoped session
cookies mean any XSS or compromised script on **any** narve.ai subdomain
can exfiltrate the session value (mitigated by `HttpOnly=True`,
`SameSite=Strict`, and CSP on the gateway). Note the cookie is also
`SameSite=Strict`, which is unusually tight; that does close the
cross-site delivery vector but does not help with same-site script
compromise on a sibling subdomain.

Subdomain dashboards do not currently set their own cookies on the
shared apex, so there is no documented path for a sibling app to
overwrite or shadow `narve_session`. Treat that as a property to
preserve, not a defence to rely on — any future subdomain that calls
`set_cookie(..., domain=".narve.ai")` would acquire the ability to forge
or replace `narve_session` for every dashboard.

### F2. `pending_token` is apex-scoped — review
Severity: low

Cookie: `pending_token` (#1).

This cookie carries an HMAC-signed invite token while the user walks the
`/token` → `/register` flow. It is **not** HttpOnly (the JS form reads it
to pre-populate the email field) and is `SameSite=Strict`. The signature
is verified server-side (`verify_pending_token`, `:79`) so a forged
cookie value cannot bypass the gate; however the *raw token* is what
gets handed back to the user as proof of "I clicked the invite link".

The token only buys access to the `/register` form — not to authenticated
endpoints. Apex scope is convenient because the invite landing could be
served from any subdomain in theory, but in practice every link in the
existing email templates points at `narve.ai` (apex), so the cookie
**could** safely be set without `Domain`. Marking as low-severity rather
than recommending an immediate change because: (a) the cookie value is
HMAC-bound so cross-subdomain read on a malicious subdomain gains the
attacker nothing they didn't already have if they have JS execution on a
narve subdomain, and (b) the 30-minute TTL caps the blast radius.

Recommendation: when next refactoring `auth/cookies.py`, drop the Domain
attribute on `pending_token`. Leave the signature scheme as-is.

### F3. `narve_gate_access` is apex-scoped — justified
Severity: informational

Cookie: `narve_gate_access` (#4).

The site-wide gate is a single-secret access check (`SITE_ACCESS_TOKEN`).
Apex scope is required so a user who passes the gate on `narve.ai` is
also "past the gate" on any dashboard subdomain — otherwise every
subdomain redirect would re-prompt for the gate token. The cookie is
HttpOnly + SameSite=Strict + HMAC-signed against
`GATEWAY_COOKIE_SECRET`. No change recommended.

There is an existing TODO in `server.py:239-241` (C4) to replace this
shared-secret gate with per-user invite-token gating; once that lands,
the cookie's value-leak risk drops further.

### F4. `narve_impersonation` is apex-scoped — review
Severity: medium

Cookie: `narve_impersonation` (#5).

Admin-only short-lived (4h) token; HttpOnly + SameSite=Lax. Lax instead
of Strict is justified by the docstring (impersonation flow may follow
external redirects). Apex scope means any narve subdomain that gains JS
execution context against the same browser can submit
`Cookie: narve_impersonation=...` on cross-subdomain calls back to the
gateway as long as SameSite=Lax permits navigation (top-level GET).

This is the most dangerous "apex-scoped because convenient" cookie in
the inventory: a successful exfil is a full admin takeover for 4h. The
mitigations are HttpOnly (prevents JS read), Lax (prevents
cross-origin POST), the explicit `ImpersonationMiddleware` consent
gate, and the database-backed `impersonation_sessions` audit trail.

Recommendation: scope `narve_impersonation` **per host** — the admin
panel is only ever served from the apex (`narve.ai/admin/...`), never
from a dashboard subdomain. Dropping the Domain attribute would close
the lateral-from-subdomain exfil path with zero functional cost. Track
as a hardening item for the next auth pass.

### F5. `_csrf` is apex-scoped — review
Severity: low

Cookie: `_csrf` (#6).

CSRF double-submit cookie. Non-HttpOnly by necessity (JS must read it
to inject into headers/form fields). Currently set with the apex Domain
via the same helper as session cookies.

Apex scope here is **probably justified** because subdomain dashboards
that POST back to the gateway need the same cookie to be readable.
However, the dashboards do not actually CSRF-check today — the gateway
exempts subdomain-proxied requests in `CSRFMiddleware.dispatch` (lines
178-181 in `csrf.py`). So the cookie is being shipped to subdomains that
ignore it; trimming Domain would still work for the gateway-side CSRF
check and would avoid leaking the token to every dashboard host.

Recommendation: low-priority cleanup. Either (a) drop the Domain
attribute, since only the gateway origin actually validates the token,
or (b) leave as-is and rely on the cookie's non-secret nature (a CSRF
token is paired with a session, not an authenticator on its own).

### F6. `narve_lang`, `narve_tz` — apex-or-not?
Severity: informational

Cookies: `narve_lang` (#10), `narve_tz` (#11).

Both are written **without** a Domain attribute. That means they are
stored per host (exact match) — set on `narve.ai`, they do not flow to
`crypto.narve.ai`. The module docstring for `narve_tz`
(`timezones.py:148`) says "every subdomain picks it up", but that is
incorrect: without `Domain=.narve.ai` the browser scopes to the request
host.

This is arguably a **bug** — UI preferences are exactly the kind of
cookie that should follow the user across subdomains. The current state
means subdomain dashboards re-detect timezone/locale from `Intl.*` on
every first visit even though the user already set them on the apex.

Recommendation: add `Domain=.narve.ai` (production-only, via
`cookie_domain_for`) to both of these. Mirror the auth-cookie helper
pattern.

### F7. `narve_share_attribution`, `narve_shared_view`, `affiliate_code`
Severity: informational — already correct

Cookies: #7, #8, #9.

All three are deliberately scoped per host (no Domain attribute). This
is correct because they are tied to the apex-served share/affiliate
flow; a subdomain dashboard has no reason to read them. No change
required.

### F8. Helper inconsistency
Severity: low

Two functionally identical helpers exist:
- `gateway/auth/cookies.py:_cookie_domain_for` (reads
  `GATEWAY_COOKIE_DOMAIN` env var, falls back to `config.json`)
- `gateway/server.py:cookie_domain_for` (resolves apex from
  `ALLOWED_DOMAINS` via `_request_apex`)

The auth-cookies helper does **not** consult `ALLOWED_DOMAINS` and so
will not multi-apex (habbig.com + narve.ai) correctly if both are
served from the same gateway. The `server.py` helper does.

The `auth/cookies` helper is the one used for `pending_token` and
`narve_session`. The session cookie therefore relies on the
`GATEWAY_COOKIE_DOMAIN` env var (or the config.json `domain` field)
being set explicitly, while the `server.py`-side session helper
(`pm_gateway_session`, written in parallel) does per-request resolution.

This is not exploitable today (only one apex is live in production), but
it is a footgun for a future multi-apex deployment: `narve_session`
would get the wrong Domain on `habbig.com` requests.

Recommendation: consolidate to one helper. The `server.py` version is
more correct. Either move it into a shared module or import it from
`auth/cookies.py`.

## Cookie count

11 server-set cookies in `gateway/` (1 client-only theme cookie not
counted).

## Scope issues — summary

| Cookie                  | Current scope (prod) | Recommendation               | Severity     |
|-------------------------|----------------------|------------------------------|--------------|
| `pending_token`         | `.narve.ai`          | drop Domain                  | low          |
| `narve_session`         | `.narve.ai`          | keep — required for SSO      | informational |
| `pm_gateway_session`    | `.narve.ai`          | keep — required for SSO      | informational |
| `narve_gate_access`     | `.narve.ai`          | keep — required for gate UX  | informational |
| `narve_impersonation`   | `.narve.ai`          | **drop Domain (per-host)**   | **medium**   |
| `_csrf`                 | `.narve.ai`          | drop Domain (low priority)   | low          |
| `affiliate_code`        | per-host             | keep                         | ok           |
| `narve_share_attribution` | per-host           | keep                         | ok           |
| `narve_shared_view`     | per-host             | keep                         | ok           |
| `narve_lang`            | per-host             | **add `.narve.ai`** (bug)    | low          |
| `narve_tz`              | per-host             | **add `.narve.ai`** (bug)    | low          |

The single highest-priority change is **F4** (scope
`narve_impersonation` per host). Everything else is hygiene or a doc fix.

The brief's broader principle — "auth cookies should be scoped per
host" — cannot be applied to the session cookies (#2, #3) without a
parallel design for cross-subdomain session passing, since the
multi-dashboard SSO model depends on apex scope. The audit flags this
tension rather than recommending an unworkable change.

## Pre-release scope check

The pre-release surface (`/`, `prerelease_page` at `server.py:3276`,
`/api/newsletter`) was deliberately excluded per the hard rule. None of
the cookies above are set inside those handlers — the CSRF cookie is
the only one that touches the pre-release page, and only via the global
`CSRFMiddleware`, which is in audit scope but not a pre-release-specific
code path. No edits or recommendations land on pre-release code.
