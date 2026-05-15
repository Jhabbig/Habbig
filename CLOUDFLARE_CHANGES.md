# Cloudflare changes — subproduct split + hardening

Appended every time this codebase changes anything that needs a matching
Cloudflare configuration change. Append, don't rewrite.

No Cloudflare MCP tools were wired into this agent run, so these
changes are specified manually. Apply via the dashboard or `terraform
apply` — both are listed where the API shape isn't ambiguous.

---

## 2026-04-21 — subproduct subdomains + WAF pass #1

### A. DNS records (proxied)

Six subdomains, all CNAMEs to the apex, proxied through Cloudflare
(orange cloud). The gateway's `SubproductMiddleware` routes each to
the right brand via the `Host` header.

| Record type | Name                   | Target     | Proxied | TTL  |
|-------------|------------------------|------------|---------|------|
| CNAME       | sports.narve.ai        | narve.ai   | yes     | auto |
| CNAME       | weather.narve.ai       | narve.ai   | yes     | auto |
| CNAME       | world.narve.ai         | narve.ai   | yes     | auto |
| CNAME       | crypto.narve.ai        | narve.ai   | yes     | auto |
| CNAME       | midterm.narve.ai       | narve.ai   | yes     | auto |
| CNAME       | traders.narve.ai       | narve.ai   | yes     | auto |

**Terraform:**

```hcl
locals {
  narve_zone_id = "REPLACE_WITH_ZONE_ID"
  subproducts   = ["sports", "weather", "world", "crypto", "midterm", "traders"]
}

resource "cloudflare_record" "subproduct" {
  for_each = toset(local.subproducts)
  zone_id  = local.narve_zone_id
  name     = each.key
  type     = "CNAME"
  value    = "narve.ai"
  proxied  = true
  ttl      = 1
}
```

### B. WAF rules (Custom Rules → Rules)

Add in the order below. Cloudflare evaluates custom rules top-to-bottom;
the host-allowlist rule MUST run before the path-based rules so a
forged `Host` never reaches them.

#### Rule A — block unknown narve subdomains

```
(http.host matches "^[^.]+\\.narve\\.ai$"
  and not http.host in {"www.narve.ai" "api.narve.ai" "admin.narve.ai"
    "staging.narve.ai" "sports.narve.ai" "weather.narve.ai"
    "world.narve.ai" "crypto.narve.ai" "midterm.narve.ai"
    "traders.narve.ai"})
→ Block
```

#### Rule B — managed challenge on API without narve referer

```
(starts_with(http.request.uri.path, "/api/")
  and not http.referer contains "narve.ai"
  and not http.user_agent contains "narve-extension")
→ Managed Challenge
```

(The extension sets `User-Agent: narve-extension/<version>` via the
`externally_connectable` path; see `extension/background.js` to add
that header alongside the Bearer token.)

#### Rule C — block known recon tooling + sensitive paths

```
(lower(http.user_agent) contains "sqlmap"
  or lower(http.user_agent) contains "nikto"
  or lower(http.user_agent) contains "nmap"
  or http.request.uri.path eq "/.env"
  or starts_with(http.request.uri.path, "/wp-admin"))
→ Block
```

#### Rule D — rate limit /auth

Rate-limit rules tab (Security → WAF → Rate limiting rules):

- Match: `starts_with(http.request.uri.path, "/auth/")`
- Characteristics: IP source address
- Period: 1 minute
- Requests allowed: 20
- When exceeded: Block for 60s
- Description: `auth-rate-limit`

#### Rule E — rate limit /admin

- Match: `starts_with(http.request.uri.path, "/admin/")`
- Characteristics: IP source address
- Period: 1 minute
- Requests allowed: 60
- When exceeded: Block

### C. Cache rules (Caching → Cache rules)

Assets are served by the gateway but most never change between
deploys. Cache aggressively at the edge.

| Rule name | Match                                                         | Cache behaviour                          |
|-----------|---------------------------------------------------------------|------------------------------------------|
| static    | `ends_with(http.request.uri.path, ".css")` OR `.js` OR `.png` OR `.jpg` OR `.svg` OR `.woff2` | Cache eligible. Edge TTL = 30 days. Browser TTL = 30 days. |
| health    | `http.request.uri.path eq "/health"`                          | Bypass cache.                            |
| api       | `starts_with(http.request.uri.path, "/api/")`                 | Bypass cache.                            |
| admin     | `starts_with(http.request.uri.path, "/admin/")`               | Bypass cache.                            |

