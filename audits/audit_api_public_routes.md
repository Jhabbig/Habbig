# Adversarial Audit — `gateway/api_public/routes.py`

**Scope:** public REST API (`/api/public/v1/*`) + its auth/rate-limit/signing
layer in `gateway/api_public/auth.py`. Focus: API-key auth path, per-key rate
limit, signature validation, response leak risks, tenant isolation, error
message info-leak.

**Auditor stance:** treating any authenticated key-holder as adversarial
(stolen key, malicious dev, abusive scraper) AND treating an unauthenticated
attacker as a probing scanner.

**Severity legend:** CRITICAL > HIGH > MEDIUM > LOW > INFO.
Counts: CRITICAL 1, HIGH 4, MEDIUM 3, LOW 2.

---

## Top 10 findings (severity-sorted)

- **[CRITICAL] Tenant-isolation gap on `GET /predictions/{id}` — author identity
  leaked for "anonymous public" rows.** `routes.py:328-363` returns
  `dict(row).keys()` from `user_predictions` for any `is_public=1` row, then
  *only* nulls `user_id` when `row["is_anonymous"]` is also true. That means a
  public-but-not-anonymous prediction discloses the author's `user_id` to any
  bearer-key holder — fine. BUT the payload is a wholesale `SELECT *` (see
  `queries/predictions.py:392`) so every column ships, including
  `reasoning`, `created_at`, `market_price_at_prediction`, and
  `edge_at_prediction`. Anyone with a `read`-scoped key (which is *every*
  valid key — `auth.py:56`) can enumerate prediction IDs sequentially
  (autoinc) and harvest the full timeline of every public-poster. Mitigation:
  whitelist the public-facing columns explicitly and strip
  `edge_at_prediction` + `created_at` minute-resolution for non-owners.
  Anonymous rows also still leak `created_at` which can be correlated with
  the user's join-time / activity pattern to de-anonymise. The "we
  deliberately don't leak existence" comment on line 332 is contradicted by
  the next branch: a 404 for "private of another user" vs 200 for "public"
  is a side channel — an attacker can probe each prediction_id and learn
  which rows exist, then learn `is_public` from the response code.

- **[HIGH] No timing-safe key lookup — DB-time differs for valid/invalid hash
  prefixes.** `auth.py:87-90` SHA-256s the bearer and does a single
  `SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL`. Lookup
  is by exact equality on a B-tree index, so hits return faster than misses
  on warm pages and slower on cold pages — over enough probes this exposes a
  timing oracle on key-hash prefixes. Worse, when a hit returns, the
  follow-up `bump_api_usage` + `touch_api_key_last_used` add two more
  round-trips before the response (`auth.py:96,117`), so the time delta
  between "valid key" and "invalid format / no row" is large and trivially
  measurable. Mitigation: add a constant-latency floor (artificial
  `await asyncio.sleep` to align the 401 path with the success path), and
  consider an in-process LRU lookup that doesn't go to disk on the miss
  path. Also: `hashlib.sha256(raw_key.encode())` runs even on bogus tokens
  — fine, but the entire `verify_api_key` should be `secrets.compare_digest`
  on the hash, not equality SQL, if a per-key salt is ever introduced.

- **[HIGH] `allowed_origins` allowlist is stored, surfaced in the settings
  UI, but **NOT enforced** by `verify_api_key`.** Migration 180
  (`migrations/180_api_keys_origins.py`) adds `api_keys.allowed_origins`,
  the management UI (`api_keys_routes.py:232`) lets users configure it, and
  `embed_routes.py:76` honours it via `queries.api_keys.validate_api_key`.
  The public-API auth path in `gateway/api_public/auth.py:60-138` does
  **not** read this column at all — it pulls the key via
  `db.get_api_key_by_hash` (`db.py:1149`) which `SELECT *`s the row but
  never compares `Origin`/`Referer` against `allowed_origins`. A user who
  set "only api.acme.com" expecting protection has zero enforcement on
  `/api/public/v1/*`. Mitigation: replicate the embed path's hostname
  check, or route both through a shared `validate_api_key` helper.

