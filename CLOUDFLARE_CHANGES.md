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