Static rule must appear above the bypasses since cache rules are first-
match-wins.

### D. Verification

After applying:

```bash
for h in sports weather world crypto midterm traders; do
  curl -sIL -o /dev/null -w "$h: %{http_code}\n" "https://$h.narve.ai/"
done
# All six should 200 (or 302 to /gate for unauthed users).

curl -sIL -o /dev/null -H "Host: foo.narve.ai" -w "unknown-host: %{http_code}\n" https://narve.ai/
# should be 403/1020 (blocked by Rule A).

curl -sIL -o /dev/null -w "direct-origin: %{http_code}\n" http://100.69.44.108:7000/
# If Tailscale-reachable: should be 403 from SubproductMiddleware
# (cf-connecting-ip absent).
```

---

## 2026-05-14 — 7 new subdomains

Added Cloudflare-side config for the new subproduct dashboards landed in
commit f55d78f-onward.

### DNS records (CNAME → narve.ai, proxied/orange-cloud)
- voters.narve.ai
- climate.narve.ai
- disasters.narve.ai
- whale.narve.ai
- cb.narve.ai            (NOT "centralbank" — display_name is "Central Bank Tracker" but subdomain is shortened to fit the brand)
- health.narve.ai        (NOT "world-health" — same shortening; dashboard_key in code is `world_health`)
- love.narve.ai          (13th subproduct, MVP)

**Terraform delta** (extend the `subproducts` local from the 2026-04-21
entry — keep the original 6, append the 7 below):

```hcl
locals {
  subproducts = [
    # original 6 (see 2026-04-21 entry)
    "sports", "weather", "world", "crypto", "midterm", "traders",
    # new 7 (2026-05-14)
    "voters", "climate", "disasters", "whale", "cb", "health", "love",
  ]
}
```

The existing `cloudflare_record.subproduct` `for_each` resource picks
these up automatically — no new resource block needed.

### Tunnel routes

Update the cloudflared config (`~/.cloudflared/config.yml` on prod box)
to include each new hostname pointed at `http://localhost:7000` (the
gateway multiplexes by `Host` header, so all subdomains hit port 7000
then proxy internally to the right subproduct port).

Append under the existing `ingress:` block, before the catch-all
`service: http_status:404` rule:

```yaml
  - hostname: voters.narve.ai
    service: http://localhost:7000
  - hostname: climate.narve.ai
    service: http://localhost:7000
  - hostname: disasters.narve.ai
    service: http://localhost:7000
  - hostname: whale.narve.ai
    service: http://localhost:7000
  - hostname: cb.narve.ai
    service: http://localhost:7000
  - hostname: health.narve.ai
    service: http://localhost:7000
  - hostname: love.narve.ai
    service: http://localhost:7000
```

**Reload steps** (run on prod box as the `cloudflared` user):

```bash
# 1. Validate config syntax before reloading — bad YAML kills the tunnel.
cloudflared tunnel ingress validate

# 2. Dry-run match each new hostname against the rules.
for h in voters climate disasters whale cb health love; do
  cloudflared tunnel ingress rule "https://$h.narve.ai/" \
    | grep -E "(matched|service)"
done
# Each should report `service: http://localhost:7000`.

# 3. Graceful reload — picks up the new ingress without dropping conns.
sudo systemctl reload cloudflared
# (or: `cloudflared tunnel run --config ~/.cloudflared/config.yml` if not
#  running as a systemd service — kill the old process after the new one
#  reports `Registered tunnel connection`.)

