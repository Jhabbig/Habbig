# Audit — Router registration in `gateway/server.py`

Scope: every router / module that contributes HTTP routes to the FastAPI `app` constructed at `gateway/server.py:534`. Three registration patterns coexist:

1. **Explicit `app.include_router(...)`** — 4 call sites total.
2. **`module.register(app)`** — 19 call sites; each module installs its handlers via `app.add_api_route` or `@app.<verb>(...)` decorators inside its own `register()` body.
3. **Side-effect-of-import** — module is imported and its top-level `@app.get/@app.post` decorators bind to the live `app`. Used for `server_features`, `affiliate_routes`, `forecast_routes`, `status_routes`, `take_routes`, `embed_routes`, `push_routes`, `offline_routes`, `admin_jobs_routes`, `admin_health_monitor_routes`, `admin_cost_alerts_routes`, `admin_test_emails_routes`, `admin_emails_routes`, `admin_integrations_routes`, `billing_routes`, `stripe_webhook_routes`, `engagement_routes`, `feedback_routes`.

This audit focuses on the four `app.include_router(...)` call sites and the contracts the FastAPI router itself can validate (prefix, tags, responses, schema visibility, ordering). Cross-checked with `gateway/api_v1.py`, `gateway/api_public/`, `gateway/routes_referrals.py`, `gateway/routes_sharing.py`, `gateway/og_routes.py`, `gateway/search_routes.py`, `gateway/server_features.py`, `gateway/take_routes.py`, `gateway/affiliate_routes.py`, `gateway/billing_routes.py`, `gateway/forecast_routes.py`.

## Severity tally

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 4 |
| Low      | 5 |
| Info     | 3 |

No double-mount of the same `APIRouter` instance. No conflicting registrations of an identical path on the same HTTP method via `@app.<verb>` decorators. The defects below are documentation / OpenAPI / mixed-auth issues that survive because FastAPI does not enforce them.

---

## Inventory — `app.include_router(...)` call sites

| Line | Router source                       | Prefix              | Tags                  | `responses=` schema | `include_in_schema` override |
|------|-------------------------------------|---------------------|-----------------------|---------------------|------------------------------|
| 6418 | `api_public.router`                 | `/api/public/v1`    | `["public-api-v1"]`   | none                | per-handler default          |
| 8200 | `api_v1.router`                     | `/api/v1`           | `["v1"]`              | none                | per-handler default          |
| 8470 | `routes_referrals.router`           | (none — apex)       | none                  | none                | per-handler default          |
| 8481 | `routes_sharing.router`             | (none — apex)       | none                  | none                | per-handler default          |

Side-channel: `og_routes.register(app)` (line 8484) internally does `app.include_router(router)` with no prefix and no tags — equivalent to a fifth bare include.

---

## Findings (severity-sorted)

### 1. [MEDIUM] `/api/search` registered twice — order-dependent shadowing

`search_routes.register(app)` (`server.py:8204`) calls `app.add_api_route("/api/search", unified_search, methods=["GET"], include_in_schema=False)` at `search_routes.py:701`.

`server_features` is imported four lines later (`server.py:8210`) and at `server_features.py:948` declares `@app.get("/api/search") async def api_search(...)`.

Both register a GET on the same concrete path. FastAPI does not raise — it appends both to `app.router.routes` and returns the **first** match. The comment at `server.py:8192-8197` flags this is intentional ("first-match wins — putting mine first lets the palette endpoint shadow the legacy one"), but the legacy handler is a different response shape and is still reachable via test harness route iteration, `importlib.reload` (used elsewhere in this file), or anything that inspects `app.routes`. The "legacy handler stays" comment is true but misleading — the route remains in the routing table and would resurface if `search_routes.register` ever fails (the `try/except` around it at `server.py:8198-8206` swallows registration errors and logs a warning, so a typo in `search_routes` silently demotes the palette endpoint without anyone noticing).

Fix: explicitly delete the `@app.get("/api/search")` decorator from `server_features.py`, or move it behind `if False:` with a deprecation comment. Keep the helper function for in-process callers.

File: `gateway/search_routes.py:701`; conflict at `gateway/server_features.py:948`.

### 2. [MEDIUM] `/api/v1` namespace mixes Bearer-token public API with session-cookie admin endpoints

`api_v1.router` is mounted at `/api/v1` with its handlers calling `_validate_key()` (Bearer-token auth, `api_v1.py:110-150`). But four other modules register `@app.get|post|patch|delete("/api/v1/...")` directly on the same prefix using **session cookies + admin guard** (`_require_admin` / `_require_active_affiliate`), bypassing the Bearer-token contract:

- `take_routes.py:195-415` — `/api/v1/markets/{slug}/takes`, `/api/v1/takes/*` (session)
- `take_routes.py:605-622` — `/api/v1/admin/takes/{take_id}/delete`, `/api/v1/admin/reports/{report_id}/resolve` (session + admin)
- `affiliate_routes.py:330-428` — `/api/v1/affiliate*` (session + affiliate guard)
- `billing_routes.py:1102-1132` — `/api/v1/billing/*` (session, `include_in_schema=False`)
- `forecast_routes.py:62-116` — `/api/v1/forecasts/*` (session)

The FastAPI app advertises `/api/v1/*` as Bearer-token endpoints in `/api/docs` / `openapi_tags` (`server.py:553-564`). A consumer of `/api/openapi.json` that grabs an API key and tries to hit `/api/v1/markets/{slug}/takes` with `Authorization: Bearer ...` gets 401 (no session cookie) — the schema implies a uniform auth model that does not exist. Subscription endpoints (`/api/v1/billing/portal`) live next to public Bearer endpoints under the same prefix.

This is a docs/OpenAPI lie, not an auth bypass — every endpoint enforces its own guard. But it makes external SDK generation impossible against the current schema.

Fix: move the session-cookie endpoints under a different prefix (e.g. `/api/internal/v1/...`) or set `include_in_schema=False` consistently on every non-Bearer `/api/v1/*` route. Currently only the billing endpoints honor this; `take_routes`, `affiliate_routes`, and `forecast_routes` leak session-cookie endpoints into the published OpenAPI under the v1 tag.

File: `gateway/api_v1.py:32`; bleed from `gateway/take_routes.py:195-622`, `gateway/affiliate_routes.py:330-428`, `gateway/billing_routes.py:1102-1132`, `gateway/forecast_routes.py:62-116`.

### 3. [MEDIUM] `routes_referrals` and `routes_sharing` mount on the apex with no `prefix`, no `tags`, no `responses` — every endpoint is uncategorized in OpenAPI

