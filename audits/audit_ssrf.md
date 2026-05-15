# SSRF Audit — `gateway/`

Audit of every server-side HTTP fetch in `gateway/` reachable from user input.
Date: 2026-05-15.

Search basis:

```
grep -rn "httpx.get|httpx.post|requests.get|requests.post|urlopen" gateway/ --include='*.py'
```

…plus the wider set of `httpx.AsyncClient(...).get/post/put/delete/patch/head/request`
call patterns (the original grep alone misses every `httpx.AsyncClient` instance
which is how the codebase actually does it).

## Summary

| Metric | Count |
|---|---|
| Total fetch call sites (non-test) | 38 |
| Sites where URL or path segment touches user input | 4 |
| Confirmed exploitable SSRF (host fully attacker-controlled) | 0 |
| Conditional / partial SSRF risk (DNS rebinding, dev-only) | 1 |
| Low-confidence host-bounded path injection | 2 |
| Not user-reachable / static URL / env-only | 33 |

## Methodology

For each call site I traced the URL back to its origin and classified it:

- **STATIC** — URL is a module constant or env var, no user input flows into it.
- **HOST-BOUNDED** — Host comes from env/constant; path or query carries
  user-controlled tokens but cannot redirect the request to a different host
  (httpx doesn't let path segments override the URL authority).
- **DNS-BOUNDED** — Host is validated against a deny-list at registration time,
  but the validator is regex-on-hostname (no DNS resolution) so a benign-looking
  hostname can resolve to 127.0.0.1 / RFC1918 / metadata at request time
  (DNS rebinding).
- **OPEN** — Host fully attacker-controlled.

No call site falls into the **OPEN** category. The most exploitable one is
`webhooks.py:_deliver_once` → **DNS-BOUNDED** in production, **OPEN in dev**.

---

## Per-site findings

### 1. `gateway/server.py:7946` — internal dashboard proxy

```python
upstream = await HTTP_CLIENT.request(
    request.method,
    upstream_url,  # = f"http://127.0.0.1:{target_port}{path}"
    ...
)
```

- URL host: hardcoded `127.0.0.1`.
- Port: `target_port = dash_cfg["target"]` from the static `DASHBOARDS` dict
  keyed by the matched route. `key` is whitelisted before lookup
  (`server.py:7866`).
- Path/query: copied from `request.url.path` / `request.url.query`, but the
  authority is fixed by the f-string so a path like `/foo/../../bar` cannot
  change host.

**Classification: STATIC.** Not SSRF. This is an intentional reverse proxy.

### 2. `gateway/server.py:3105` — admin subproducts probe

```python
with _ur.urlopen(target, timeout=1.0) as r:  # nosec
```

- `target = cfg.get("upstream")` for each `slug, cfg in DASHBOARDS.items()`.
- `DASHBOARDS` is a module-level dict populated from `config.json` /
  `subproduct.py` — not from request input.
- Reachable only from `_check_subproducts()` (admin "deep" health probe).

**Classification: STATIC.** Not user-controllable.

### 3. `gateway/admin_integrations_routes.py:122,150,165` — admin integration probes

```python
async with httpx.AsyncClient(timeout=5.0) as cli:
    r = await cli.get("https://api.stripe.com/v1/balance", ...)   # L122
    r = await cli.head(url)                                       # L150
    r = await cli.get(url)                                        # L165
```

- L122: literal `https://api.stripe.com/v1/balance`.
- L143: `_test_cloudflare` calls `_http_head("http://localhost:7000/health")`
  (literal).
- L99/108: `_test_polymarket` / `_test_kalshi` build URLs from
  `POLYMARKET_API_BASE` / `KALSHI_API_BASE` env vars.
- All callers go through `_TESTERS` dict keyed by `slug`; `slug` is matched
  against five known integrations (`anthropic`, `polymarket`, `kalshi`,
  `stripe`, `cloudflare`) and returns 400 otherwise. Route is also gated by
  `_require_admin_user` + CSRF middleware.

**Classification: STATIC.** Even an admin cannot inject an arbitrary URL.

### 4. `gateway/admin_health_monitor_routes.py:106` — health pinger

```python
url = f"http://localhost:{port}/health"
resp = client.head(url, timeout=2.0)
```

- `port` comes from the hardcoded `SERVICES` list (14 ports, slug→port).
- No request input flows in.

**Classification: STATIC.**

### 5. `gateway/webhooks.py:184` — outbound webhook delivery (**HIGHEST RISK**)

```python
async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
    resp = await client.post(url, content=body_bytes, headers=headers)
```

- `url` is `sub["url"]` — the URL the user (or admin acting on their behalf)
  saved at `POST /settings/webhooks`. User input.
- The URL is validated once at creation by
  `webhooks_routes._validate_url` (`gateway/webhooks_routes.py:75-114`):
  - http/https scheme required.
  - In production (`PRODUCTION=1`) the host is rejected if a regex matches any
    of: `localhost`, `127.*`, `0.0.0.0`, `169.254.*`, `10.*`, `192.168.*`,
    `172.(16-31).*`, IPv6 loopback, IPv6 ULA prefix.
  - In production, plain `http://` is also rejected.

Issues with this control:

  1. **Dev / non-production has NO SSRF guard.** The `_os.environ.get("PRODUCTION", "0") == "1"` gate
     wraps the entire deny-list loop, so on any machine where `PRODUCTION` is
     unset a user can register `http://127.0.0.1:7000/admin/...` or
     `http://localhost:8888/...` and have the gateway POST signed payloads
     into its own internal services. This includes the dashboard ports listed
     in `admin_health_monitor_routes.SERVICES` and the SQLite-admin endpoints.
     Severity in dev: low (no production data). Severity if this code ever
     runs without `PRODUCTION=1` set: high.
  2. **DNS rebinding.** The deny-list is a regex on `parsed.hostname`. A user
     can register `http://attacker.example.com/x` where `attacker.example.com`
     resolves to a public IP at validation time and to `169.254.169.254`
     (AWS IMDSv1) or `127.0.0.1` when `_deliver_once` finally connects. httpx
     respects the OS resolver; nothing pins the IP at validation time. AWS
     IMDSv2 is token-protected so v2 is safe, but v1 metadata is still
     exposed on EC2 instances that haven't disabled it, and other intranet
     services (Redis admin, Prometheus, etc.) generally have no token.
  3. **No port allowlist.** Users can target any port (e.g. `:6379` Redis,
     `:11211` memcached). Even if the response is a 4xx the connection
     itself may have side-effects (e.g. Redis CONFIG SET if HTTP request
     smuggling against an inline-protocol server works).
  4. **Missing IPv6 link-local.** `^\[?f[cd][0-9a-f]{2}:` blocks IPv6 ULA but
     not `fe80::/10` link-local or `::ffff:127.0.0.1` IPv4-mapped IPv6.
  5. **Missing 100.64.0.0/10** (carrier-grade NAT, used by some clouds).
  6. **POST with signed body.** The `X-Narve-Signature` is computed from the
     user's own secret, so a self-targeted SSRF doesn't grant the attacker
     forged credentials — but it does coerce the gateway into emitting
     traffic the attacker can read in a log + the response status code is
     surfaced in the admin DLQ (`webhook_dead_letter` table) and visible to
     the owner, leaking internal HTTP behaviour.

**Classification: DNS-BOUNDED in production, OPEN in dev.** The
hardening is partial. Recommend: (a) drop the `PRODUCTION` gate so the
validator always runs, (b) resolve the hostname at validation time AND
re-resolve and assert non-private at connect time (or use httpx's
`transport` hook with a custom resolver that pins the resolved IP), and
(c) add a per-deployment scheme/port allowlist.

### 6. `gateway/email_system/service.py:154` — MailChannels relay

```python
resp = await client.post(self.relay_url, json=body, headers=headers)
```

- `self.relay_url = os.environ.get("EMAIL_RELAY_URL", "")`.

**Classification: STATIC.**

### 7. `gateway/scraper/transmission/pusher.py:99` — scraper → main server push

```python
url = f"{MAIN_SERVER_URL}/api/scraper/ingest"
resp = await client.post(url, json=payload, headers=_auth_headers())
```

- `MAIN_SERVER_URL` is a module-level env var (`os.environ["MAIN_SERVER_URL"]`).
- `payload` includes scraper-supplied content (post data), not URLs.

**Classification: STATIC.**

### 8. `gateway/scraper/scrapers/substack.py:131,160` — Substack RSS

```python
resp = await client.get(feed_url)
```

- `feed_url` iterated from `_get_feeds()` which reads `SUBSTACK_FEEDS` env var
  (or built-in constants).

**Classification: STATIC.**

### 9. `gateway/scraper/scrapers/truthsocial.py:133,143,162` — TruthSocial

```python
resp = await client.get(f"{API_URL}/accounts/lookup", params={"acct": handle})
resp = await client.get(f"{API_URL}/accounts/{account_id}/statuses", ...)
resp = await client.get(f"{API_URL}/timelines/tag/{tag}", ...)
```

- `API_URL = f"{BASE_URL}/api/v1"` — constant.
- `handle` iterates from `TRUTHSOCIAL_PROMINENT_ACCOUNTS` config constant.
- `tag` is a keyword from scraper config.
- `account_id` is returned by the lookup endpoint, so it's data from
  truthsocial.com itself (not user input). A compromised TruthSocial could
  return an `id` containing `../evil.com/x` — the f-string interpolation
  would expand to `https://truthsocial.com/api/v1/accounts/../evil.com/x/statuses`.
  httpx preserves the path but does not change the host. **Low-confidence
  path-injection** that cannot reach a different host.

**Classification: STATIC (host-bounded).**

### 10. `gateway/scraper/scrapers/metaculus.py:50,125` — Metaculus

```python
resp = await client.get(f"{API_BASE}/questions/", params={"search": kw, ...})
```

- `API_BASE` constant; `kw` is a config keyword, not user input.

**Classification: STATIC.**

### 11. `gateway/observability/sentry_api.py:123` — Sentry issues

```python
url = f"https://sentry.io/api/0/projects/{org}/{project}/issues/"
resp = await client.get(url, headers=headers, params=params)
```

- `org`/`project` from env vars.

**Classification: STATIC.**

### 12. `gateway/jobs/telegram_sends.py:58` — Telegram bot

```python
resp = await client.post(f"{_TG_API}/bot{token}/sendMessage", json=payload)
```

- `_TG_API` constant; `token` from env. `chat_id` is from DB (set when user
  links their Telegram). Not in URL host/path beyond `/bot{token}/`.

**Classification: STATIC.**

### 13. `gateway/jobs/pipeline_jobs.py:31` — scraper kick-off

```python
resp = await client.post(f"{scraper_url.rstrip('/')}/pull", headers=...)
```

- `scraper_url = os.environ["SCRAPER_URL"]`.

**Classification: STATIC.**

### 14. `gateway/status_system/probes.py:164` — scraper health probe

```python
resp = await client.get(f"{base}/health", headers=headers)
```

- `base = os.environ.get("SCRAPER_URL", "http://localhost:8001")`.

**Classification: STATIC.**

### 15. `gateway/insider/lobbying.py:38` — Senate LDA

```python
resp = await client.get(LDA_URL, params={...})
```

- `LDA_URL = "https://lda.senate.gov/api/v1/filings/"` constant.

**Classification: STATIC.**

### 16. `gateway/insider/sec_form4.py:117,140` — SEC EDGAR

```python
resp = await client.get(url)       # url = SUBMISSIONS_URL_TEMPLATE.format(cik=...)
resp = await client.get("https://www.sec.gov/files/company_tickers.json", ...)
```

- `cik` is looked up from a ticker that comes from a config list. Even with
  user input, format is `^\d+$` and the template fixes the host.

**Classification: STATIC.**

### 17. `gateway/insider/sec_form13f.py:100` — SEC EDGAR

```python
url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
resp = await client.get(url)
```

- `cik` from `MONITORED_13F_CIKS` env var.

**Classification: STATIC.**

### 18. `gateway/insider/congressional_trades.py:44` — capitoltrades

```python
resp = await client.get(CAPITOL_TRADES_URL, params={"pageSize": ...})
```

**Classification: STATIC.**

### 19. `gateway/insider/unusual_options.py:44` — Unusual Whales

```python
resp = await client.get(UNUSUAL_WHALES_URL, params={"limit": ...})
```

**Classification: STATIC.**

### 20. `gateway/insider/fec_campaign.py:48` — FEC

```python
async with httpx.AsyncClient(base_url=FEC_BASE, ...) as client:
    resp = await client.get("/schedules/schedule_a/", params={...})
```

**Classification: STATIC.**

### 21. `gateway/portfolio/polymarket.py:130,177` — Polymarket positions / Gamma

```python
url = f"{_api_base()}/positions"
resp = await client.get(url, params={"address": wallet_address})
...
base = f"{_gamma_base()}/markets"
resp = await client.get(base, params={"id": ",".join(chunk)})
```

- `_api_base()` / `_gamma_base()` from env, default `clob.polymarket.com` /
  `gamma-api.polymarket.com`.
- `wallet_address` enters via routes in `portfolio/routes.py` and is
  validated by `is_valid_address` (`_ADDRESS_RE = ^0x[0-9a-fA-F]{40}$`).
- `chunk` is a list of market IDs (Polymarket-internal identifiers).

**Classification: STATIC.**

### 22. `gateway/portfolio/kalshi.py:82,135` — Kalshi auth + positions

```python
resp = await client.post(f"{_api_base()}/login", json={"email": ..., "password": ...})
resp = await client.get(f"{_api_base()}/portfolio/positions", headers=...)
```

- `_api_base()` from env. Email/password are body, not URL.

**Classification: STATIC.**

### 23. `gateway/backend/markets/kalshi_client.py:190,231,283,310,329,350,409`
     `gateway/backend/markets/polymarket_client.py:79,105,118,143,163,187`

Multiple call sites of the form:

```python
resp = await client.get(f"{self.base_url}/markets/{ticker}", ...)
resp = await client.get(f"{self.gamma_base}/markets/{slug}")
```

- Host (`self.base_url` / `self.gamma_base` / `self.clob_base`) is set from
  module constants `KALSHI_API_BASE` / `GAMMA_API` / `CLOB_API` at client
  construction.
- `ticker` / `slug` reach these via `unified_markets.fetch_single_market`,
  which is reachable from user-facing routes (e.g.
  `intelligence_routes.api_market_environmental` at L227, `market_routes.py`
  at L398, L874, L989, `extension_routes.py:248`).
- `market_id` is split on the `poly:` / `kalshi:` prefix; everything after
  the colon is dropped straight into the path.
- No validation on slug/ticker characters. A user-supplied
  `market_id=kalshi:%2F..%2Fadmin` becomes
  `https://trading-api.kalshi.com/.../markets/%2F..%2Fadmin`. httpx will
  URL-encode but preserve the path under the configured host. **Cannot
  reach a different host** because the authority is fixed by the f-string,
  but **can probe arbitrary paths on kalshi.com / polymarket.com**.

**Classification: HOST-BOUNDED path injection (low-confidence).** Worth
adding a `^[A-Za-z0-9_-]{1,128}$` regex on `slug`/`ticker` before the
fetch — same shape as the existing `is_valid_address` guard for wallets.

### 24. `gateway/external_forecasts/silver_bulletin.py:68`
     `gateway/external_forecasts/fivethirtyeight.py:78`
     `gateway/external_forecasts/manifold.py:44`
     `gateway/external_forecasts/metaculus.py:49`

All four iterate over module-level `_SOURCE_PAGES` tuples or use a constant
`_BASE` (e.g. `https://api.manifold.markets/v0`). Search term passed as
query param to manifold/metaculus comes from the market's
`market.question` text from our own DB; not directly user-supplied (markets
are scraped from Polymarket/Kalshi, then their question text is reused as a
search query for benchmarking).

**Classification: STATIC.**

### 25. `gateway/scripts/benchmark_endpoints.py:79` — CLI script

```python
with urllib.request.urlopen(req, timeout=timeout) as resp:
```

- Standalone benchmark CLI; URLs come from CLI args, never network input.

**Classification: STATIC (not a server route).**

---

## Top 3 highest-risk sites

1. **`gateway/webhooks.py:184` (`_deliver_once`)** — DNS-bounded in prod,
   OPEN in dev. User-registered URL is POSTed to with signed body. Mitigated
   in prod by the regex deny-list in `webhooks_routes._validate_url`, but
   subject to DNS rebinding (no IP pinning), and the entire guard is gated
   behind `PRODUCTION=1` so dev environments are exposed. Also missing IPv6
   link-local, IPv4-mapped IPv6, port allowlist, and 100.64.0.0/10.
2. **`gateway/backend/markets/kalshi_client.py:284` (`get_market`)** —
   Host-bounded path injection. User-supplied `market_id=kalshi:<anything>`
   gets concatenated into the request path without validation. Cannot reach
   a different host (httpx fixes the authority), but can scan arbitrary
   paths under `trading-api.kalshi.com`. Low impact; cheap to fix with a
   slug regex.
3. **`gateway/backend/markets/polymarket_client.py:105` (`get_market`)** —
   Same shape as #2 for `polymarket.com`.

Everything else is either statically-rooted at a fixed host (`api.stripe.com`,
`sentry.io`, `sec.gov`, `manifold.markets`, etc.), or behind an env var the
user can't influence (`SCRAPER_URL`, `EMAIL_RELAY_URL`, `MAIN_SERVER_URL`),
or in admin-only test handlers wired to a fixed set of integration slugs.

## Recommendations (no code changes made — this is an audit)

- **webhooks**: drop the `PRODUCTION` gate around `_validate_url`'s
  deny-list so dev is protected too; resolve the hostname at validation and
  again at request time, refusing if either resolution lands in a private
  range; consider a `connect_to=...` override on httpx to pin the resolved
  IP; add an explicit `allowed_ports = {80, 443}`.
- **market clients**: regex-validate `slug`/`ticker` in
  `unified_markets.fetch_single_market` before passing to the client.
- **leave alone**: every other site in this audit.