# 4. Confirm reload landed.
sudo systemctl status cloudflared --no-pager | grep -E "(Active|Reloaded)"
```

### WAF / Rate limits

No new WAF rules required — subdomains inherit the apex's existing rules.
Rate limits propagate by Host pattern: configured `*.narve.ai` already
covers them. Confirm in Cloudflare dashboard.

**However:** the 2026-04-21 Rule A (block unknown narve subdomains) has
a hard-coded allowlist. Extend it to include the 7 new hosts, otherwise
they will be blocked at the edge before the tunnel sees the request:

```
(http.host matches "^[^.]+\\.narve\\.ai$"
  and not http.host in {"www.narve.ai" "api.narve.ai" "admin.narve.ai"
    "staging.narve.ai" "sports.narve.ai" "weather.narve.ai"
    "world.narve.ai" "crypto.narve.ai" "midterm.narve.ai"
    "traders.narve.ai" "voters.narve.ai" "climate.narve.ai"
    "disasters.narve.ai" "whale.narve.ai" "cb.narve.ai"
    "health.narve.ai" "love.narve.ai"})
→ Block
```

### SSL

Cloudflare Universal SSL covers all subdomains automatically — no action.

### Verification

```bash
for h in voters climate disasters whale cb health love; do
  curl -sIL -o /dev/null -w "$h: %{http_code}\n" "https://$h.narve.ai/health"
done
# All seven should 200.
```

### Tasks remaining
- [ ] DNS records added (manual, Cloudflare dashboard)
- [ ] Tunnel route added (manual, cloudflared config + reload)
- [ ] Smoke test each subdomain returns 200 on `/health` after deploy

---

## 2026-05-14 — idempotent DNS sync script

`scripts/cloudflare_dns_sync.py` replaces the manual Cloudflare-dashboard
walk for the 13 subproduct subdomains. It reads `gateway/config.json` as
the source of truth, diffs against the live zone via Cloudflare's REST
API, and reports/creates missing CNAMEs.

### Usage

```bash
# Dry-run (read-only — default; safe to run anywhere).
python3 scripts/cloudflare_dns_sync.py