`server.py:8470` does `app.include_router(_referrals_router)` and line 8481 does `app.include_router(_sharing_router)`. Both routers are instantiated at `routes_referrals.py:38` and `routes_sharing.py:53` with the bare `APIRouter()` constructor — no `prefix`, no `tags`, no `responses`, no `dependencies`. The 10+ paths each router mounts (`/invite/{code}`, `/api/invite/{code}`, `/api/invite/{code}/accept`, `/settings/referrals`, `/api/referrals/me`, `/leaderboard`, `/api/leaderboard*`, `/s/m/{token}`, `/s/s/{token}`, `/s/p/{token}`, `/og/shared/*`, `/tools/card-preview`, `/api/tools/card-preview`, `/api/share/*`, `/settings/invites`, `/api/invites/me`) all land in the published OpenAPI document with `tags: []` (i.e. under the schema's "default" bucket).

The app declares ten `openapi_tags` in `server.py:553-564` ("Public API v1", "Predictions", "Markets", "Sources", "Feed", "Usage", "Embeds", "Account", "AI", "Health") but **none of the four `include_router` call sites use any of those tags**:

- `api_public.routes:33` uses `["public-api-v1"]` — not in `openapi_tags`.
- `api_v1.py:32` uses `["v1"]` — not in `openapi_tags`.
- `routes_referrals` / `routes_sharing` use nothing.

Result: every group on the schema page gets either the wrong heading (tag the schema doesn't know how to render) or no heading at all. The `openapi_tags` block in `server.py` is purely cosmetic right now.

Fix:
- Add `prefix="/api/referrals"` to `routes_referrals.router` for the `/api/*` endpoints (keep the public-page paths bare); or split the router into two — one prefixed, one apex.
- Add `tags=["Account"]` / `tags=["Sharing"]` to both routers.
- Align the declared `openapi_tags` keys with the strings the routers actually use (`Public API v1` vs `public-api-v1`).

File: `gateway/routes_referrals.py:38`; `gateway/routes_sharing.py:53`; tag declarations at `gateway/server.py:553-564`.

### 4. [MEDIUM] Zero `responses=` schemas anywhere on the four included routers — OpenAPI cannot document non-200 envelopes

None of the four `app.include_router(...)` call sites or the routers they mount declare `responses=` either at the `APIRouter(...)` constructor or on individual `@router.<verb>` handlers. Every endpoint that 401s (`api_v1._validate_key`, `api_public/auth.verify_api_key`), 404s (`v1_source_detail`, `v1_market_consensus`), 410s (`get_api_key_raw` guard), or 429s (rate limiter) only documents the 200 happy path. Consumers reading `/api/openapi.json` get no schema for the JSON error envelope the gateway actually returns (the one produced by `error_handlers.py` — `{status, slug, message, request_id, ...}`).

This is the single largest gap in the documented public-facing API surface.

Fix: define a shared `ErrorEnvelope` Pydantic model in `error_handlers.py` (or a new `api_public/schemas.py`), then attach a `responses={401: {"model": ErrorEnvelope}, 404: ..., 429: ...}` dict on each `APIRouter(...)` constructor. `api_v1.py:32` and `api_public/routes.py:33` are the priority — they're the only Bearer-token external surface.

Files: `gateway/api_v1.py:32`, `gateway/api_public/routes.py:33`.

### 5. [LOW] Catch-all `/{full_path:path}` is mounted at line 8516 — every router declared after this point is unreachable via HTTP

`server.py:8516-8536` declares the catch-all proxy. Routes registered later in the file (lines 8542+ is `@app.websocket("/{full_path:path}")`) cannot serve HTTP requests at narrower paths — the catch-all swallows them. Today the only `@app.<verb>` after the catch-all is the WebSocket handler, so this is fine. But the entire mount graph relies on every future router landing **above** line 8516, and the file is 8679 lines long with no marker comment that the bottom of the include block is a hard boundary. Several block comments above do warn about this ("MUST land before the catch-all"), but the boundary itself is not flagged.

Fix: add a banner comment + a startup assertion that walks `app.router.routes` and refuses to launch if any non-catch-all route is mounted after the catch-all index.

File: `gateway/server.py:8513-8516`.

### 6. [LOW] `try/except: log.warning` around every `register(app)` swallows registration failures silently

`server.py` wraps every mount in `try / except Exception as _exc: log.warning(...)` (e.g. lines 8198-8206, 8225-8232, 8275-8282 — twenty-plus instances). A typo in `take_routes.py` that breaks the module's import would log a single warning at startup and the entire feature surface (community takes, take voting, admin moderation) goes missing from the running app. Nothing in `/health` notices.

This is intentional defensive isolation (one broken feature shouldn't take down the gateway) but it means router registration failures are invisible in production unless someone tails the logs at boot.

Fix: surface a `registered_modules` counter on `/health` and have the deploy gate refuse to roll out if the count drops vs the previous deploy. Already partially addressed by `health_monitor` for the subproducts; the gateway's own modules deserve the same treatment.

File: `gateway/server.py:6325-8510` — every `try/except` wrapper in the mount block.

### 7. [LOW] `og_routes.register(app)` quietly wraps `app.include_router` — fifth bare-include hidden behind the `register()` convention

`og_routes.py:175-182` defines `register(app)` and inside it calls `app.include_router(router)`. The router itself (`og_routes.py:38`) is bare `APIRouter()` — same prefix/tags/responses gap as finding #3. From a reader's perspective it looks like an apex-mounted module similar to `subproduct_signup_routes`, but the implementation path is different. Consistency-only — no security impact.

Fix: either inline the `include_router` at the call site in `server.py` so all five bare includes are visible together, or push every router-style module through `register()` so the convention is uniform.

File: `gateway/og_routes.py:175-182`.

### 8. [LOW] No deduplication check before `_search_routes.reload(...)` — pytest's module-cache reuse re-registers routes on the live `app`

`server.py:8198-8206` (and the eleven similar reload-safe blocks for `server_features`, `affiliate_routes`, `forecast_routes`, etc., lines 8210-8450) explicitly `importlib.reload` modules whose routes were installed by `@app.get/@app.post` decorators. Reload re-runs the decorators against the **same** `app` object. FastAPI does not de-duplicate — every reload appends a fresh copy of every route. After two test runs in the same process, every `@app.get` handler appears twice in `app.routes`; after ten runs, ten copies.

The first-match-wins semantics mean the runtime behavior is correct, but `app.routes` grows unboundedly. `/openapi.json` will list duplicate operation IDs (FastAPI normally raises on collision; the catch-all uses `include_in_schema=False` so it's exempt, but the duplicates from reload are visible).

In production this never fires because `importlib.reload` is gated on the module already being in `sys.modules` (which it isn't at first-boot). In test runs that exercise `app = TestClient(server.app)` after a module-cache flush, the duplication is real. The conftest in `gateway/tests/conftest.py` (modified — `git status` flagged it earlier) should be the authoritative pattern.

Fix: before re-running `importlib.reload`, clear all routes whose endpoint module matches the reloaded module (`app.router.routes = [r for r in app.router.routes if getattr(r.endpoint, "__module__", "") != mod.__name__]`). Or — simpler — make `register()` the canonical entry and drop the reload-on-import dance entirely.

File: `gateway/server.py:8198-8450`.

### 9. [LOW] Tag string mismatch — `openapi_tags` block declares 10 tags, none of which are used by any mounted router

The `openapi_tags` array at `server.py:553-564` declares: `"Public API v1"`, `"Predictions"`, `"Markets"`, `"Sources"`, `"Feed"`, `"Usage"`, `"Embeds"`, `"Account"`, `"AI"`, `"Health"`. The routers in scope tag their endpoints with: `"public-api-v1"`, `"v1"`, `"referral_invite"`, or nothing. Zero overlap. The schema page renders the declared `openapi_tags` group headers but no endpoint maps to them, while every actual endpoint sits in an undocumented `default` / `public-api-v1` / `v1` bucket beneath.

Fix: rename `api_public.routes:33` tag to `"Public API v1"`, `api_v1.py:32` tag to one of the published group names, and either add `"Sharing"` / `"Referrals"` to `openapi_tags` or retag those routers under existing buckets.

File: `gateway/server.py:553-564`.

### 10. [INFO] Three `app.include_router` calls are unguarded against duplicate prefix mounts

FastAPI permits two routers with overlapping or identical prefixes to coexist (first-match resolution). Today no two routers in the gateway share an `APIRouter` prefix — `api_public.router` is `/api/public/v1`, `api_v1.router` is `/api/v1`, and the two `routes_*` routers are apex. But the `@app.<verb>("/api/v1/...")` direct-decorator routes from `take_routes`, `affiliate_routes`, `billing_routes`, `forecast_routes` interleave with the `api_v1.router` prefix and are functionally a second router on the same prefix (see finding #2). Worth treating as a registration invariant — single `APIRouter` per prefix — and enforcing in a startup check.

Fix: emit a warning at boot if any path registered via `@app.<verb>` matches an `APIRouter` prefix already mounted.

### 11. [INFO] `webhooks.register_with_hub()` is called inside the same `try/except` block as `webhooks_routes.register(app)` — a routing failure and a hub-bridge failure surface identically

`server.py:6416-6440` mounts `api_public.router`, `api_keys_routes.register(app)`, `webhooks_routes.register(app)`, and `webhooks.register_with_hub()` in four separate `try/except` blocks. Each catches `Exception` and logs at `exception` level. The hub-bridge call (`register_with_hub`) does not install HTTP routes — it wires a callback into the realtime hub. A reader scanning this section would naturally assume the four blocks are all router mounts. Cosmetic / readability only.

Fix: rename the `register_with_hub` block's log message to mention "bridge" not "router".

File: `gateway/server.py:6435-6440`.

### 12. [INFO] No assertion that `/api/openapi.json` contains every documented endpoint group

The app exposes `openapi_url="/api/openapi.json"` (`server.py:571`) and references it from `/api/docs`. Findings #2-#4 + #9 mean that the schema is currently mis-tagged, missing error envelopes, and pollutes the v1 namespace with non-Bearer endpoints. A snapshot test that asserts the schema's `tags`, the `responses` shape for the 4xx codes, and the auth declared per group would have caught all four of those issues at PR time.

Fix: add `gateway/tests/api/test_openapi_schema.py` with snapshot assertions on the served schema. Already partial coverage in `gateway/tests/integration/test_api_v1.py` (Bearer auth path) but no schema-shape coverage.

---

## Top 3 (impact-ordered)

1. **`/api/search` is mounted twice** with shadowing. Order-dependent — a load failure in `search_routes` silently demotes the new palette handler. (Finding #1.)
2. **`/api/v1` namespace mixes auth models** — Bearer-token public API endpoints live next to session-cookie admin/affiliate/billing endpoints under the same prefix. The published OpenAPI advertises a uniform Bearer contract that does not exist. (Finding #2.)
3. **Zero `responses=` schemas + tag mismatch** across all four `app.include_router(...)` mounts. Every external consumer of `/api/openapi.json` gets an incomplete contract — no 4xx envelope shapes, no working tag grouping. (Findings #3, #4, #9.)
