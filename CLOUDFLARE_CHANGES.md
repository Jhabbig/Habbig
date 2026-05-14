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
