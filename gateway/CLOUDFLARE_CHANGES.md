# Cloudflare DNS & Email Runbook — narve.ai

> **Status:** documentation-only runbook. Cloudflare MCP was not available in
> the build environment, so every change below must be applied manually via
> the Cloudflare dashboard or `cloudflared` CLI. Re-run this runbook each time
> you rotate keys or change providers.

## Pre-flight checks (do these first)

Before touching DNS, log in at https://dash.cloudflare.com and verify:

1. `narve.ai` appears under **Websites** and is **Active**.
2. **Email → Email Routing** is enabled (Cloudflare will offer to add MX records automatically).
3. You have API token with `Zone.DNS:Edit` + `Zone.Email Routing Rules:Edit` scopes if you want to script future changes.
4. The existing Cloudflare Tunnel (`habbig.com` → your Tailscale server) is untouched — this runbook only adds **new** records; no deletions.

## Records to add

All records are at the apex (`narve.ai`). TTL is set to 300 (5 min) so DKIM
and SPF can be rotated quickly if a provider is swapped.

### 1. MX — inbound routing via Cloudflare Email Routing

Cloudflare Email Routing adds MX records automatically when you enable it. If
they are missing, add them manually:

| Type | Name | Content | Priority | TTL | Proxy |
|------|------|---------|----------|-----|-------|
| MX | `@` | `route1.mx.cloudflare.net` | 74 | 300 | DNS only |
| MX | `@` | `route2.mx.cloudflare.net` | 18 | 300 | DNS only |
| MX | `@` | `route3.mx.cloudflare.net` | 2  | 300 | DNS only |

**Reason:** lets Cloudflare receive mail at `*@narve.ai` so the routing rules
below can forward to your personal inbox. Proxy must be **DNS only** — MX is
not a proxied record type.

### 2. SPF — authorises outbound senders

Cloudflare Email Routing **does not send outbound mail**. You need a separate
SPF include for whichever outbound provider you pick. The default below covers
MailChannels (the recommended path for a Cloudflare-native stack). If you pick
Resend / Postmark / SES, swap the `include:` clause for the one they publish.

| Type | Name | Content | TTL | Proxy |
|------|------|---------|-----|-------|
| TXT | `@` | `v=spf1 include:relay.mailchannels.net include:_spf.mx.cloudflare.net ~all` | 300 | DNS only |

**Reason:** receivers use SPF to verify outbound mail originates from an
authorised relay. Cloudflare's include handles bounce replies that come back
through Email Routing; MailChannels handles new outbound sends.

**If replacing an existing SPF record:** do not create a second TXT — merge
the includes into one record. Multiple SPF TXTs is an RFC violation.

### 3. DKIM — outbound provider's public key

The exact record comes from your outbound provider. Typical providers:

- **MailChannels via Cloudflare Worker:** records are auto-published at
  `mailchannels._domainkey` when you deploy the worker. Follow
  https://support.mailchannels.com/hc/en-us/articles/7680240933261.
- **Resend:** dashboard → Domains → narve.ai → shows three CNAME records
  that proxy to `resend.com`. Add them as CNAMEs with proxy OFF.
- **Postmark / SES:** dashboard gives one or more `_domainkey` TXT records.

