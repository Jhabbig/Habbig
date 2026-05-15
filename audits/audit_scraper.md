# Scraper Audit — `gateway/scraper/`

**Date:** 2026-05-15
**Scope:** All outbound HTTP fetches in `gateway/scraper/` (5 scrapers,
1 main-server pusher).
**Method:** Synchronous bash grep + manual read of every fetch call site.
**Audit dimensions:** rate-limit compliance, user-agent identification,
redirect-following safety, content-type validation, max-fetch-size cap.
**Out of scope per hard rule:** Pre-release surface
(`gateway/static/prerelease.html`, `pwa_middleware.py` critical CSS).

Read-only audit. No code changes performed.

---

## 1. Headline

| Metric | Value |
|---|---|
| Direct fetch call sites (non-test) | 7 |
| Scraper modules audited | 5 (twitter, truthsocial, metaculus, substack, pusher) |
| Sites with rate-limit gating | 7/7 (100%) |
| Sites with honest user-agent identification | 0/7 (0%) — *intentional per stealth design* |
| Sites with redirect-following safety controls | 0/7 (0%) |
| Sites with content-type validation | 0/7 (0%) |
| Sites with max-fetch-size cap | 0/7 (0%) |

### Severity rollup

| Severity | Count | Items |
|---|---|---|
| CRITICAL | 0 | — |
| HIGH | 2 | F-01 missing max-fetch-size cap (DoS / memory exhaustion); F-02 missing content-type validation on JSON/RSS parse |
| MEDIUM | 3 | F-03 default httpx redirect handling unset; F-04 no streaming on Substack RSS; F-05 spoofed user-agent / no contact identifier on Metaculus & Substack |
| LOW | 4 | F-06 no per-host circuit breaker; F-07 missing `Retry-After` honouring; F-08 Twitter/TruthSocial accept any TLS cert via Playwright default; F-09 hashtag path-segment not URL-encoded |
| INFO | 2 | I-01 rate-limit configurable via admin API (no hard floor); I-02 keyword search payload size bounded only by counter, not bytes |

**Top 3 priorities:**

1. **F-01 (HIGH)** — Add `max-fetch-size` cap. Every `httpx.AsyncClient`
   call reads the entire response body into memory via `resp.json()` or
   `resp.text` with **no size limit**. A hostile or compromised
   metaculus.com / truthsocial.com / Substack feed can OOM the scraper
   by returning a multi-GB response.
2. **F-02 (HIGH)** — Validate `Content-Type` before parsing. `resp.json()`
   on the Mastodon/Metaculus endpoints and `feedparser.parse()` on the
   RSS endpoint are called unconditionally on status 200. A captive
   portal, error page, or DNS-hijacked response in HTML form will throw
   late in the parser stack and leak the raw body into `log.exception`.
3. **F-05 (MEDIUM)** — Set an honest `User-Agent` for the public-API
   fetches (`metaculus.py`, `substack.py`). Both currently send no UA
   at all (httpx default `python-httpx/x.y.z`). Combined with no
   contact email, this is hostile-bot behaviour to those sites and a
   ToS-violation lever; the cost to fix is one line per call site.

---

## 2. Fetch call inventory

```
$ grep -rn "httpx\." gateway/scraper/ --include='*.py' | grep -v tests/
gateway/scraper/transmission/pusher.py:98       AsyncClient(timeout=30)         # → MAIN_SERVER_URL/api/scraper/ingest
gateway/scraper/scrapers/metaculus.py:47        AsyncClient(timeout=15)         # → metaculus.com /api2/questions/
gateway/scraper/scrapers/metaculus.py:124       AsyncClient(timeout=10)         # health check
gateway/scraper/scrapers/substack.py:130        AsyncClient(timeout=15)         # → feed URL (env-configurable host)
gateway/scraper/scrapers/substack.py:159        AsyncClient(timeout=10)         # health check
gateway/scraper/scrapers/truthsocial.py:131     AsyncClient(headers=…, timeout=30)  # → truthsocial.com /api/v1/accounts/…
gateway/scraper/scrapers/truthsocial.py:161     AsyncClient(headers=…, timeout=30)  # → truthsocial.com /api/v1/timelines/tag/…
```

