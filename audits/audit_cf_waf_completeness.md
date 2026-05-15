# Cloudflare WAF Completeness Audit — narve.ai

**Date:** 2026-05-15
**Auditor:** Sync read-only review of `CLOUDFLARE_CHANGES.md` against OWASP Top 10 (2021)
**Source of truth:** `/Users/shocakarel/Habbig/CLOUDFLARE_CHANGES.md` (commits 2026-04-21 → 2026-05-15)
**Scope:** Edge / Cloudflare WAF posture only. App-side controls referenced for context but NOT scored as edge coverage.
**Method:** Synchronous bash; no live probes against pre-release surfaces. No edge rules modified by this audit.

---

## TL;DR

The documented Cloudflare WAF rules (Rules A–E from 2026-04-21 + 2026-05-15
`/admin/api/*` shield) cover **noise-floor abuse, recon tooling, host-header
forgery, and admin/auth rate-limiting**. They do **NOT** systematically
defend against OWASP Top 10 categories at the edge — defence is split, with
the app layer carrying most of the load.

**Verdict: PARTIAL.** Edge posture is adequate as a noise floor, insufficient
as a primary OWASP-Top-10 control. Ten gaps are already self-identified in
the 2026-05-14 "WAF + rate-limit posture audit" entry (items 1–10) and
remain open. This audit adds an OWASP-axis view and flags four further
gaps not on that list.

---

## 1. Documented WAF rules (as of 2026-05-15)

Compiled from `CLOUDFLARE_CHANGES.md` — these are the rules an attacker
would actually encounter at the edge:

| # | Name / Source | Action | Trigger |
|---|---|---|---|
| A | 2026-04-21 + 2026-05-14 host allowlist | Block | Unknown `*.narve.ai` host not in 13-subdomain allowlist |
| B | 2026-04-21 | Managed Challenge | `/api/*` without `narve.ai` referer AND not `narve-extension` UA |
| C | 2026-04-21 | Block | UA contains `sqlmap`/`nikto`/`nmap`, OR path is `/.env`, OR path starts with `/wp-admin` |
| D | 2026-04-21 rate-limit | Block 60s | `/auth/*` >20 req/min/IP |
| E | 2026-04-21 rate-limit | Block | `/admin/*` >60 req/min/IP |
| F | 2026-05-15 custom rule | Managed Challenge | `/admin/api/*` (Posture A) |
| G | 2026-05-15 rate-limit | Block 10min | `/admin/api/*` >60 req / 5 min / IP |

Cache rules (static / health / api / admin bypasses) and DNS / tunnel
config are out of scope for this WAF audit.

---

## 2. OWASP Top 10 (2021) coverage matrix

Legend: `EDGE` = stopped at Cloudflare; `APP` = stopped only by gateway code;
`NONE` = no documented control at either layer; `PARTIAL` = limited or
conditional coverage.