| Type | Name | Content | TTL | Proxy |
|------|------|---------|-----|-------|
| TXT | `<selector>._domainkey` | *(provider's public key, base64 wrapped)* | 300 | DNS only |

**Reason:** DKIM signs every outbound message. Without it, Gmail and Outlook
will quarantine or reject.

### 4. DMARC — tells receivers what to do with failures

| Type | Name | Content | TTL | Proxy |
|------|------|---------|-----|-------|
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; pct=100; rua=mailto:dmarc@narve.ai; ruf=mailto:dmarc@narve.ai; fo=1; adkim=r; aspf=r` | 300 | DNS only |

**Reason:** `p=quarantine` tells receivers to sandbox mail that fails both SPF
and DKIM (not delete — quarantine is safer for a new setup). `rua` and `ruf`
send aggregate and forensic reports to `dmarc@narve.ai`, which is routed to
your inbox via the rule in Step 5.

Once DMARC reports have been clean for ~2 weeks, tighten to `p=reject`.

### 5. Email Routing rules

At **Email → Email Routing → Routing Rules**, add these **Custom Address**
routes. Set the destination to whichever personal inbox you want notifications
in — for narve.ai we use `shocakarel@gmail.com` by default.

| From (on `narve.ai`) | To (personal) | Reason |
|---|---|---|
| `support@` | `shocakarel@gmail.com` | Support tickets from `/support` form |
| `legal@` | `shocakarel@gmail.com` | Legal / DMCA replies (rendered on `/terms`) |
| `privacy@` | `shocakarel@gmail.com` | GDPR requests (rendered on `/privacy`) |
| `feedback@` | `shocakarel@gmail.com` | Feedback widget notifications |
| `noreply@` | `shocakarel@gmail.com` | Bounce handling for transactional mail |
| `dmarc@` | `shocakarel@gmail.com` | Aggregate DMARC reports |

Also add a **catch-all** rule: `*@narve.ai → shocakarel@gmail.com` so any typo
or new alias still lands in your inbox.

## Outbound sending — MailChannels via Cloudflare Worker

**Important:** MailChannels closed their free public Cloudflare Worker relay
in August 2024. You now need a domain-lockdown record to enable sending.

### Step 1 — add the domain-lockdown TXT

This tells MailChannels "only this Cloudflare account may send as narve.ai":

| Type | Name | Content | TTL |
|------|------|---------|-----|
| TXT | `_mailchannels` | `v=mc1 cfid=YOUR_CLOUDFLARE_ACCOUNT_ID.workers.dev` | 300 |

Find your Cloudflare account ID at **Workers & Pages → Overview** (sidebar).

### Step 2 — deploy the relay Worker

```bash
# In a scratch directory
mkdir narve-email-worker && cd narve-email-worker
npm init -y
npm install -D wrangler@latest
```

Create `src/index.js`:

```javascript
// Cloudflare Worker: relay SMTP-style sends to MailChannels.
// Called by the gateway's EmailService via HTTPS POST with a shared secret.
export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    const auth = request.headers.get("authorization") || "";
    if (auth !== `Bearer ${env.RELAY_SECRET}`) return new Response("Forbidden", { status: 403 });

    const payload = await request.json();
    const mcResponse = await fetch("https://api.mailchannels.net/tx/v1/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        personalizations: [{ to: [{ email: payload.to, name: payload.toName || "" }] }],
        from: { email: payload.from || "noreply@narve.ai", name: payload.fromName || "narve.ai" },
        subject: payload.subject,
        content: [
          { type: "text/plain", value: payload.text || "" },
          { type: "text/html", value: payload.html || "" },
        ],
        reply_to: payload.replyTo ? { email: payload.replyTo } : undefined,
      }),
    });
    return new Response(await mcResponse.text(), { status: mcResponse.status });
  },
};
```

Create `wrangler.toml`:

```toml
name = "narve-email-relay"
main = "src/index.js"
compatibility_date = "2024-05-01"

[vars]
# RELAY_SECRET is set via `wrangler secret put RELAY_SECRET`
```

Deploy:

```bash
npx wrangler login
npx wrangler secret put RELAY_SECRET   # paste a random 32-char secret
npx wrangler deploy
```

Note the deployed URL (e.g. `https://narve-email-relay.your-subdomain.workers.dev`).

### Step 3 — wire the gateway

In `.env`:

```bash
EMAIL_RELAY_URL=https://narve-email-relay.your-subdomain.workers.dev
EMAIL_RELAY_SECRET=<same secret as above>
```

The gateway's `email/service.py` uses `EMAIL_RELAY_URL` when set and falls
back to raw SMTP otherwise, so swapping providers is a single env-var change.

## Alternative — any SMTP provider

If MailChannels feels fragile, the `EmailService` class also supports plain
SMTP. Comment out `EMAIL_RELAY_URL` and set:

```bash
SMTP_HOST=smtp.resend.com       # or smtp.postmarkapp.com, email-smtp.eu-west-1.amazonaws.com, etc.
SMTP_PORT=587
SMTP_USER=resend
SMTP_PASSWORD=<api key>
```

Every provider publishes SPF + DKIM records — add those to Cloudflare DNS
exactly as they instruct.

## Verification

After DNS has propagated (5–10 min with TTL 300):

