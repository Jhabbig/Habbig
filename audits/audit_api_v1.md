# Audit — `gateway/api_v1.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Target: `/Users/shocakarel/Habbig/gateway/api_v1.py` (498 lines, reviewed at HEAD `7a443e0`, branch `feature/platform-build`).

Scope (from brief):

1. Missing auth
2. Missing rate-limit
3. Response leak (PII / cross-tenant / secret material in JSON bodies)
4. Schema validation (input + output)

Pre-release-only findings are flagged but **not counted** in the severity table per the
hard rule. They are listed in a separate "Pre-release notes" appendix.

Supporting layers read for cross-checks:

- `gateway/server.py` (router mount L8187-8190; CSRF skip-list L1147-1167; OpenAPI security tagging L695-725)
- `gateway/security/rate_limiter.py` (`limiter.check` contract, decorator semantics L160-220)
- `gateway/queries/api_keys.py` (no `first_displayed_at` column write path)
- `gateway/migrations/014_api_keys.py`, `128_api_keys_ext.py`, `180_api_keys_origins.py` (no migration introduces `first_displayed_at`)
- `gateway/saved_views_schema.py` (`filters_from_query`, `build_where` for `sources` + `predictions` scopes)
- `gateway/cache/ttl.py` (`DEFAULT_TTLS`, `ttl_invalidate.on_new_prediction` — does NOT bust `sources:` prefix)
- `gateway/db.py` (predictions schema L195-208; `predictions.content` is `TEXT NOT NULL`)
- `gateway/tests/test_api_v1_consensus.py` (only 401 / 404 / 200 / TTL paths; no auth-bypass, scope, or output-shape coverage)

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High     | 4 |
| Medium   | 5 |
| Low      | 4 |
| Info     | 2 |
| **Total**| **16** |

Severity rubric:

- **CRITICAL** — auth bypass, cross-tenant data leak, RCE, secret exposure.
- **HIGH** — exploitable under realistic conditions; quota/rate-limit bypass; SQL injection vector.
- **MEDIUM** — defense-in-depth, race-condition windows, non-fatal output leaks, log hygiene.
- **LOW** — code quality with security implications; refactor foot-guns; missing hardening.

## Top 3 findings (ranked by exploitability × impact)

1. **CRITICAL — `create_api_key` swallows arbitrary DB errors** (`api_v1.py:60-78`). The migration `0XX_api_keys_first_displayed_at` is still a TODO (no file in `gateway/migrations/`), so the primary INSERT at L62-66 always fails with `OperationalError: no such column: first_displayed_at`. The bare `except Exception:` at L67 then falls back to the legacy INSERT — but the same blanket catch silently absorbs UNIQUE-constraint violations, NOT-NULL violations, schema drift on any other column (e.g. if `scopes` default ever changes), AND the M16 guard itself once the migration ships. Because `_validate_key` later rejects revoked / hash-mismatched keys, a hash-collision-style retry storm here is invisible. The audit-log helper used elsewhere is not called, so a creation-fail-then-fallback never shows up in `audit_log`. Net effect today: every prod key is created via the legacy path with no `first_displayed_at` stamp, so `get_api_key_raw`'s `SELECT first_displayed_at` at L97-100 always hits the second `except Exception: return None` branch — the M16 "first display only" guarantee is **structurally inert**. The function is a one-line safety net protecting a column that does not exist; any code path that relied on it (e.g. a future GET handler that hands back `raw_key`) would silently pass the guard. **Fix:** (a) ship the migration, (b) narrow the catch to `sqlite3.OperationalError` AND match the message (`"no such column"`), (c) log + audit on the fallback path so the migration gap is visible, (d) make `get_api_key_raw` `return None` explicitly only when the column truly is NULL — today it returns None regardless of value, which is wrong semantics.

2. **HIGH-1 — Bearer token comparison is a non-constant-time SHA-256 lookup, but the prior step is a length leak.** `_validate_key` at L115-128 strips `auth[7:]` with no length cap (raw_key has no maximum), then hashes and SELECTs. Two issues stack: (a) `request.headers.get("Authorization", "")` accepts headers up to FastAPI/uvicorn's default 8 KiB, so a 4 KB Bearer token forces a 4 KB SHA-256 hash on every request — cheap individually, but a per-IP attacker with no rate limit (see HIGH-2) can pin CPU; (b) `c.execute("SELECT * FROM api_keys WHERE key_hash = ?")` returns all columns including `key_hash`, `rate_limit_hour`, `tier`, `user_id`, `last_used_at` — the row dict is then returned (L150) to callers, but only handler internals see it. The actual leak is HIGH-3 below; this finding is the DoS surface. **Fix:** cap the raw key length at the strip site (`raw_key = auth[7:200]` — narve keys are `narve_` + 43 base64url chars = 49 chars; 200 is generous), and SELECT only the columns you use (`id, revoked_at, rate_limit_hour, user_id, tier`) — avoid `SELECT *` so future schema additions don't accidentally widen the row dict.