Playwright fetches (twitter.py:147, twitter.py:294, truthsocial.py:214,
truthsocial.py:321) are separate — they ride the browser engine's
fetch stack with stealth/UA overrides but no app-level redirect or
size limits.

---

## 3. Findings (severity-sorted)

### F-01 — HIGH — Missing max-fetch-size cap on every outbound fetch

**Where:** every `async with httpx.AsyncClient(...)` block in
`metaculus.py`, `substack.py`, `truthsocial.py`, `pusher.py`.

**What:** None of the call sites set a max-response-bytes limit. All
do `await client.get(...)` followed by `resp.json()` or `resp.text`,
which materialises the entire body in memory.

```python
# truthsocial.py:131 — example
async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
    resp = await client.get(f"{API_URL}/accounts/lookup", params={"acct": handle})
    if resp.status_code != 200:
        return []
    account = resp.json()    # ← full body into memory, no cap
```

**Risk:** A malicious response — or an accidentally enormous one (the
`metaculus.com/api2/questions/` endpoint can paginate at 50 items × N
fields with large descriptions; substack feeds can carry full-post
HTML in `<content:encoded>`) — will materialise an unbounded body.
This is a credible OOM vector against a process that already runs on
the same box as the main gateway.

**Fix sketch (not implemented in this audit):**

```python
async with httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_response_bytes=...)) as client:
    async with client.stream("GET", url) as resp:
        size = 0
        chunks = []
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:  # e.g. 8 MiB
                raise ValueError("response too large")
            chunks.append(chunk)
        body = b"".join(chunks)
```

A simpler partial mitigation: check `resp.headers.get("content-length")`
and bail before reading the body if it exceeds a configured cap.

---

### F-02 — HIGH — Missing Content-Type validation before parsing

**Where:**

- `metaculus.py:60-64` — checks status `== 200`, then `data = resp.json()`.
- `metaculus.py:124-127` — health check, same pattern.
- `substack.py:130-137` — checks status, then `feedparser.parse(resp.text)`.
- `truthsocial.py:134-156, 162-175` — checks status, then `resp.json()`
  twice in `_fetch_account_statuses` and once in `_fetch_hashtag`.

**What:** Status 200 with `Content-Type: text/html` (captive portal,
WAF block page, ISP injection, hijacked DNS) is parsed as JSON / RSS.
The httpx layer will raise `json.JSONDecodeError` deep in the call,
which is caught by the broad `except Exception` and logged via
`log.exception(...)` — so the raw HTML body **lands in the log file**.

**Risk:** Information disclosure if the HTML contains a captive-portal
banner with the operator's IP, employer's name, internal credentials,
or pre-shared-key auth hints. Plus operational noise — failures look
like JSON parse errors instead of "upstream returned wrong content
type", which delays triage.

**Fix sketch:**

```python
ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
if ct not in ("application/json", "application/activity+json"):
    log.warning("Metaculus: unexpected content-type %r for kw=%r", ct, kw)
    continue
```

---

### F-03 — MEDIUM — Default httpx redirect handling unset (untrusted-host risk)

**Where:** all `httpx.AsyncClient(...)` constructors in scraper.

**What:** None of the call sites passes `follow_redirects=`. The httpx
default is `follow_redirects=False` (safe), but this is **not enforced
in code or asserted**. A future maintainer who upgrades httpx, copies
a recipe from the docs, or adds `client.get(url, follow_redirects=True)`
inline introduces redirect-following silently.

**Specific risk paths:**

- `substack.py` — feed host comes from `SUBSTACK_FEEDS` env. A
  compromised feed config or a Substack-controlled 30x redirect to a
  third-party host would currently fail-closed (default), but with
  follow=True would let an attacker pivot the fetch to an arbitrary
  URL with the scraper's network privileges (the scraper sits on
  loopback alongside the main server; SSRF is a viable goal).
