# Security Policy

## Reporting a vulnerability

Email **security@narve.ai** with a proof-of-concept and the impact you
observed. We respond within **48 hours** (business days).

If you cannot reach us by email, DM the admin handle on the site and
ask for a secure-channel redirect. Do not post findings publicly until
the issue is resolved.

## Scope

**In scope:**

- `narve.ai` and all `*.narve.ai` subdomains served from production
- The public API at `/api/v1/*` and `/api/public/*`
- The Chrome extension at `chrome.google.com/webstore/detail/...`
  (see `extension/` in this repo)
- Backend weaknesses: auth bypasses, subscription-access bypasses,
  CSRF, SSRF, SQLi, RCE, leaky logs, secret exposure
- Impersonation escape (destructive route blocklist bypass)

**Out of scope:**

- DoS against our own rate-limiters (the limiters returning 429 is the
  feature, not the bug — report exceptions)
- Self-XSS requiring the user to paste code into their own console
- Anything that needs physical access to a victim's device
- Social-engineering the operator
- Version disclosure on third-party services (Cloudflare, etc.)
- Findings against staging (`staging.narve.ai`) unless they also
  reproduce on production and expose user data

## Disclosure

We credit reporters in [CHANGELOG.md](CHANGELOG.md) under `### Security`
unless you prefer anonymity. No bug bounty programme is currently
funded. Severe findings (auth bypass, subscription bypass, RCE) may
receive complimentary Pro access at our discretion.

## What we guarantee

- Every list-shaped API response is forensically watermarked per-user.
  Reported leaks can be attributed; please **do not** post
  watermarked data publicly as a demonstration.
- Admin impersonation is audit-logged with both identities and blocks
  destructive routes server-side. Reporting an impersonation-escape is
  a high-priority finding.
- Session tokens are SHA-256 hashed at rest; password hashes are
  PBKDF2-HMAC-SHA256 with a per-user salt.
- Subscription entitlement is always checked server-side in
  `_is_paid()` or `_require_paid()` — never client-side only. Bypasses
  here are severe.

## What we do not guarantee

- The scraper's Playwright sessions are treated as compromisable; we
  do not store long-lived site credentials for scraped platforms in
  the gateway DB.
- The Chrome extension is a thin reader; we do not treat it as a
  trusted client. Any API path the extension uses MUST also be safe
  when hit directly with a manufactured token.