3. **HIGH-2 — Pre-auth rate-limit is missing; `_validate_key` only rate-limits AFTER it knows the key is valid.** L132-141 calls `limiter.check(f"apiv1:{row['id']}", ...)` keyed by the api_key row id — but that key id is only known after a successful DB lookup. Anonymous traffic hitting `/api/v1/sources` with an invalid or guessed Bearer token gets a fresh `SELECT * FROM api_keys WHERE key_hash = ?` on every request with **no per-IP throttle inside this module**. The global per-IP middleware (`GLOBAL_RATE_LIMIT_PER_MIN`, ref'd in test bootstrap) does exist, but it counts every request equally — a brute-force enumeration of API keys (each call: 1 SQL read, 1 SHA-256) burns budget for legitimate traffic from the same NAT/CGNAT pool. With a 32-byte URL-safe key the search space is fine; the operational concern is the cost of negative-path responses: each 401 still costs a DB round-trip plus the hash. **Fix:** add a pre-validation `_is_rate_limited(f"apiv1_anon:{get_client_ip(request)}", 60, 60)` at the top of `_validate_key`, returning 429 *before* the DB read, and a separate, tighter bucket for 401-emitting paths (`apiv1_invalid:{ip}`, 10 per 5 min) — the auth-bucket pattern already exists in `rate_limiter.py:AUTH_RATE_LIMIT_*`.

---

## All findings

### CRITICAL

- **CRIT-1 | L60-78 | `create_api_key` blanket-catches `Exception` around the primary INSERT.** Covered as top-3 #1 above. Compounds with L97-107 — `get_api_key_raw` returns None regardless of the column's value (`if not row: return None` ... `return None`), so the M16 chokepoint described in the docstring is fictional. The function is referenced from the docstring of `get_api_key_raw` (L84-94) as the only sanctioned read path, but no caller actually invokes it in this file — no handler ever returns raw key material. The risk is latent: anyone adding a "show me my key once" endpoint can wrongly believe this helper enforces the first-display guard. **Fix:** see top-3. Also delete `get_api_key_raw` entirely until the migration lands and a real first-display flow exists — dead code that claims to be a security guarantee is worse than no code.

### HIGH

- **HIGH-1 | L115-128 | Bearer length leak / DoS surface.** Covered as top-3 #2.

- **HIGH-2 | L132-141 | Pre-auth rate-limit missing.** Covered as top-3 #3.

- **HIGH-3 | L143-148 | `last_used_at` UPDATE writes on every authed request with no debounce, on a separate `db.conn()` connection.** Two issues: (a) every API call takes a second write lock on `auth.db` after the SELECT lock from L122-125 already released — SQLite WAL handles this fine at low QPS but a key burning its 10k/hr enterprise budget will issue 10k writes/hr against `api_keys.last_used_at`. There is no batching, no `last_used_at IS NULL OR last_used_at < now - N` predicate, and no async deferral. (b) `c.execute(... UPDATE ... WHERE id = ?)` runs inside the `with db.conn() as c:` context manager, which commits on exit; if the request body is large or downstream `_compute()` is slow, the write has already committed before the response goes out — there is no rollback path if the handler 500s after this point. The user sees a "last_used" that ticked even though the request failed. **Fix:** debounce — `UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)` with a 60s freshness window. Or move the UPDATE to a fire-and-forget after the response (FastAPI `BackgroundTasks`). Either eliminates the write storm.

- **HIGH-4 | L156, 250, 290, 385, 460 | `_validate_key` is called but its return value (the api_key row) is discarded.** Every endpoint does `_validate_key(request)` with no assignment. Consequence: there is no tier-based gating on which endpoints a key can hit. A free-tier key (rate_limit_hour=1000) and an enterprise key (10000) reach the same endpoints — `/markets/edge` (which spins up a `PolymarketClient` AND a `KalshiClient` and calls `unified_markets.fetch_unified_markets` with a 5-minute TTL) is callable by every key tier. The migration at `128_api_keys_ext.py` introduced an `api_keys.scopes` column (`'read'` default, `'read,write'` for write paths) and the docstring of that migration explicitly says "scope unlocks POST /api/public/v1/predictions" — this module has NO POST endpoints and reads no scope, but the broader concern is that an enterprise-only endpoint cannot be enforced. **Fix:** return the row dict from `_validate_key` and have each handler do `key = _validate_key(request); _require_tier(key, "enterprise")` for paid endpoints. At minimum, check `'read' in key['scopes'].split(',')` for read endpoints to lock down keys that were issued write-only by mistake.

### MEDIUM

- **MED-1 | L168, 310, 469 | Input clamping on `limit` but not on `offset`.** `limit = max(1, min(limit, 500))` is correct, but `offset` is taken straight from the query param and forwarded into `LIMIT ? OFFSET ?`. SQLite handles huge OFFSETs by counting rows, so `offset=999999999` against `source_credibility` (today ~5-15k rows, eventually 100k+) forces a full table scan returning zero rows on every call. Negative offsets are passed through to SQLite which treats them as 0 — not a security bug, but the API contract is unclear. **Fix:** `offset = max(0, min(offset, 50000))`. Match the pattern used elsewhere (see `forecast_routes.py` for the standard cap).

- **MED-2 | L177-204 | Pagination cache key omits filter dimensions.** `cache_key = f"sources:sort_default:filter_none:page_{page_num}_size_{limit}"` is correct for the no-filter path (the `if not sv_filters:` guard), but the `_compute` closure calls `db.list_all_source_credibilities()` and slices `[offset:offset+limit]` — meaning the FULL list is loaded into memory inside the factory on a cache miss, then sliced. If `list_all_source_credibilities` returns 100k rows, the un-cached cache miss allocates the full list, slices the page, returns the page — but the closure's `sources` variable goes out of scope so the full list is GC'd. Cost: O(N) memory per cache miss × concurrent miss count. With a thundering-herd on cache expiry (`DEFAULT_TTLS["sources"] = 120`), N concurrent workers each allocate the full list. **Fix:** push the LIMIT/OFFSET into the SQL query (new `db.list_source_credibilities_page(limit, offset)`); cache the page directly. Today's row count makes this a latent concern, not an active vuln, but the brief explicitly listed "response leak" — a memory-pressure DoS is still a leak of compute resources.

- **MED-3 | L370 | `content[:500]` truncates Python strings, but does NOT sanitize.** The `predictions.content` column is operator-supplied (per `gateway/db.py:201`, `TEXT NOT NULL`, no length cap, no character allow-list). It is rendered into the JSON response verbatim. The endpoint correctly returns `application/json` so HTML/script content is not auto-executed by browsers, BUT clients that string-interpolate the field into a webpage (e.g. an embed widget, a dashboard) will render whatever the operator pasted in — bidi-override unicode (`U+202E`), zero-width joiners, control characters all pass through. The 500-char cap is the only filter. **Fix:** strip non-printable / bidi control chars at write time (in the extractor) AND at read time as a belt-and-braces: `content = ''.join(c for c in r["content"] if c.isprintable() or c == '\n' or c == ' ')[:500]`. Same fix as audit_api_keys_routes.md MED-3.

- **MED-4 | L460-497 | `/markets/edge` instantiates Polymarket + Kalshi clients on every request.** L476-479 creates two HTTP clients (`PolymarketClient()`, `KalshiClient(base_url=os.environ.get("KALSHI_API_BASE", ...))`), calls `fetch_unified_markets(..., cache_ttl=300)`, then `await poly.close()` + `await kalshi.close()`. The HTTP clients are not reused; each call performs a TLS handshake against both upstreams. With no per-tier gating (HIGH-4), a free-tier key (1000 req/hr) can drive 1000 Polymarket + 1000 Kalshi handshakes per hour, per key. Polymarket has documented rate limits; sustained hammering can get the gateway's egress IP throttled, which then breaks the upstream cache that legitimate dashboards depend on. **Fix:** module-level singleton clients with connection pooling (the rest of the codebase uses this pattern — see `backend/markets/__init__.py`), or read from `unified_markets.fetch_unified_markets` cache without re-instantiating clients on a hot path.

- **MED-5 | L170-247 + 312-382 | SQL is composed via f-string interpolation of `extra_where` / `extra_joins` from `saved_views_schema.build_where`.** `build_where` returns `(where_clause_str, params_list, joins_list, ...)`. Parameters are bound via `?` placeholders (good), but the WHERE clause text and JOIN clause text are interpolated directly. If `build_where` ever returns attacker-influenced JOIN text (today it does not — joins are hard-coded constants in `saved_views_schema.py:391-419`), the f-string at L221, L225, L347, L353 would be a SQL injection sink. The risk is bounded by `build_where`'s own correctness, but the audit pattern is fragile — a future contributor adding a new scope to `saved_views_schema` might forget that this consumer doesn't sanitize. **Fix:** either (a) have `build_where` return a named-tuple with an explicit `is_safe_static` flag the consumer asserts on, or (b) move to SQLAlchemy Core / a tiny query-builder. At minimum, add a comment block at the top of each handler citing the trust assumption: `# trusts saved_views_schema.build_where to emit static SQL fragments`.

### LOW

- **LOW-1 | L67-70 | `log.warning` on the migration-gap fallback is fired on every successful key creation, in prod.** The fallback IS the happy path today (no migration), so every prod-issued key logs a `WARNING` line saying "column missing." This is log spam, and worse, it desensitizes operators to the warning. When the migration ships and the warning becomes meaningful, it will be ignored. **Fix:** demote to `log.debug`, or gate behind a one-shot flag (`_logged_first_displayed_at_missing` module-level bool).

- **LOW-2 | L385 | Path param `slug: str` with no length / charset validation.** The route is `@router.get("/markets/{slug:path}/consensus")` with the `:path` converter, which accepts slashes — a slug like `foo/../bar` is structurally permitted at the routing layer (FastAPI does not canonicalize). The slug is then passed to `db.get_predictions_for_market(slug)` (parameterized — no SQLi) and to `ttl_cache.delete(f"credibility_consensus:{slug}")` via the invalidator. A slug containing `:` would shadow the cache key namespace separator (`credibility_consensus:` prefix); not exploitable for cross-key reads but the cache key space is polluted. **Fix:** at the top of the handler, `if not re.match(r"^[a-z0-9-/]{1,200}$", slug): raise HTTPException(400, "invalid slug")`. Mirror `market_routes.py`'s slug pattern.

- **LOW-3 | L165, 305 | Docstring promises "malformed filters drop silently" — verified, but no log emit on drop.** Both handlers wrap `saved_views_schema.filters_from_query` and `build_where` in `try/except Exception` with `# pragma: no cover` markers. Silently swallowing means a misconfigured client cannot tell why their `categories=foo&min_credibility=garbage` returned unfiltered data. From a security angle the silent-drop is correct (no info leak about which filter parse failed), but operationally it makes debugging impossible. **Fix:** `log.info("v1_filter_parse_failed scope=sources key=%s err=%s", request.url.path, type(exc).__name__)` — log the exception type, not the value, to avoid leaking attacker-controlled input to logs.

- **LOW-4 | L460-497 | `/markets/edge` does not return a stable shape.** `m.to_dict()` (L495) emits whatever `UnifiedMarket` dataclass currently serializes — there is no `response_model=` on the route. A field rename in `unified_markets.py` silently changes the public API contract for paying customers. Other endpoints in this file at least interpolate a fixed dict literal (L184-200, L262-280). **Fix:** define a `pydantic.BaseModel` for the response and pin it via `response_model=V1MarketEdge` — FastAPI then strips unknown fields on the way out. Same pattern for the other endpoints would harden schema-validation, which the brief listed as a scope item.

### INFO

- **INFO-1 | Whole file | No `response_model=` on any of the 5 endpoints.** Without a pydantic model, FastAPI: (a) does not validate the response shape, (b) does not generate an accurate OpenAPI schema (the docs page describes "Any" return type), (c) does not strip extra fields that downstream `_compute` factories accidentally include. The `JSONResponse` wrapper bypasses the response-model machinery anyway, even if added later — to actually benefit, return the dict directly and let FastAPI serialize. **Fix:** add `response_model` to all five routes; remove `JSONResponse(...)` wrappers and return the dict.

- **INFO-2 | L156, 250, 290 | Cache hits return the same Python dict object across requests until expiry.** `ttl_cache.get_or_compute` returns the cached value by reference (see `gateway/cache/ttl.py` for the TTLCache implementation). If a future handler mutates the returned dict (e.g. adds a per-request `request_id`), it mutates the cached object and every subsequent reader sees that mutation until expiry. Not exploitable today (no mutation), but a known foot-gun. **Fix:** document this contract in the file header; ideally `ttl_cache.get_or_compute` should return `copy.deepcopy(value)` on hit.

---

## Pre-release notes (not counted in severity totals — per hard rule)

These observations would block a public-API GA but are out of scope for the pre-release audit:

- The endpoints have no OpenAPI examples, no `summary=` strings, and the docstrings do not document error responses (401, 404, 429). The OpenAPI tagger in `server.py:701-703` puts these under "Markets" / "Sources" / "Predictions" tags but a developer hitting the spec sees no example request/response. (For GA — not blocking pre-release.)

- `/markets/edge` calls upstream Polymarket + Kalshi inline on the request path; this should be backed by a scheduled job that writes to a `markets_edge_cache` table, with the API serving the precomputed snapshot. (Architecture-level — pre-release-only.)

- No telemetry on which endpoints are actually hit. `last_used_at` is incremented but there is no per-endpoint counter, no per-tier counter, no per-status-code breakdown. The `api_usage_hourly` rollup from migration `128_api_keys_ext.py` is referenced in that migration's docstring as "the public API middleware UPSERTs this row" — but this module is NOT that middleware and does not UPSERT into `api_usage_hourly`. Either (a) this module is missing the middleware integration, or (b) the migration's docstring describes a sibling module (`api_public/`). Worth a separate audit of `gateway/api_public/` to disambiguate. (Pre-release-only — observability gap, not a vuln.)

---

## Non-findings (verified clean)

- **CSRF on this router:** all five routes are GET only. `_CSRF_SKIP_PREFIXES` (`server.py:1148`) does not need to include `/api/v1` because CSRF middleware only triggers on state-mutating verbs (POST/PATCH/PUT/DELETE per `CSRF_PATCH_DELETE_ENFORCE` semantics at `server.py:1143`). No CSRF surface.

- **SQL injection via filter params:** verified. `filters_from_query` in `saved_views_schema.py:321` parses query params into a typed dict; `build_where` at L358 returns `?`-placeholdered SQL fragments with separate params list. The f-string interpolation at L221, L225, L347, L353 inlines only the WHERE / JOIN *text*, which is built from hard-coded constants in `saved_views_schema.py` (verified L391-419). MED-5 above flags this as a fragility concern, not an active vuln.

- **Parameterized queries everywhere else:** all DB calls in this file use `?` placeholders with separate param tuples. No string-formatted SQL anywhere.

- **No PII in responses:** the JSON bodies expose `source_handle`, `market_id`, credibility scores, prediction content, calibration arrays. No user emails, no user IDs, no IP addresses, no session tokens. The `_validate_key`'s row dict (which DOES contain user_id, key_hash, etc.) is discarded by every handler — confirmed at HIGH-4. Even if a handler accidentally returned `key`, the dict has `key_hash` (SHA-256) not the raw key, so the response leak risk is bounded.

- **Hardcoded creds / secrets:** none in this file. `KALSHI_API_BASE` is read from env at L478.

- **Type validation on path params:** FastAPI handles `limit: int`, `offset: int`, `resolved: Optional[int]` — non-numeric input is rejected with a 422 before the handler runs. `slug: str` is unconstrained (see LOW-2). `handle: str` and `category: Optional[str]` are unconstrained but the SQL is parameterized, so the surface is "ugly cache keys" not injection.

- **Auth on every endpoint:** `_validate_key(request)` is called as the first executable line of every handler — verified L167, L254, L309, L411, L468. No `if request.user.is_admin: ...` fast-path. No silent dev-mode bypass. The auth check itself has issues (HIGH-1, HIGH-2, HIGH-4) but it is structurally present.

- **Rate limit per validated key:** `limiter.check(f"apiv1:{row['id']}", ...)` at L134-136 fires on every authed request. The bucket is keyed by api_key.id, not user_id, so a user with 5 keys gets 5x the budget — intentional per the `_TIER_QUOTAS` design (see `audits/audit_api_keys_routes.md` for the matching concern on the management side). Verified.

---

*Audit run 2026-05-15 against `feature/platform-build` HEAD `7a443e0`. Re-run after the `first_displayed_at` migration lands, after `_validate_key` is hardened per HIGH-1/2/3/4, and after a `response_model=` pass on all five endpoints.*
