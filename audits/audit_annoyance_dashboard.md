# Adversarial audit: `annoyance-dashboard/server.py` + `db.py`

**Date:** 2026-05-15
**Auditor focus areas (per request):**
1. Shared-secret HMAC validation from gateway (X-Gateway-Secret required)
2. Direct-origin request rejection in prod
3. SQL injection in dashboard queries
4. Public endpoints exposing private data

**Files in scope:**
- `/Users/shocakarel/Habbig/annoyance-dashboard/server.py` (755 LOC)
- `/Users/shocakarel/Habbig/annoyance-dashboard/db.py` (~1012 LOC)
- Supporting (read for context, not in scope): `auth.py`, `rate_limiter.py`, `config.py`

---

## Severity counts

| Severity | Count |
|----------|------:|
| Critical | 0 |
| High     | 3 |
| Medium   | 5 |
| Low      | 4 |
| Info     | 3 |

**Total findings: 15**

---

## Top 3 (most important to fix)

1. **H-01 — Admin-route localhost gate trusts `request.client.host` (TCP peer), not a verified header.**
   `auth.require_admin` consults `request.client.host` to decide whether to allow `/admin/*` and to fall back to a synthetic `super_admin` identity when no gateway auth headers are attached. There is no proxy-trust configuration on uvicorn and no `X-Gateway-Secret` requirement on this path. If the dashboard ever ships with `HOST != 127.0.0.1` (or behind any L7 proxy that doesn't terminate at 127.0.0.1) the gate degrades to "anyone whose TCP peer happens to be localhost." Worse: the localhost fallback issues a synthetic super_admin **even when no gateway headers are present**, so a single mis-deployment (host binding loosened, port published, or sidecar reverse-proxy that does not strip headers) means `/admin/reclassify`, `/admin/trigger`, `/admin/fp-resolve` are reachable unauthenticated as super_admin. The `assert_bound_to_localhost` startup check is the only defence and it is bypassable through several common ops mistakes (Docker `--network=host`, `0.0.0.0` set in a sidecar, public Cloudflare Tunnel pointing at 127.0.0.1, etc.). Admin routes must additionally require a valid `X-Gateway-Secret` (or a separate localhost-only admin token) — never just IP — and the localhost-fallback `super_admin` should be gated behind an explicit `ADMIN_ALLOW_LOCALHOST_NOAUTH=true` env flag.

2. **H-02 — No direct-origin rejection: `X-Gateway-Secret` is optional on every non-admin route.**
   `get_session_user` returns `None` when the secret is missing or invalid, and `require_paid_user` only raises 402 in that case. There is **no path** that returns 403 ("direct origin / missing gateway signature") — every public endpoint either rejects with 402 (paywall) or, for `/api/me` and `/healthz` and `/admin/page`, leaks information about server existence and DB state without proof of gateway origin. A direct attacker who bypasses the gateway (e.g. by hitting `127.0.0.1:8053` through any SSRF, an internal jumpbox, or a leaked subdomain that points at the box) can:
   - call `/healthz` to confirm liveness + read the DB path + learn whether `ANTHROPIC_API_KEY` is configured (`server.py:290-296`),
   - call `/api/me` for free with no secret to confirm "auth = false" (`server.py:725-740`),
   - probe `/admin` HTML and `/admin/*` (gated only by localhost check — see H-01),
   - enumerate every `/api/*` shape via 402 vs 422 vs 400 vs 429 vs 500 differentials.
   The gateway is the only thing that enforces TLS, WAF, and rate limiting; a single uvicorn process trusting `request.client.host` is not a substitute. Mitigation: a `require_gateway_signature` middleware that rejects with 403 on **every** request that lacks a valid `X-Gateway-Secret`, with `/healthz` either moved to a separate health socket or whitelisted by exact path.

3. **H-03 — `get_entity_recent_classified_posts` and `get_entity_hourly_counts_by_source` build a `LIKE` pattern from an unsanitized URL path parameter, enabling cross-entity data scraping and `LIKE`-wildcard injection.**
   `server.py:644` (`/api/entity/{name}/recent-posts`) and `server.py:632` (`/api/entity/{name}/spikes`) accept a path-parameter `name` that flows verbatim into:

   ```python
   # db.py:711
   (f"%{entity}%", limit),
   ```

   and

   ```python
   # db.py:903
   (hour_iso, next_hour, f"%{entity}%"),
   ```

   The parameter is *bound* (no classic SQL injection — `?` placeholders are used), but `%` and `_` in the user-supplied name are not escaped, so a Pro subscriber can submit `GET /api/entity/%25/recent-posts` (URL-encoded `%`) and pull **every** classified post in the database back, including:
   - raw `posts.content` (the original Reddit/Bluesky text) for posts ≤ 30 days old,
   - `posts.author`, `posts.url`, `c.is_sensitive`, `c.sensitive_reason`.

   The comment on `db.py:688-699` calls this "loose substring match is acceptable" — that justification was written assuming exact entity names, not user-controlled patterns. With `%` allowed, the `LIKE` predicate becomes a no-op. The retention loop scrubs content at 30 days (good), but everything fresher is exfiltratable in a single request. Also note the SQLite `LIKE` is case-insensitive by default on ASCII so even `_pple` matches every classification — useful for unindexed scans.

   Fix: `entity_name = name.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')` and append `ESCAPE '\\'` to the two `LIKE` predicates, or switch to a proper JSON containment query (`json_each(entities_json)`). Also cap response sizes and validate `name` against a charset whitelist.

---

## Findings detail

### H-01 — Admin gate trusts TCP peer IP (re-stated above). [HIGH]
**Where:** `auth.py:130-153`, `server.py:472-618` (all `/admin/*` handlers).
**Risk:** Auth bypass / privilege escalation if any deployment posture lets an attacker present as 127.0.0.1 (host networking, SSRF from a co-located process, kernel bind to 0.0.0.0, container break-out, etc.).
**Why it matters:** the localhost-fallback synthetic super_admin (`auth.py:146-152`) is granted **when no gateway secret is present** — so this isn't a "defence in depth" check, it's the *only* check on that path. Once an attacker speaks to 127.0.0.1, `auth.require_admin` returns a `super_admin` dict and they can call:
- `POST /admin/reclassify?limit=999999` — bulk reset every classified post (cost ceiling will then refire Claude classification, draining the daily $10 ceiling).
- `POST /admin/trigger?loop=classifier` — same, drives spend.
- `POST /admin/fp-resolve` — silently resolve every FP flag, making bad spikes look fine to operators.
- `GET /admin/cost-summary` — observe API spend / model fingerprint / ceiling state.
- `GET /admin/fp-queue` — read every user-submitted FP reason (which is free-text 0-500 chars) and the `user_email` of every flagger — that's PII exfiltration of every Pro user who's ever clicked the flag button.

**Fix:** require a valid `X-Gateway-Secret` on `/admin/*` paths too, then gate `super_admin` strictly via tier. Drop the localhost-fallback synthetic admin behind an opt-in env flag for operator console use only.

---

### H-02 — No direct-origin rejection (re-stated above). [HIGH]
**Where:** `auth.py:56-99`, `server.py:290-296` (`/healthz`), `server.py:725-740` (`/api/me`), `server.py:612-618` (`/admin` HTML).
**Risk:** Surface enumeration + information disclosure if the box is reachable directly. The dashboard refuses to bind 0.0.0.0 (`auth.assert_bound_to_localhost`, server.py:228) but doesn't refuse to *serve* requests whose origin is not the gateway. The two checks are not equivalent: misconfigured Nginx, Cloudflare Tunnel, or `iptables -t nat -A PREROUTING` rules will all pass requests through to 127.0.0.1:8053 without `X-Gateway-Secret`.
**Specific leaks** to unauthenticated callers:
- `/healthz` returns `db` path **and** the boolean `has_api_key` — confirms the box has an Anthropic key configured (interesting for an attacker plotting cost-burn).
- `/api/me` always returns 200 even without the gateway secret (`server.py:725-740`).
- `/admin` page (HTML shell) — `auth.require_admin` will 403 here, but it does so without checking the gateway secret first, so a direct attacker on 127.0.0.1 fetches the page.

**Fix:** add a single FastAPI middleware that runs before route dispatch, validates `X-Gateway-Secret` against `GATEWAY_SSO_SECRET` (constant-time), and short-circuits to 403 for everything except a small allowlist (`/healthz`, `/static/*`). Even `/healthz` should not echo the DB path.

---

### H-03 — `LIKE`-wildcard injection on entity name parameter (re-stated above). [HIGH]
**Where:** `db.py:688-712`, `db.py:890-905`, called from `server.py:632`, `server.py:644`.
**Risk:** Authenticated cross-entity data scraping: a single Pro subscriber pulls every classified post in the database, including raw content for posts <30 d old, with one request to `/api/entity/%/recent-posts?limit=200`.
**Also:** `db.py:447-458` (`get_entity_history`) uses `WHERE entity = ?` (equality) and is safe; the bug is specifically the `LIKE %?%` join filter on `entities_json`.
**Fix:** escape `%`/`_`/`\\` and add `ESCAPE '\\'`, or use JSON containment (`entities_json LIKE '%"name": "Apple"%'` is still fragile — a `json_each` approach is robust).

---

### M-01 — `/api/fp-flag` writes every user's email and free-text "reason" to `fp_flags` and `admin/fp-queue` exposes them in JSON to any super_admin caller. [MEDIUM]
**Where:** `db.py:819-831` (insert), `db.py:834-847` (list, joined with `spikes`), `server.py:570-584`.
**Risk:** Privacy: the admin queue returns every flagger's `user_email` and verbatim `reason` (≤500 chars). Combined with H-01 (localhost-fallback admin), an attacker on the box reads every Pro subscriber's email and any free-text complaint they submitted. This is non-trivial PII for a paid SaaS.
**Fix:** hash or truncate emails server-side before storing them, and consider not surfacing them in `/admin/fp-queue` at all (a Stripe customer ID would be enough for the reviewer to look the user up).

---

### M-02 — `entity_markets.json` cache poisoning is impossible *now* but the public endpoint is unrate-limited beyond `_guard_api`. [MEDIUM]
**Where:** `server.py:679-683`, `server.py:660-676`.
**Risk:** Low-grade DoS / cache amplification. `_load_entity_markets` lazy-loads a JSON file once per process. The file path is `Path(__file__).parent / "entity_markets.json"` — safe. But `/api/entity/{name}/markets` is gated only by `_guard_api` (60 req/min/user), and a single user with 100 entity names can drive 100 dict lookups per request × 60/min — not catastrophic but the cache key being `_ENTITY_MARKETS_CACHE` (no per-entity bound) means a future curator typo that returns a 50 MB blob OOMs the worker on first request.
**Fix:** validate `_ENTITY_MARKETS_CACHE` size at load, add a hard upper bound on `name` length (already capped in the suggestions endpoint at 200, not here).

---

### M-03 — `/api/market-suggestions` writes user-controlled strings to a plain-text log on disk, with `\t`-delimited fields the user can forge. [MEDIUM]
**Where:** `server.py:686-720`.
**Risk:** Log injection / log forgery. The handler builds:

```python
line = (
    f"{datetime.now(timezone.utc).isoformat()}\t"
    f"user={user.get('id')}\t"
    f"email={user.get('email')}\t"
    f"entity={entity}\t"
    f"url={url}\t"
    f"note={note}\n"
)
```

`entity`, `url`, `note` are user-controlled (capped at 200/500/500 chars), but the user can embed `\t` and `\n` and pretend to be a different user in subsequent forged lines. If any downstream parser splits on `\t`/`\n` (a future SIEM, even a `grep | cut`), the attacker can attribute false suggestions to other emails.
**Fix:** JSON-encode each line (`json.dumps(...)`) so embedded delimiters are escaped, and strip control characters from the inputs.

---

### M-04 — `db.py:199` builds a `PRAGMA table_info` query with f-string interpolation of `table`. [MEDIUM]
**Where:** `db.py:198-200` (`_table_columns`).
**Code:**
```python
rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
```
**Risk:** Theoretical SQL injection — but `table` only comes from `_COLUMN_MIGRATIONS` (developer-controlled hard-coded strings) and from `cur.execute(...)` in `db.insert_spike:518-521` and `db.get_recent_spikes:560-563`, also hard-coded `"spikes"`. So this is **not exploitable today**. It's flagged because the function signature accepts a free string and the f-string composes raw SQL; any future caller that wires this to a request parameter inherits a fresh SQLi. SQLite's `PRAGMA` form does not accept `?` placeholders, so the only correct fix is a whitelist of known table names. Add an `assert table in _KNOWN_TABLES` guard.

---

### M-05 — `scrub_raw_content_older_than` runs on a thread-local connection with no `BEGIN IMMEDIATE`; under WAL + parallel writers a long retention scan can starve out the classifier's update. [MEDIUM]
**Where:** `db.py:910-924`, `server.py:157-175` (retention loop).
**Risk:** Liveness, not security per se. Under load (`fetched + insert_post` running every 600s × 2 sources × 50 posts) the retention loop's `UPDATE posts SET content=''` scan can block the classifier's `UPDATE posts SET classified=?`. SQLite WAL helps with read-write contention but writer-writer still serializes. The loop runs every 6 h on the same thread-local conn (`_get_conn` is per-thread) so this is bounded, but if `posted_at` is unindexed at the *cutoff* point the scan is slow.
**Note:** `idx_posts_posted_at` (db.py:73) exists, so the cutoff range scan is indexed. Risk is residual. Could add a `LIMIT 1000` cap on each retention pass.

---

### L-01 — `/api/entity/{name}/markets`, `/api/entity/{name}/spikes`, `/api/entity/{name}/recent-posts`, `/api/entity/{name}` all happily accept arbitrarily long `name` path parameters; no length cap. [LOW]
**Where:** `server.py:406-414`, `server.py:632-641`, `server.py:644-653`, `server.py:679-683`.
**Risk:** Resource exhaustion via long `LIKE` patterns (combined with H-03, a `name` of `%` + 16 KB padding becomes a worst-case scan).
**Fix:** cap `len(name) <= 200`.

---

### L-02 — `/api/fp-flag` accepts arbitrary `target_id` strings, calls `int(target_id)`; the only validation is "must parse as int". Negative IDs, zero, and IDs that don't exist all silently succeed (the insert just no-ops via FK violation, but the route returns `{"ok": true}`). [LOW]
**Where:** `server.py:428-467`, `db.py:819-831`.
**Risk:** UX confusion + log noise. The catch on `db.insert_fp_flag` swallows the FK violation silently (`server.py:465`); a confused user could spam fake spike IDs and get a `200 OK` back. Combined with the per-user 10/min limit this is bounded but not zero.
**Fix:** check spike exists before inserting, return 404 if not.

---

### L-03 — `request.client.host` is read directly (`auth.py:127`) without consulting `forwarded_allow_ips` or `--proxy-headers`; if the gateway ever puts a trusted proxy in front (Cloudflare → gateway → annoyance-dashboard), the localhost check on `/admin/*` may see the gateway's IP, not the original client. [LOW]
**Where:** `auth.py:126-127`.
**Risk:** Today the gateway and annoyance-dashboard are colocated on 127.0.0.1, so this is correct. If the architecture ever splits (gateway in cluster A, dashboards in cluster B), the localhost check silently degrades to "the gateway machine is always admin." Document this assumption inline.

---

### L-04 — `/healthz` returns the configured `db` path (an absolute filesystem path) and an `ANTHROPIC_API_KEY` boolean to *anyone* (no auth, no gateway secret, no rate limit). [LOW]
**Where:** `server.py:290-296`.
**Risk:** Mild information disclosure. Reveals deployment layout (`/Users/shocakarel/Habbig/annoyance-dashboard/annoyance.db` → developer username) and configuration state. Useful for fingerprinting in a multi-stage attack.
**Fix:** `/healthz` should return only `{"status": "ok"}`; everything else moves to `/admin/healthz` behind the admin gate.

---

### I-01 — `db.get_recent_spikes` and `db.get_entity_spikes` deserialize JSON columns from the DB with `json.loads(... or "null")`; a poisoned `sources_json` (which is never user-controlled today) would cause a deserialization that ignores `null` and returns the default. Safe today, fragile if writers change. [INFO]

### I-02 — `entity_markets.json` is read with `Path.read_text()`; if the file is symlinked outside the repo by a malicious operator, contents are trusted. Not realistically exploitable. [INFO]

### I-03 — The `auth.assert_bound_to_localhost` check is good and called twice (`server.py:228`, `server.py:747`). Worth keeping. [INFO]

---

## Threat-model summary

The dashboard is conceptually a *backend service* meant to be reachable only via the gateway, but the codebase enforces that posture with two weak signals:

1. **A startup check** that the listen address is loopback. Bypassable by deployment misconfig.
2. **A localhost check on `/admin/*`** that synthesizes super_admin on no-auth. Bypassable by direct origin.

Every public endpoint *checks the gateway secret*, but no endpoint *requires* the gateway secret as a precondition for serving any response. The contract is "if you send the secret, you get a user; otherwise you might still get something." The gap between "valid gateway request" and "any request that lands on the socket" is the entire attack surface for H-01 and H-02.

SQLi proper is not present (the codebase consistently uses `?` placeholders), but the `LIKE` injection in H-03 achieves the same exfiltration outcome with less effort.

Recommended hardening order:
1. **Add a `gateway_signature_required` middleware** (covers H-02 fully and reduces H-01's blast radius). 1-day fix.
2. **Escape `LIKE` wildcards in the two entity lookups** (H-03). 1-hour fix.
3. **Stop synthesizing super_admin on localhost-no-auth** (H-01). 30-min fix + a flag for ops convenience.
4. Hash emails in `fp_flags` (M-01).
5. Cap `name` length (L-01).
6. Trim `/healthz` (L-04).