```bash
# SPF
dig TXT narve.ai +short | grep spf1
# Expected: "v=spf1 include:relay.mailchannels.net ... ~all"

# DMARC
dig TXT _dmarc.narve.ai +short
# Expected: "v=DMARC1; p=quarantine; ..."

# MX
dig MX narve.ai +short
# Expected: 74 route1.mx.cloudflare.net., 18 route2.mx.cloudflare.net., 2 route3...

# End-to-end
# Send yourself a test email via the admin panel's "Send test email" button
# (added in Feature 1). Confirm:
#  - It arrives
#  - Gmail header shows spf=pass, dkim=pass, dmarc=pass
```

## Change log

| Date | Change | Reason | Operator |
|------|--------|--------|----------|
| 2026-04-08 | Runbook authored — no live changes yet | Initial setup for Features 1 and 9 | Claude (no Cloudflare MCP) |
| _pending_ | Apply records in sections 1–5 | Required before SMTP works | — |
| _pending_ | Deploy MailChannels worker | Required for outbound sending | — |
| _pending_ | Tighten DMARC to `p=reject` | After 2 weeks of clean `rua` reports | — |

## Notes for future operators

- **Always propagate TTL first.** If you lower a record from TTL=3600 to TTL=300,
  wait one hour before making breaking changes so caches expire.
- **Never put two SPF TXT records on the same name.** Merge the `include:` lists.
- **Tunnel is separate.** The existing Cloudflare Tunnel (`habbig.com → tailscale`)
  is a CNAME to `.cfargotunnel.com`. Do not delete it.
- **If sending breaks:** check https://dash.cloudflare.com/.../email/routing
  first, then Sentry for SMTP errors, then the MailChannels Worker logs.


# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure changes — staging, WAF, CDN, health checks
# Added 2026-04-08. Each section is a standalone checklist. Apply via
# https://dash.cloudflare.com — I don't have Cloudflare MCP in my tool set.
# ─────────────────────────────────────────────────────────────────────────────

## 1. Staging subdomain

### 1.1 DNS
**Status:** ⬜ pending (likely already resolved via `*.narve.ai` wildcard)

Because `*.narve.ai` is a wildcard CNAME pointing at the Cloudflare Tunnel,
`staging.narve.ai` resolves automatically. **No new DNS record required.**
Confirm with:

```bash
dig +short staging.narve.ai
# Should return a Cloudflare edge IP (e.g. 172.x.x.x)
```

### 1.2 Cloudflare Tunnel ingress (ON THE SERVER)
**Status:** ⬜ pending

Add a staging-specific ingress rule to `/etc/cloudflared/config.yml` BEFORE
the `*.narve.ai` wildcard rule. Order matters — first match wins.

```bash
ssh julianhabbig@100.69.44.108
sudo nano /etc/cloudflared/config.yml
```

Current contents (as of 2026-04-08):

```yaml
tunnel: 30566a6e-8963-427e-99fa-393fa20a332e
credentials-file: /etc/cloudflared/30566a6e-8963-427e-99fa-393fa20a332e.json

ingress:
  - hostname: narve.ai
    service: http://localhost:7000
  - hostname: "*.narve.ai"
    service: http://localhost:7000
  - hostname: habbig.com
    service: http://localhost:7000
  - hostname: "*.habbig.com"
    service: http://localhost:7000
  - service: http_status:404
```

Target contents:

```yaml
tunnel: 30566a6e-8963-427e-99fa-393fa20a332e
credentials-file: /etc/cloudflared/30566a6e-8963-427e-99fa-393fa20a332e.json

ingress:
  # staging MUST come before the wildcard rule
  - hostname: staging.narve.ai
    service: http://localhost:7001

  # production
  - hostname: narve.ai
    service: http://localhost:7000
  - hostname: "*.narve.ai"
    service: http://localhost:7000

  # legacy habbig.com
  - hostname: habbig.com
    service: http://localhost:7000
  - hostname: "*.habbig.com"
    service: http://localhost:7000

  # fallback
  - service: http_status:404
```

Apply:

```bash
sudo systemctl restart cloudflared
sleep 3
systemctl is-active cloudflared
```

### 1.3 Staging Page Rule — never cache
**Status:** ⬜ pending

Dashboard → **Rules → Page Rules → Create Page Rule**