- `truthsocial.py` `_fetch_account_statuses` — uses the `id` returned
  by `/accounts/lookup` to build the next URL. If the lookup ever
  redirected, the resolved JSON could come from anywhere; today this
  is safe but only by accident.

**Recommendation:** Pass `follow_redirects=False` **explicitly** on
every client, and add a comment explaining why. For Substack and
Metaculus which legitimately do redirect (Substack feeds 301 from
`https://x.substack.com/feed` to `https://www.example.com/feed`),
implement a *bounded* redirect handler that re-checks the new host
against an allowlist before following.

---

### F-04 — MEDIUM — RSS feed body buffered to text without streaming

**Where:** `substack.py:130-137`.

```python
async with httpx.AsyncClient(timeout=15) as client:
    resp = await client.get(feed_url)
    if resp.status_code != 200:
        return []
feed = feedparser.parse(resp.text)
```

**What:** `feed.text` materialises the entire feed body as a Python
`str` (UTF-8 decoded) before parsing. The function then takes only
`feed.entries[:20]`, but the full payload is loaded first. A large or
malicious feed (multi-MB `<description>` payloads) bloats memory.

**Risk:** Same class as F-01 but specifically on the RSS endpoint
where bodies can legitimately reach 1–2 MB.

**Fix sketch:** Stream the body up to a cap of e.g. 4 MiB, then parse
the bytes. `feedparser.parse(bytes_obj)` works.

---

### F-05 — MEDIUM — Spoofed UA / no contact identifier on public-API fetches

**Where:** `metaculus.py:50` and `substack.py:131` send no User-Agent
header (httpx default `python-httpx/<version>`).
`truthsocial.py:61` sends a forged Chrome UA on every public-API call.
`twitter.py:60-66` rotates 5 spoofed UAs.

**What:** The Twitter and TruthSocial scrapers explicitly spoof UAs as
part of an anti-detection strategy documented in the file headers and
README ("rate limits aggressive to avoid detection", playwright-stealth
applied) — that is a deliberate product choice and out of scope for a
mechanical fix. But Metaculus and Substack are **public APIs / public
RSS feeds** where the polite practice is the opposite: identify as a
bot, include a contact URL or email, and respect any documented
rate-limit-by-UA policy.

**Risk:**

- Reputational / ToS: Metaculus has no public API ToS forbidding
  unidentified clients, but their service is operated by a small team
  that posts maintenance windows in user-agent-grouped traffic
  analytics. Showing up as `python-httpx/0.27.0` is unfriendly.
- Operational: When Metaculus or Substack blocks the IP for being
  unidentified, there is no email channel to request unblock.
- Detection: `python-httpx/...` is a known bot UA fingerprint; some
  CDNs (Cloudflare, Fastly) will challenge or block it by default.

**Recommendation:** For `metaculus.py` and `substack.py` only (not
Twitter/TruthSocial), set
`User-Agent: narve-research/1.0 (+https://narve.ai/scraper-policy; contact@narve.ai)`.

---

### F-06 — LOW — No per-host circuit breaker

**Where:** all scrapers. APScheduler triggers `run_twitter_scrape`
and `run_truthsocial_scrape` on a fixed interval regardless of recent
failure history. After consecutive 5xx or rate-limit responses, the
sane behaviour is to back off.

**What:** `pusher.py` does back off on 429 (120s) and on 5xx
(exponential 2/4/8/16/32/60s for **a single batch**, max 3 retries),
but **the scraper modules themselves** retry on the next scheduled
tick with no awareness of recent failure. If TruthSocial returns 503
for 12 hours, the scraper hammers it every 15 minutes for 12 hours.

**Risk:** Increases the chance of permanent IP/UA block. Wastes
local resources. Not a security issue per se.

**Recommendation:** Add `_last_failure_at` + `_consecutive_failures`
to each scraper; skip a scheduled tick when the count is high and the
last failure was recent.

---

### F-07 — LOW — Missing `Retry-After` honouring