| OWASP | Category | Edge coverage | Gap |
|---|---|---|---|
| A01 | Broken Access Control | `NONE` | No edge ACLs. Rule E rate-limits `/admin/*` but doesn't enforce auth. IDOR, missing-function-level checks, path traversal entirely up to app. No edge allowlist for forensic endpoints (`/admin/trace-watermark`, `/admin/health-monitor`). |
| A02 | Cryptographic Failures | `PARTIAL` | Universal SSL + HSTS preload (header audit); no edge enforcement of TLS minimum version documented; no rule pinning `cf.tls_version` >= 1.2. |
| A03 | Injection (SQLi/NoSQLi/cmd/LDAP) | `PARTIAL` | Rule C blocks `sqlmap` UA but **only** by user-agent string — trivially bypassed by `curl -A 'Mozilla/5.0'`. **Cloudflare Managed Rules** (OWASP Core Ruleset / SQLi / XSS managed rules) are not mentioned anywhere in `CLOUDFLARE_CHANGES.md`. If they're enabled, document it; if not, this is the largest single gap. |
| A04 | Insecure Design | `n/a` | Architectural; cannot be addressed at edge. |
| A05 | Security Misconfiguration | `PARTIAL` | Rule A blocks forged hosts; Rule C blocks `/.env` + `/wp-admin`. No coverage of `/.git/`, `/.aws/`, `/config.php`, `/backup.zip`, `/server-status`, etc. No edge enforcement that the origin is only reachable via the tunnel (relies on `SubproductMiddleware` rejecting requests missing `CF-Connecting-IP`). |
| A06 | Vulnerable / Outdated Components | `NONE` | No virtual-patching managed rules referenced (e.g. Log4Shell, Spring4Shell, ProxyShell families). Cloudflare's "Cloudflare Managed Ruleset" / "Cloudflare Specials" would cover this — not documented as enabled. |
| A07 | Identification & Authentication Failures | `PARTIAL` | Rule D rate-limits `/auth/*` at 20/min/IP (intentionally laxer than app's 5/15min); no datacenter-IP challenge (gap #4 in the 2026-05-14 audit); no bot-management score block (gap #5); no MFA-bypass-specific rules; no protection against password-reset enumeration. |
| A08 | Software & Data Integrity Failures | `PARTIAL` | `/stripe/webhook` is CSRF-exempt with **no IP allowlist** at the edge (gap #3 in the 2026-05-14 audit). No edge rule on subresource integrity. |
| A09 | Logging & Monitoring Failures | `PARTIAL` | No Logpush / Notifications rule on `/admin/trace-watermark` (gap #7 in 2026-05-14 audit). No alert on Rule A/C blocks. No edge alert on rate-limit floods. |
| A10 | SSRF | `NONE` | `audit_ssrf.md` exists separately; no edge rule documented to inspect outbound or block obvious SSRF-as-input patterns (`http://169.254.169.254`, `http://localhost`, `file://`, etc.). The narve gateway makes outbound calls to scraper feeds + Stripe, which is a classic SSRF surface — no edge gate documented. |

---

## 3. Mapping documented rules to OWASP categories

For traceability — what each documented rule actually contributes:

- **Rule A** → A05 (host-header forgery surface reduction).
- **Rule B** → A07 (light bot deterrent on `/api/*`), with collateral
  damage risk to `/api/embed/*` (gap #1 in 2026-05-14 audit).
- **Rule C** → A03 (UA-only, weak), A05 (path-based, narrow).
- **Rule D** → A07 (auth rate-limit floor).
- **Rule E** → A01 + A07 (admin rate-limit floor, no ACL).
- **Rule F** → A01 + A07 (admin-API JS-challenge for headless bots).
- **Rule G** → A01 + A07 (admin-API tighter rate-limit).

Categories with **zero** documented edge rule attribution: A02 (TLS
config), A04 (n/a), A06 (managed-ruleset virtual patches), A10 (SSRF
egress / payload patterns).

---

## 4. Coverage gaps (the actual answer)

### 4.1 Inherited from 2026-05-14 self-audit (still open)

These are already documented in `CLOUDFLARE_CHANGES.md` §"Gaps still open"
and are restated here for completeness. None are closed as of 2026-05-15.

1. **`/api/embed/*` edge limit + Rule B carve-out** (referer check
   challenges legit cross-origin traffic).
2. **`/api/scraper/*` edge limit** (currently HMAC-only at app).
3. **`/stripe/webhook` Stripe IP allowlist** — A08 critical. Path is
   CSRF-exempt; any IP can POST today.
4. **Datacenter-IP managed challenge on `/auth/*`** — A07.
5. **Bot-management ASN / score block** (replaces Rule C's UA-only
   filter) — A03 / A07.
6. **Admin-IP allowlist for `/admin/health-monitor` +
   `/api/admin/health-monitor`** — A01.
7. **Alert on every `/admin/trace-watermark` access** — A09 critical.
8. **Per-IP limit on `/api/markets/connect/*`** (app limit is per-user,
   so attacker with N accounts on one IP gets N × the limit) — A07.
9. **DDoS incident-response runbook + "Under Attack" mode toggle
   procedure** — A09.
10. **General fallback 600/min/IP apex-wide rate-limit** — A07.

### 4.2 New gaps (not on the 2026-05-14 list)

11. **Cloudflare Managed Rulesets (OWASP Core / Cloudflare Specials)
    not documented.** This is the single largest OWASP-Top-10 gap.
    Cloudflare's managed rulesets are the standard mitigation for A03
    (SQLi/XSS payload patterns) and A06 (virtual patches for known
    CVEs). `CLOUDFLARE_CHANGES.md` makes no reference to enabling,
    tuning, or excluding any managed ruleset. **Required action:**
    confirm in the Cloudflare dashboard whether the "Cloudflare Managed
    Ruleset" and "Cloudflare OWASP Core Ruleset" are enabled at default
    sensitivity, and append the configuration to `CLOUDFLARE_CHANGES.md`
    so the documented edge posture matches reality.

12. **No TLS minimum-version enforcement rule documented.** A02. SSL/TLS
    > Edge Certificates > "Minimum TLS Version" should be `TLS 1.2` (or
    `TLS 1.3`). Not visible from the changes doc.

13. **No `/.git/`, `/.svn/`, `/.aws/`, `/.docker/`, `/backup*`,
    `/server-status`, `/phpinfo*` block list.** A05. Rule C catches
    `/.env` and `/wp-admin` only. Add a single block rule for the full
    secrets-and-leaks path family.

14. **No method allowlist per path family.** A01 / A05. Edge currently
    accepts arbitrary HTTP methods (`TRACE`, `PATCH`, `OPTIONS`,
    `CONNECT`) on every endpoint. App-side likely returns 405, but the
    edge could short-circuit. Especially relevant for `/api/embed/*`
    (GET-only) and `/stripe/webhook` (POST-only).

15. **Rule C is UA-substring-based.** A03. `sqlmap`/`nikto`/`nmap` UAs
    are blocked, but the same tools with `--user-agent='Mozilla/5.0'`
    sail through. This rule provides theatre, not defence; combine with
    bot-management score (gap #5) or remove.

16. **No payload-size limit at edge.** A04/A05 adjacent. No rule
    referencing `http.request.body.size` or similar. App-side limits
    exist but a 100 MB POST still consumes tunnel bandwidth before
    being rejected.

17. **No rule covering `/.well-known/` exposure.** A05. Some
    `.well-known` paths are legitimate (acme-challenge, security.txt,
    apple-app-site-association); others (`/.well-known/secrets`) are
    not. Worth an explicit allowlist of expected `.well-known/*`
    leaves rather than a blanket bypass.

---

## 5. What's NOT a gap

To avoid the report becoming a wishlist, calling out where the documented
posture is sufficient:

- **Host-header attacks** (A05): Rule A is correct and comprehensive,
  and the 2026-05-14 entry correctly extended the allowlist to all 13
  subdomains. The host check runs **first** in evaluation order — good.
- **Tunnel-only origin reachability** (A05): documented via
  `SubproductMiddleware`'s `CF-Connecting-IP` requirement. App-layer,
  but architecturally correct.
- **Auth rate-limit philosophy** (A07): the "edge as noise floor, app as
  enforcement" split is explicitly documented and intentional. The
  laxer-at-edge design is correct given the app-layer enforcement.
- **Admin-API hardening** (A01/A07): the 2026-05-15 entry adding Rules
  F + G with managed challenge + 60/5min rate limit closes the
  carry-over LOW #1 properly.

---

## 6. Recommended deploy order for gaps

If implementing all gaps in a single pass:

1. **First (severity-driven):**
   - Gap #3 (Stripe IP allowlist) — A08 critical.
   - Gap #11 (enable Cloudflare Managed Rulesets) — single largest OWASP
     A03/A06 coverage win.
   - Gap #7 (`/admin/trace-watermark` Logpush alert) — A09.
   - Gap #12 (TLS min version) — A02 one-click toggle.
2. **Second (defence-in-depth):**
   - Gaps #4, #5, #8, #15 (auth + bot management hardening) — A07/A03.
   - Gap #6 (admin IP allowlist) — A01.
   - Gap #13 (`.git/`/`.aws/` path block) — A05.
3. **Third (operational hardening):**
   - Gaps #1, #2 (embed + scraper edge limits).
   - Gap #10 (apex 600/min/IP fallback).
   - Gap #9 ("Under Attack" runbook).
   - Gaps #14, #16, #17 (method allowlist, payload size, `.well-known`).

Each batch should be followed by re-running this audit and appending the
delta to `CLOUDFLARE_CHANGES.md` per the established pattern.

---

## 7. Coverage gaps (executive summary)

The single sentence to relay: **edge WAF is a useful noise floor (host
allowlist + rate-limits + admin-API shield) but does not defend OWASP
A01, A03, A06, A09, A10 in any structured way; the largest missing piece
is Cloudflare Managed Rulesets (gap #11), followed by Stripe webhook IP
allowlist (#3) and the forensic-endpoint alerting (#7).**

Ten gaps already documented in the 2026-05-14 self-audit remain open;
seven additional gaps surfaced by mapping rules onto OWASP Top 10
(#11–#17). Detailed list in §4.