| Setting | Value |
|---|---|
| URL match | `staging.narve.ai/*` |
| Cache Level | **Bypass** |
| Disable Performance | ON |

---

## 2. Health checks

### 2.1 Production
**Status:** ⬜ pending

Dashboard → **Traffic → Health Checks → Create**

| Setting | Value |
|---|---|
| Name | `narve-production-health` |
| Hostname | `narve.ai` |
| Path | `/health` |
| Method | `GET` |
| Expected codes | `200` |
| Interval | `60` seconds |
| Retries | `2` |
| Notification email | julian.habbig@icloud.com |

### 2.2 Staging
**Status:** ⬜ pending

Same as production but:
- Hostname: `staging.narve.ai`
- Interval: `300` seconds

### 2.3 Scraper (deferred)
**Status:** ⬜ deferred — scraper not yet deployed on the server

---

## 3. CDN cache rules

Dashboard → **Caching → Cache Rules → Create rule**

### 3.1 Rule: `narve-api-no-cache` (priority 1 — must be first)
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| If incoming requests match | `(starts_with(http.request.uri.path, "/api/") or starts_with(http.request.uri.path, "/admin/") or http.request.uri.path eq "/health" or http.request.uri.path eq "/login" or http.request.uri.path eq "/signup" or starts_with(http.request.uri.path, "/forgot-password"))` |
| Then | **Cache eligibility: Bypass cache** |

### 3.2 Rule: `narve-static-long-cache` (priority 2)
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| If incoming requests match | `starts_with(http.request.uri.path, "/_gateway_static/")` |
| Then | **Cache eligibility: Eligible for cache** |
| Edge TTL | Override origin — **30 days** |
| Browser TTL | Override origin — **7 days** |
| Serve stale while revalidating | ON |

### 3.3 Speed / compression
**Status:** ⬜ pending

Dashboard → **Speed → Optimization**

| Setting | State |
|---|---|
| Brotli | ON |
| Early Hints | ON |
| HTTP/2 to Origin | ON |
| HTTP/3 (with QUIC) | ON |
| 0-RTT Connection Resumption | ON |

Dashboard → **Speed → Optimization → Content Optimization**

| Setting | State | Why |
|---|---|---|
| Auto Minify: CSS | ON | Safe — CSS is straightforward |
| Auto Minify: HTML | OFF | Server-rendered, minifier breaks `{{ key }}` templating |
| Auto Minify: JavaScript | OFF | `trade.js` uses template literals that the minifier mangles |

---

## 4. WAF rules

Dashboard → **Security → WAF → Custom rules → Create rule**

### 4.1 `narve-block-login-threats`
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| Expression | `(starts_with(http.request.uri.path, "/login") or starts_with(http.request.uri.path, "/signup") or starts_with(http.request.uri.path, "/forgot-password")) and cf.threat_score gt 10` |
| Action | **Block** |

### 4.2 `narve-auth-rate-limit`
**Status:** ⬜ pending — this is a **Rate Limiting Rule**, not a custom rule

Dashboard → **Security → WAF → Rate limiting rules → Create rule**

| Setting | Value |
|---|---|
| Expression | `starts_with(http.request.uri.path, "/login") or starts_with(http.request.uri.path, "/signup") or starts_with(http.request.uri.path, "/forgot-password")` |
| Requests per period | 20 |
| Period | 1 minute |
| Action | Block |
| Duration | 60 seconds |
| Characteristic | IP address |

### 4.3 `narve-admin-rate-limit`
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| Expression | `starts_with(http.request.uri.path, "/admin/")` |
| Requests per period | 60 |
| Period | 1 minute |
| Action | Block |
| Duration | 300 seconds |

### 4.4 `narve-block-scanners`
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| Expression | `(http.user_agent contains "sqlmap") or (http.user_agent contains "nikto") or (http.user_agent contains "nmap") or (http.user_agent contains "masscan") or (starts_with(http.request.uri.path, "/.env")) or (starts_with(http.request.uri.path, "/wp-admin")) or (starts_with(http.request.uri.path, "/wp-login")) or (starts_with(http.request.uri.path, "/phpmyadmin"))` |
| Action | **Block** |

### 4.5 `narve-block-unverified-bots`
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| Expression | `(cf.client.bot) and not (cf.verified_bot_category in {"Search Engine Crawler"})` |
| Action | **Managed Challenge** *(not Block — legitimate bots sometimes fail verification)* |