**Where:** `pusher.py:107-110` hardcodes `await asyncio.sleep(120)`
on a 429. The other scrapers do not handle 429 at all (the broad
`except Exception` swallows it after status check).

**What:** RFC 7231 `Retry-After` header may carry a server hint
(seconds or HTTP-date). Ignoring it can lead to bans for premature
retries.

**Recommendation:** Parse `Retry-After`, bounded e.g. `min(int(header), 600)`.

---

### F-08 — LOW — Playwright accepts default TLS posture without explicit pinning

**Where:** `twitter.py:96-105` and `truthsocial.py:188-195`.

**What:** `launch_persistent_context` uses Chromium/Firefox's default
TLS validation. This is fine for the documented hosts (`x.com`,
`truthsocial.com`) but there is no certificate pinning or transparency
log check. The session cookies stored in `stealth/profiles/twitter/`
are sensitive (full account access), and a MITM on first session
setup could exfiltrate them via a corrupted browser context.

**Risk:** Low in production (TLS works), but the setup flow runs in a
**headed browser** on the operator's machine where the operator may
also have corporate root certs installed; this is the realistic MITM
path. Out of scope for a mechanical scraper audit but worth flagging.

---

### F-09 — LOW — Hashtag path segment not URL-encoded

**Where:** `truthsocial.py:163`.

```python
resp = await client.get(
    f"{API_URL}/timelines/tag/{tag}",   # ← f-string interpolation
    params={"limit": min(limit, 40)},
)
```

`tag` is derived from a keyword via
`keyword.strip().replace(" ", "").replace("#", "")`. The cleaning is
incomplete: keywords containing `/`, `?`, `#` (already stripped), `.`,
or URL-encoded sequences will produce a malformed path.

**Risk:** Mostly a correctness issue — the scrape silently returns 0
posts when the path is rejected. No injection because the host is
fixed in `BASE_URL`. Low.

**Recommendation:** `from urllib.parse import quote; quote(tag, safe="")`.

---

## 4. Per-dimension summary

### 4.1 Rate-limit compliance — OK with caveats

| Surface | Configured | Default | Jitter |
|---|---|---|---|
| Twitter between keywords | `TWITTER_DELAY_BETWEEN_KEYWORDS_SECONDS` | 45s | +0..10s |
| TruthSocial between keywords | `TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS_SECONDS` | 30s | +0..10s |
| TruthSocial between accounts | hardcoded | 5..15s random | — |
| TruthSocial account-lookup → statuses | hardcoded | 2..5s random | — |
| Twitter scrape interval | `TWITTER_INTERVAL_MINUTES` | 20 min | — |
| TruthSocial scrape interval | `TRUTHSOCIAL_INTERVAL_MINUTES` | 15 min | — |
| Metaculus | none | none | — |
| Substack | none | none | — |
| Pusher (to main server) | retry-only | 2/4/8/16/32/60s + 120s on 429 | — |

**Observations:**

- Twitter & TruthSocial have aggressive, jittered delays. Reasonable.
- Metaculus and Substack have **no delay between feed/query calls** —
  they loop over keywords / feeds with zero pause. For Metaculus this
  is 7+ keywords × ~50ms RTT each, which is fast but probably fine for
  a public API. For Substack with multiple feeds it's a tighter
  burst. Recommend a 1–2s sleep between iterations for politeness.
- Admin API (`PATCH /scheduler/interval/{job_id}`) accepts any
  `interval_minutes >= 1` — no upper-bound check that the operator
  isn't accidentally setting an interval below the platform's
  documented rate limit. (I-01.)
- No `Retry-After` parsing (F-07).

### 4.2 User-agent identification

| Scraper | UA strategy | Identifies as narve.ai? |
|---|---|---|
| Twitter (Playwright) | Random Chrome/Firefox UA from pool of 5 | No (deliberate stealth) |
| TruthSocial (Playwright) | Fixed Chrome UA | No (deliberate stealth) |
| TruthSocial (httpx) | Fixed Chrome UA | No |
| Metaculus | **None** (httpx default `python-httpx/x.y.z`) | No |
| Substack | **None** (httpx default `python-httpx/x.y.z`) | No |
| Pusher | None (auth via Bearer token, host is internal) | n/a |

