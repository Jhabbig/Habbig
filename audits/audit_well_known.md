# /.well-known/ + disclosure surface audit — 2026-05-15

Synchronous curls only. Pre-release page untouched.

## Disclosure-contact files in the repo

- `SECURITY.md` (repo root) — present. Contact: `security@narve.ai`, 48-hour
  SLA, scope/disclosure sections defined.
- `gateway/server.py:3779` — `@app.get("/.well-known/security.txt")`
  serves Contact/Expires/Preferred-Languages/Policy. Env-driven via
  `SECURITY_TXT_CONTACT` and `SECURITY_TXT_EXPIRES`.
- No `security.txt` file shipped statically — generated dynamically per
  request from env vars (acceptable, but see "Signing" gap below).

## Curl matrix (production: https://narve.ai)

| Path                                          | Status | Notes                                          |
|-----------------------------------------------|--------|------------------------------------------------|
| `/.well-known/security.txt`                   | 200    | text/plain, correct body, signed-no            |
| `/.well-known/change-password`                | 302    | → `/gate` (NOT to `/profile/password`)         |
| `/.well-known/openid-configuration`           | 302    | → `/gate` (no OIDC; should be 404)             |
| `/.well-known/oauth-authorization-server`     | 302    | → `/gate`                                      |
| `/.well-known/jwks.json`                      | 302    | → `/gate`                                      |
| `/.well-known/host-meta`                      | 302    | → `/gate`                                      |
| `/.well-known/webfinger`                      | 302    | → `/gate`                                      |
| `/.well-known/nodeinfo`                       | 302    | → `/gate`                                      |
| `/.well-known/assetlinks.json`                | 302    | → `/gate` (no Android app)                     |
| `/.well-known/apple-app-site-association`     | 302    | → `/gate` (no iOS app)                         |
| `/.well-known/mta-sts.txt`                    | 302    | → `/gate` (no MTA-STS policy served)           |
| `/.well-known/dnt-policy.txt`                 | 302    | → `/gate`                                      |
| `/robots.txt`                                 | 200    | cached HIT, 4h TTL                             |
| `/security` (Policy URL in security.txt)      | 302    | → `/gate` — researchers can't read policy      |

Servers respond from Cloudflare with full security-header stack on the
200 responses (CSP, HSTS preload, X-Frame-Options DENY, X-Content-Type-
Options nosniff, Permissions-Policy, Referrer-Policy, COOP, CORP).

## security.txt body served

```
Contact: mailto:security@narve.ai
Expires: 2027-04-08T00:00:00Z
Preferred-Languages: en
Policy: https://narve.ai/security
```

Conforms to RFC 9116 minimum (Contact + Expires). Expires set ~11 months
out — within the 1-year recommendation.

## Gaps (ordered by severity)

### HIGH — Policy URL is gated, can't be read by researchers

`Policy: https://narve.ai/security` in security.txt returns 302→/gate
during pre-release. The whole point of the Policy field is to let an
external reporter read the disclosure policy without authentication.
RFC 9116 §2.5.6 explicitly states the Policy URL "MUST be reachable".

**Options:**
1. Add `/security` to `_PUBLIC_PATHS` in `gateway/server.py` and route
   it to render `SECURITY.md` (or a static `gateway/static/security.html`)
   as a public page.
2. Point Policy to a hosted URL outside the gate (e.g. the GitHub
   `SECURITY.md`: `Policy: https://github.com/Jhabbig/Habbig/blob/main/SECURITY.md`).

### MEDIUM — `/.well-known/change-password` should redirect to `/profile/password`, not `/gate`