### 4.6 `narve-markets-api-limit`
**Status:** ⬜ pending

| Setting | Value |
|---|---|
| Expression | `starts_with(http.request.uri.path, "/api/markets/")` |
| Requests per period | 120 |
| Period | 1 minute |
| Action | Block |
| Duration | 60 seconds |

---

## 5. Verification after applying

```bash
# Staging resolves and responds
curl -sS https://staging.narve.ai/health

# /health is never cached
curl -sI https://narve.ai/health | grep -i cache-control
# → Cache-Control: no-store, max-age=0

# Static assets are cached long
curl -sI https://narve.ai/_gateway_static/gateway.css | grep -i cache-control
# → Cache-Control: public, max-age=2592000, immutable

# Scanner paths blocked
curl -o /dev/null -sS -w '%{http_code}\n' https://narve.ai/.env
# → 403 after rule 4.4 applied

# Auth rate limit kicks in after 20 rapid requests
for i in {1..25}; do curl -o /dev/null -sS -w '%{http_code}\n' https://narve.ai/login; done
# → last few should be 429 after rule 4.2 applied
```

---

## 6. Sub-brand subdomains

Six narve.ai sub-brand products are served from dedicated subdomains, all
proxied through Cloudflare to the same origin that already serves
`narve.ai`. Universal SSL covers every subdomain automatically. Apply
these records after the main `narve.ai` apex records are live.

### Records to add

| Type  | Name     | Content   | TTL | Proxy    |
|-------|----------|-----------|-----|----------|
| CNAME | sports   | narve.ai  | 300 | Proxied  |
| CNAME | weather  | narve.ai  | 300 | Proxied  |
| CNAME | world    | narve.ai  | 300 | Proxied  |
| CNAME | crypto   | narve.ai  | 300 | Proxied  |
| CNAME | midterm  | narve.ai  | 300 | Proxied  |
| CNAME | traders  | narve.ai  | 300 | Proxied  |

Each `<slug>.narve.ai` points to the same origin as `narve.ai`. The gateway
detects the sub-brand from the Host header (`get_subdomain` in server.py)
and serves the correct landing / proxied dashboard without any extra
routing on the Cloudflare side.

### Cloudflare Tunnel alternative

If you tunnel instead of CNAME-to-apex, add each subdomain as a hostname
on the existing `cloudflared` tunnel:

```bash
cloudflared tunnel route dns <TUNNEL_UUID> sports.narve.ai
cloudflared tunnel route dns <TUNNEL_UUID> weather.narve.ai
cloudflared tunnel route dns <TUNNEL_UUID> world.narve.ai
cloudflared tunnel route dns <TUNNEL_UUID> crypto.narve.ai
cloudflared tunnel route dns <TUNNEL_UUID> midterm.narve.ai
cloudflared tunnel route dns <TUNNEL_UUID> traders.narve.ai
```

`TUNNEL_UUID` is the one currently serving `narve.ai`. Confirm with
`cloudflared tunnel list` on the Ubuntu server (100.69.44.108). Restart
the tunnel service after adding the routes.

### Verification after applying

```bash
# Each subdomain resolves to Cloudflare (proxied → 104.x / 172.x).
for s in sports weather world crypto midterm traders; do
  dig +short "${s}.narve.ai" | head -n 1
done

# Landing page renders with the correct wordmark.
for s in sports weather world crypto midterm traders; do
  echo "=== ${s} ==="
  curl -sS "https://${s}.narve.ai/" | grep -o 'narve.ai / [a-z]*' | head -n 1
done

# Each subdomain has its own sitemap / robots.
curl -sS https://sports.narve.ai/sitemap.xml | grep '<loc>'
curl -sS https://sports.narve.ai/robots.txt
```

### Rollback

Delete the six CNAME records from the Cloudflare dashboard, or remove the
tunnel hostnames with `cloudflared tunnel route dns --delete`. No origin
changes are needed — the subdomains silently stop resolving.

---

## 7. Change log

| Date | Who | Section | Change |
|---|---|---|---|
| 2026-04-08 | AI | File | Added infrastructure section |
| 2026-04-21 | AI | §6 | Added six sub-brand subdomain CNAMEs (sports, weather, world, crypto, midterm, traders) |