# Apply: create any missing CNAMEs (proxied=true, ttl=1, content=narve.ai).
python3 scripts/cloudflare_dns_sync.py --apply
```

### Env vars

Added to `gateway/.env.example`:

```
CLOUDFLARE_API_TOKEN=     # scoped: Zone:DNS:Edit on narve.ai
CLOUDFLARE_ZONE_ID=       # narve.ai zone identifier
```

Token must be zone-scoped — create at
<https://dash.cloudflare.com/profile/api-tokens> with the "Edit zone DNS"
template restricted to the `narve.ai` zone. Never use a global / account-
wide token here.

### Safety

- **Dry-run is the default.** `--apply` must be passed explicitly.
- **The script never deletes records.** "Extra" subdomains in the zone
  but absent from `gateway/config.json` are surfaced for manual review
  only — leave deletions to a human via the Cloudflare dashboard.
- **30s timeout** per HTTP request (avoids hanging in CI).
- **Idempotent:** running it repeatedly is a no-op once the zone is in
  sync. Re-run after adding a subproduct to `gateway/config.json`.

### When to run

- After adding/renaming a subproduct in `gateway/config.json`.
- As a periodic drift check (cron / monthly manual run).
- Before/after a Cloudflare dashboard-side change, to confirm reality
  matches the code-defined expectation.

Replaces the "DNS records added (manual, Cloudflare dashboard)" task in
the 2026-05-14 entry above.

---

## 2026-05-14 — WAF + rate-limit posture audit

End-to-end audit of edge rules vs. app-side enforcement (see
`/tmp/narve_cloudflare_audit_20260514_1708.md` for the working copy that
generated this entry). No new edge rules pushed this pass — the items
below are the **current reality** so future audits can diff against
something concrete, plus the **gaps still open**.

### App-side rate-limit reality (cross-reference)

Source: `gateway/security/rate_limiter.py` (`SlidingWindowRateLimiter`,
`@rate_limit` decorator, shared `auth:<ip>` bucket) plus inline
`server._is_rate_limited(...)` calls in `server.py` and
`server_features.py`.

| Endpoint group         | App-side limit                                   | Where                                                                              |
|------------------------|--------------------------------------------------|------------------------------------------------------------------------------------|
| `/auth/login`          | 10/5min/IP **plus** 5/15min shared `auth:<ip>`   | `server_features.py:1714`, plus `_auth_rate_limited` in `server.py:1623`           |
| `/auth/register`       | 5/10min/IP **plus** 5/15min shared `auth:<ip>`   | `server_features.py:1554`                                                          |
| `/auth/forgot-password`| 3/hour/IP **and** 3/hour/email                   | `server_features.py:248,258`                                                       |
| `/auth/reset-password` | 5/hour/IP                                        | `server_features.py:307`                                                           |
| `/auth/logout`         | 20/min/IP                                        | `server_features.py:1810`                                                          |
| `/api/markets/connect/*` | **5/min per-user** (NOT per-IP)                | `market_routes.py:286,329,364`                                                     |
| `/admin/jobs/*`        | 30–120/min/admin (varies by sub-route)           | `admin_jobs_routes.py:85,107,121,133,145`                                          |
| `/admin/*` (general)   | No blanket limit — admin gating via `_require_admin_user` | `admin_routes.py`                                                          |
| `/api/embed/*`         | **None at app layer**                            | `embed_routes.py` (no `@rate_limit`)                                               |
| `/api/scraper/ingest`  | **None at app layer**; HMAC API-key gates access | `scraper/transmission/pusher.py:6`                                                 |
| `/api/search/*`        | 120–180/min per `_search_rate_key`               | `search_routes.py:162,344,390`                                                     |
| `/api/notifications/*` | 20–240/min/user                                  | `notification_routes.py:62,125,139,175,183,191,203,211`                            |
| `/api/push/*`          | 5–60/min/user                                    | `push_routes.py:44,65,111,131,158`                                                 |

Client IP at the app layer comes from `CF-Connecting-IP` first, then
`X-Forwarded-For[0]`, then `request.client.host`. Anything that bypasses
the tunnel (direct origin hits via Tailscale) is rejected by
`SubproductMiddleware` because `CF-Connecting-IP` is absent.

### Edge limit ↔ app limit interaction

The documented edge rules (Rule D `/auth/*` at 20/min/IP, Rule E
`/admin/*` at 60/min/IP) are intentionally **laxer** than the app
limits. The edge is the noise floor that filters out scripted abuse
before it reaches uvicorn; the app limits are the real enforcement and
are calibrated tighter (e.g. 5/15min shared auth bucket vs. 20/min edge
auth limit). Do not relax app limits assuming edge will catch — they're
both load-bearing.

### Forensic / sensitive admin endpoints (no special edge rule yet)

- `GET /admin/health-monitor`, `GET /api/admin/health-monitor`
  (`admin_health_monitor_routes.py`) — single-pane status of all 13
  services. Admin-gated app-side; no edge admin-IP allowlist.
- `GET /admin/trace-watermark?id=<wm>` (`admin_routes.py:712`) — reverse
  lookup for per-recipient email watermarks. Every hit (including
  misses on forged fingerprints) is audit-logged via
  `EMAIL_WATERMARK_TRACE`. No edge rule alerts on access.

Both endpoints rely entirely on `_require_admin_user` today; if the edge
ever lets traffic reach them without admin session cookies, the app
returns 403, but the access attempt is not surfaced to ops outside the
in-database audit log.

### Gaps still open (edge work TODO)

1. **`/api/embed/*` edge limit + Rule B exception.** Embed widgets are
   served cross-origin to customer sites, so Rule B's referer check
   (managed challenge if referer doesn't contain `narve.ai`) will
   challenge legit traffic. Need: edge rate limit of 1000/min/IP scoped
   to `/api/embed/*` AND an explicit Rule B carve-out for that prefix.
2. **`/api/scraper/*` edge limit.** Spec calls for 30/min/IP with
   API-key bypass. Currently only HMAC gating at app. Recommended CF
   rule: rate-limit when `http.request.headers["authorization"]` is
   missing or doesn't match the expected pattern.
3. **`/stripe/webhook` Stripe IP allowlist.** Path is CSRF-exempt
   (`security/csrf.py:50`) but no edge rule restricts who may POST to
   it. Add WAF rule: allow only `ip.src in $stripe_webhook_ips`,
   block everything else. Stripe publishes the list at
   <https://stripe.com/files/ips/ips_webhooks.json>.
4. **Datacenter-IP managed challenge on `/auth/*`.** Cloudflare bot
   management exposes a "datacenter" IP category — challenge every
   `/auth/*` request whose source is a known hosting provider. Stops
   credential-stuffing from rented infra.
5. **Bot-management ASN/score block.** Rule C currently filters by
   user-agent string only (trivially spoofed). Add a rule using
   `cf.bot_management.score < 30` (definite bot) → Block.
6. **Admin-IP allowlist for `/admin/health-monitor` +
   `/api/admin/health-monitor`.** App auth is sufficient functionally,
   but edge restriction adds defence in depth for a high-signal
   reconnaissance target (it enumerates 13 services).
7. **Alert on every `/admin/trace-watermark` access.** Add a Logpush
   filter or Cloudflare Notifications rule that pings ops on every
   request to this path (including 403s). The endpoint should fire
   rarely; volume = possible compromise.
8. **Per-IP limit on `/api/markets/connect/*`.** App limit is per-user,
   so an attacker with N accounts on one IP gets N × the limit. Add CF
   rate limit of 120/min/IP to bound the IP regardless of account
   rotation.
9. **DDoS incident-response runbook.** Document the procedure for
   flipping to "Under Attack" mode (zone-level toggle) and rolling
   back. Cross-link from `RUNBOOK.md`.
10. **General fallback limit.** Spec asks for 600/min/IP apex-wide;
    currently no such rule exists. Add as last rate-limit rule (lowest
    priority) so unhandled paths still have a noise-floor cap.

### Tasks remaining (this audit)

- [ ] Land items 1–10 above as edge rules (probably across 2–3
      deploy passes; items 3 and 7 are highest priority — webhook
      origin spoofing and forensic-endpoint silent access).
- [ ] Re-run audit after each batch; append a new dated entry to this
      file with the diff.

---

## 2026-05-14 evening — Cloudflare Tunnel ingress

The narve.ai cloudflared config on the prod box (`~/.cloudflared/config.yml` or wherever)
needs the 13 subdomains explicitly listed:

```yaml
tunnel: narve-prod-XXXX
credentials-file: /home/julianhabbig/.cloudflared/narve-prod-XXXX.json

ingress:
  - hostname: narve.ai
    service: http://localhost:7000
  - hostname: sports.narve.ai
    service: http://localhost:7000  # gateway multiplexes by Host
  - hostname: weather.narve.ai
    service: http://localhost:7000
  - hostname: world.narve.ai
    service: http://localhost:7000
  - hostname: crypto.narve.ai
    service: http://localhost:7000
  - hostname: midterm.narve.ai
    service: http://localhost:7000
  - hostname: traders.narve.ai
    service: http://localhost:7000
  - hostname: voters.narve.ai
    service: http://localhost:7000
  - hostname: climate.narve.ai
    service: http://localhost:7000
  - hostname: disasters.narve.ai
    service: http://localhost:7000
  - hostname: whale.narve.ai
    service: http://localhost:7000
  - hostname: cb.narve.ai
    service: http://localhost:7000
  - hostname: health.narve.ai
    service: http://localhost:7000
  - hostname: love.narve.ai
    service: http://localhost:7000
  - service: http_status:404
```

After editing: `sudo systemctl reload cloudflared` (or `cloudflared --config <path> tunnel run` if running manually).

Smoke: `curl -sI https://voters.narve.ai/` should return 200 from the gateway.

**Tasks remaining:**
- [ ] Update cloudflared config on prod
- [ ] Reload service
- [ ] Smoke each subdomain

---

## 2026-05-15 — WAF rule: protect /admin/api/* endpoints

Closes carry-over audit LOW #1: existing CF WAF rules (the 2026-04-21
pass) cover `/admin/*` rate-limiting at 60/min/IP but do **not**
specifically harden the `/admin/api/*` JSON surface, which is the
highest-value attack target on the origin (admin mutation endpoints,
job triggers, watermark trace, health monitor, etc.). Add a dedicated
custom rule + rate-limit pair to make brute-force / scraping
impossible at the edge before requests reach uvicorn.

### CF Dashboard nav path

```
narve.ai → Security → WAF → Custom rules → Create rule
```

(Rate-limit half lives one tab over: `narve.ai → Security → WAF →
Rate limiting rules → Create rule`.)

### Custom rule — managed challenge + country allowlist

- **Rule name:** `admin-api-edge-shield`
- **Expression** (copy-paste into the expression editor in "Edit
  expression" mode):

  ```
  (http.request.uri.path matches "^/admin/api/")
  ```

- **Action — pick one of the two postures below depending on how
  locked-down you want it.** Posture B is stricter; default to A if
  unsure since you sometimes admin from random networks (phone tether,
  hotel wifi, etc.).

  **Posture A — Managed Challenge (default, recommended):**
  - Action: `Managed Challenge`
  - Lets a real human through after a JS challenge; blocks
    headless / scripted abuse cold. No country pinning, so admin
    works from anywhere.

  **Posture B — Block by country (stricter, only if admin is
  geo-stable):**
  - Expression (extends the path match):

    ```
    (http.request.uri.path matches "^/admin/api/"
      and not ip.geoip.country in {"GB" "US"})
    ```

  - Action: `Block`
  - Allowlist GB + US only (adjust to wherever you actually admin
    from — `DE`, `FR`, etc.). Any other origin gets a hard 403 at
    the edge.

### Rate-limit rule — pair with the custom rule above

Second tab: `narve.ai → Security → WAF → Rate limiting rules → Create
rule`.

- **Rule name:** `admin-api-rate-limit`
- **Match expression:**

  ```
  (http.request.uri.path matches "^/admin/api/")
  ```

- **Characteristics:** `IP source address`
- **Period:** `5 minutes`
- **Requests allowed:** `60`
- **When exceeded:** `Block` for `10 minutes`
- **Description:** `admin-api-rate-limit — 60 req / 5 min per IP`

This is **tighter** than the existing Rule E (`/admin/*` at 60/min/IP
= 300/5min/IP) because `/admin/api/*` is the JSON mutation surface,
not the HTML console. Legit admin usage is bursty-but-low (login,
load page, click a few buttons → ~10-20 reqs in a minute, then idle);
60 / 5min is generous for humans, fatal for scrapers.

### Justification

The admin API is the single highest-value target on the origin:
- `POST /admin/api/jobs/*` triggers background jobs (rate-limited
  app-side at 30-120/min/admin per `admin_jobs_routes.py:85-145`, but
  edge limit adds defence in depth).
- `GET /admin/api/health-monitor` enumerates all 13 services
  (`admin_health_monitor_routes.py`) — a recon goldmine for an
  attacker mapping the surface.
- `GET /admin/trace-watermark?id=<wm>` (`admin_routes.py:712`)
  reverse-looks-up per-recipient email watermarks; every hit is
  audit-logged, but the audit only fires *after* the request reaches
  uvicorn.

App-side, `_require_admin_user` returns 403 on missing session, so
functional auth is fine. But:
1. **Brute-force / credential-stuffing** against any future
   `/admin/api/login`-style endpoint would burn uvicorn cycles for
   every guess.
2. **Scraping** the JSON endpoints for shape/error-message
   reconnaissance is trivially scriptable from a single IP.
3. **DoS amplification** — an attacker can issue thousands of
   `/admin/api/*` requests, each of which forces an admin-session
   lookup against SQLite before 403ing. Edge block = origin saved.

The managed-challenge posture (A) stops headless bots cold without
locking out the human; the rate-limit pair caps even an authenticated
session if it goes rogue (compromised admin laptop). Both rules cost
~zero on Cloudflare's Pro plan.

### Deploy order

1. Add the rate-limit rule first (rate-limit page) — it's
   purely additive, can't lock you out.
2. Smoke `/admin/api/health-monitor` from your normal IP to confirm
   nothing breaks at 60 req / 5min.
3. Add the custom rule (Posture A by default).
4. Smoke again — managed challenge should fire once per session for
   a real browser, then cookie-bypass subsequent requests.
5. If switching to Posture B later: confirm your current public IP's
   country **before** flipping action to Block (use
   <https://ifconfig.co/country-iso>).

### Tasks remaining

- [ ] Add rate-limit rule via CF dashboard (narve.ai → Security →
      WAF → Rate limiting rules → Create rule)
- [ ] Add custom rule, Posture A (narve.ai → Security → WAF →
      Custom rules → Create rule)
- [ ] Smoke `/admin/api/health-monitor` and `/admin/api/jobs/*` from
      a real browser session after both rules are live
- [ ] (Optional) Upgrade to Posture B once a stable admin-country
      set is confirmed