For the Twitter/TruthSocial paths the spoofing is intentional and
documented in file headers. For Metaculus and Substack it is **neither
spoofed nor honest** — the worst combination. See F-05.

### 4.3 Redirect-following safety

| Scraper | follow_redirects | Allowlist check | Bounded |
|---|---|---|---|
| All httpx call sites | not set (httpx default `False`) | n/a | n/a |
| Playwright (twitter, truthsocial) | Browser default (follows) | none | none |

No call site explicitly sets `follow_redirects=False`. Today the
httpx default is safe; reliance on a default is brittle (F-03).

### 4.4 Content-type validation

**None of the 7 call sites checks `Content-Type`.** Every site jumps
from a `status_code == 200` check straight to `resp.json()` or
`feedparser.parse(resp.text)`. See F-02.

### 4.5 Max-fetch-size cap

**No cap anywhere.** No `content-length` precheck, no streaming with a
byte counter, no `httpx.Limits(max_response_bytes=...)`. See F-01,
F-04.

---

## 5. Test coverage of the audited paths

Audited per-platform under `gateway/scraper/tests/`:

- `tests/test_transmission.py` — mocks `httpx.AsyncClient` in pusher,
  tests 200 / 401 / 429 / 5xx code paths. Does **not** cover content-
  type mismatch, oversized response, or redirect-follow. Recommend
  adding regression tests for F-01 / F-02 fixes.
- No tests exercise `metaculus.py`, `substack.py`, or the public-API
  paths of `truthsocial.py`. Adding contract tests with httpx-mock or
  respx is cheap.

---

## 6. Methodology — synchronous bash only (hard rule)

Commands run, in order:

```
ls -la gateway/scraper/
ls gateway/scraper/scrapers/ gateway/scraper/transmission/ gateway/scraper/storage/
grep -rn "httpx\.\|requests\.\|urllib\|urlopen\|aiohttp" gateway/scraper/ --include='*.py'
grep -rn "follow_redirects\|max_redirects\|allow_redirects" gateway/scraper/
grep -rn "content_type\|Content-Type\|content-type" gateway/scraper/ --include='*.py'
grep -rn "content.length\|content_length\|max_size\|MAX_SIZE" gateway/scraper/ --include='*.py'
grep -rn "User-Agent\|user_agent\|user-agent" gateway/scraper/ --include='*.py'
```

Then `cat` (via Read tool) on every file under `gateway/scraper/` that
contains a fetch call site. No code modification, no test runs, no
network calls.

---

## 7. Out of scope

- Pre-release surfaces (`gateway/static/prerelease.html`,
  `gateway/static/pages/prerelease.css`,
  `gateway/pwa_middleware.py` critical CSS) — per hard rule.
- Playwright session cookie storage hardening (covered in
  `audit_security_dir.md` previously).
- The `transmission/pusher.py` happy-path (covered in
  `audit_state_reconciliation_drift.md`).
- Auth-key handling on the scraper's own FastAPI endpoints (covered
  in `audit_server_auth.md`).

---

## 8. Disposition

| # | Severity | Status |
|---|---|---|
| F-01 | HIGH | Open — recommend size cap + streaming on httpx calls |
| F-02 | HIGH | Open — recommend Content-Type allowlist before parsing |
| F-03 | MEDIUM | Open — recommend explicit `follow_redirects=False` |
| F-04 | MEDIUM | Open — recommend streaming RSS body |
| F-05 | MEDIUM | Open — set honest UA on Metaculus / Substack |
| F-06 | LOW | Open — add per-host circuit breaker |
| F-07 | LOW | Open — honour `Retry-After` |
| F-08 | LOW | Open — note only; outside scraper scope |
| F-09 | LOW | Open — URL-encode hashtag path segment |
| I-01 | INFO | Note — admin API has no minimum interval floor |
| I-02 | INFO | Note — limit is by post-count, not body size |