- **[HIGH] Fail-open on rate-limit write race / DB error.** `auth.py:96-100`
  catches *any* exception from `bump_api_usage` and falls through with
  `count = 1`, so a transient sqlite lock means the limit isn't enforced
  for that request. Worse, an attacker who can induce DB stalls (e.g.
  pushing the WAL into a long checkpoint by hammering a write-heavy
  endpoint in parallel) gets free traffic during the stall window. The
  comment ("denying real traffic because a write race failed is worse than
  allowing one extra request above the quota") understates the blast
  radius: the fallback admits an *unbounded* number of extra requests
  while the DB is unhappy, not just one. Mitigation: fail-closed for keys
  whose `usage_this_hour` is already near the cap, or use an in-process
  token bucket as a backstop.

- **[HIGH] No request signature, no replay protection, no nonce.** The
  module docstring (`routes.py:1-12`) only enumerates bearer-token auth +
  `sign_if_available` (which is *outbound* forensic watermarking, not
  inbound integrity). There is no HMAC/X-Signature header check, no
  request-id, no timestamp window, no nonce, no body hash. The
  `POST /predictions` mutation (`routes.py:366-432`) is therefore
  replayable: capture the bearer + body once, replay until rate-limit
  flips. Combined with TLS-stripping captive portals or any logging path
  that captures `Authorization` headers, a stolen header is full account
  takeover for the API surface. Mitigation: require an
  `X-Narve-Timestamp` + `X-Narve-Signature: hmac-sha256(secret, ts || method ||
  path || sha256(body))` for write endpoints, reject `|ts - now| > 300s`,
  and persist `(key_id, sig)` nonces to a short-TTL replay cache.

- **[MEDIUM] `_ok` reads `request.state.api_key` without nullguard — a
  handler that forgets to call `verify_api_key` will 500 with an
  `AttributeError` and leak the traceback on a misconfigured deploy.** Every
  GET handler manually calls `verify_api_key(request)` (`routes.py:91`,
  `:104`, `:120`, …) instead of using `Depends(verify_api_key)`. The
  `POST /predictions` handler relies on `Depends(require_scope("write"))`
  which transitively calls `verify_api_key` — that works today, but any
  future GET added without the manual call will pass auth (FastAPI doesn't
  enforce it) AND will crash inside `_ok` at `request.state.api_key`
  (`routes.py:61`) leaking a stack trace if `DEBUG=True` is ever
  accidentally enabled. Mitigation: convert to
  `key: dict = Depends(verify_api_key)` on every route or have `_ok`
  re-validate with a guard + 401 fallback.

- **[MEDIUM] Raw `slug`/`handle` echoed in 404 detail bodies — reflected
  content + enumeration aid.** `routes.py:109` `f"Market {slug} not
  found"`, `:174` `f"Source @{handle} not found"`. The `slug` path-param
  passes through FastAPI's default string converter with no length cap, no
  pattern, no charset restriction; a 2 KiB payload of `<script>` or
  `${jndi:...}` will be reflected verbatim in the JSON `detail` field.
  JSON-encoding neutralises HTML/JS interpretation in browsers, but logs
  ingest this into log-management UIs (Datadog, Loki) that *do* render
  it, and the bytes echo amplifies log-spam DoS. Mitigation: truncate
  slug to first 80 chars and quote-escape before formatting, or just
  return `"Market not found"` with the slug only in the structured log.

- **[MEDIUM] Tenant fan-out via SQL injection-adjacent — direct SQL in
  `/sources/{handle}/predictions` and `/sources/{handle}/history`.** The
  module docstring (line 9-11) claims "Nothing here should be doing SQL
  itself", yet `routes.py:186-191` and `:204-210` build SQL inline with
  parameterised binds (safe from injection), but bypass the
  `queries/` layer's `LEFT JOIN` to `source_credibility` and instead
  `SELECT *` from `predictions`. That returns every column including
  `source_url` (potentially private upstream URL with token query-strings
  if the scraper ever stored a URL with creds — defensive concern) and
  `content` (full extracted text). Either move these to query helpers
  *or* explicitly select the public columns. Also: there is no
  `OFFSET`/pagination cursor on these two — an attacker can call
  `?limit=1000` and exfiltrate the whole `predictions` table for any
  `source_handle` in O(1) calls.

- **[LOW] Rate-limit headers reveal another user's exact key prefix on the
  same response.** `routes.py:73` `"X-Narve-Key-Prefix": key.get("key_prefix",
  "")` is harmless when echoed to the key-owner, but combined with the
  fact that the bearer prefix is also predictable (`narve_`) from
  `auth.py:39`, this is one of the few headers that ever leaves the server
  with caller-identifying material. If a downstream caching proxy honours
  `Vary` incorrectly or strips `Authorization` while caching the body, this
  header propagates the prefix in cached responses. Risk is small but the
  header serves no client purpose (the client knows its own prefix).
  Mitigation: drop `X-Narve-Key-Prefix` from 2xx, or only emit it on the
  `/usage` endpoint.

- **[LOW] `_clamp` ignores `int` overflow / non-positive `limit=` values
  silently, masking abuse signals.** `routes.py:78-83` accepts any
  `int(limit)` then clamps with `max(1, min(v, hard_max))`. Sending
  `limit=-99999999` returns `1`, sending `limit=99999999` returns the
  hard-max. There is no log/metric on "client tried to request a 100M
  page" — that's exactly the signal you want to alert on. Also, the
  `before_id` validation in `/feed` (`routes.py:243-249`) raises 400 on
  negative values, which is inconsistent: feed strictly validates,
  every other endpoint silently coerces. Mitigation: emit a
  `log.info("clamped %s.limit=%s -> %s", endpoint, raw, v)` and align on
  one validation policy.

---

## Other observations (not in top-10 but worth a sweep)

- `sign_if_available` (`auth.py:162`) wraps **every** outbound payload but
  the `forensics.signer` module is imported lazily inside try/except —
  silent failure here means watermarking can silently disappear if a
  module-level import fails after a deploy. The dashboard won't surface
  this (`auth.py:172` swallows). Consider a startup smoke-test of
  `forensics.signer.sign_response`.
- `v1_create_prediction` uses `body.get("market_question") or ""[:500]`
  which truncates at 500 chars — fine — but `category` is bound to 32
  chars without an allowlist. A malicious key can pollute the
  `category` column with arbitrary text (e.g. `<svg onload=...>`)
  which then leaks back through `GET /feed?category=` reflection and
  any admin UI that renders the column.
- `v1_calendar` (`routes.py:299-322`) embeds inline SQL again. Same
  observation as #8.
- No request ID echoed in responses — incident triage on the customer
  side is harder. Cheap to add.

---

## Severity counts

- CRITICAL: 1
- HIGH:     4
- MEDIUM:   3
- LOW:      2
- TOTAL:   10