The W3C [change-password-url](https://w3c.github.io/webappsec-change-password-url/)
spec says password managers / browsers will probe `/.well-known/change-password`
to deep-link users to the change-password form. Currently it 302s to
`/gate`. After the gate ships off, this still falls through the
catch-all and dies.

**Fix:** add a small handler:
```python
@app.get("/.well-known/change-password")
async def well_known_change_password():
    return RedirectResponse("/profile/password", status_code=302)
```
And add to `_PUBLIC_PATHS` so it survives the gate. `/profile/password`
itself is gated (which is correct — user must auth first), but the
*redirect* needs to be reachable.

### MEDIUM — Unwanted endpoints disclose pre-release state

`/.well-known/openid-configuration`, `/oauth-authorization-server`,
`/jwks.json`, `/webfinger`, `/host-meta`, `/nodeinfo`, `/dnt-policy.txt`,
`/assetlinks.json`, `/apple-app-site-association`, `/mta-sts.txt` all
return **302 → /gate**. A scanner probing for OIDC/OAuth/Fediverse/mobile
deep-links currently learns the site is gated rather than getting a clean
404. After GA they'll just leak the catch-all behaviour.

**Fix:** add an explicit handler that returns **404 for unimplemented
well-known suffixes** so we don't pollute scanner output and don't tease
implementations that don't exist:
```python
@app.get("/.well-known/{suffix:path}")
async def well_known_404(suffix: str):
    # security.txt and change-password are handled above; everything
    # else is intentionally unimplemented.
    raise HTTPException(status_code=404)
```
Order this AFTER `/.well-known/security.txt` and
`/.well-known/change-password`.

### LOW — security.txt is not PGP-signed

RFC 9116 §2.4 recommends (SHOULD) signing security.txt with the
contact's PGP key. Currently served as plain text. If we ever expect
researchers in adversarial conditions (DNS-poisoning, CDN compromise)
to verify the Contact line is genuine, sign it.

**Skip if:** the threat model treats CDN takeover as out-of-scope (which
SECURITY.md already implies — "DoS against our own rate-limiters" out
of scope, Cloudflare trusted as terminator).

### LOW — `Encryption:` field absent

Not required, but conventional. Add a PGP fingerprint or HTTPS-served
key URL if there's a real key. Otherwise omit (current state — fine).

### LOW — `Canonical:` field absent

Helps detect security.txt being mirrored elsewhere. Add:
```
Canonical: https://narve.ai/.well-known/security.txt
```

### INFO — no `/.well-known/security.txt` redirect from `/security.txt`

RFC 9116 §3 says servers MAY also serve at `/security.txt` for legacy
compatibility. Not required. Current state: `/security.txt` falls
through to the catch-all → /gate (same MEDIUM bucket above).

## Source pointers

- security.txt handler: `gateway/server.py:3779-3789`
- gate-bypass list: `gateway/server.py:1450-1485` (`_PUBLIC_PATHS`)
- auth-middleware bypass: `gateway/auth/middleware.py:33-39`
  (skips DB session for `/.well-known/` prefix and root pages)
- SECURITY.md disclosure policy: repo root, contact `security@narve.ai`

## Verification commands run

```
curl -sS -i -L --max-time 15 https://narve.ai/.well-known/security.txt
curl -sS -i -L --max-time 15 https://narve.ai/.well-known/change-password
curl -sS -i -L --max-time 15 https://narve.ai/.well-known/openid-configuration
curl -sS -i    --max-time 10 https://narve.ai/.well-known/host-meta
curl -sS -i    --max-time 10 https://narve.ai/.well-known/webfinger
curl -sS -i    --max-time 10 https://narve.ai/.well-known/assetlinks.json
curl -sS -i    --max-time 10 https://narve.ai/.well-known/apple-app-site-association
curl -sS -i    --max-time 10 https://narve.ai/.well-known/nodeinfo
curl -sS -i    --max-time 10 https://narve.ai/.well-known/dnt-policy.txt
curl -sS -i    --max-time 10 https://narve.ai/robots.txt
curl -sS -i    --max-time 10 https://narve.ai/security
curl -sS -i    --max-time 10 https://narve.ai/.well-known/mta-sts.txt
curl -sS -i    --max-time 10 https://narve.ai/.well-known/oauth-authorization-server
curl -sS -i    --max-time 10 https://narve.ai/.well-known/jwks.json
```
