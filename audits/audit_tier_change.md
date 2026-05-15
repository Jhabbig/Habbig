# Adversarial audit — Tier-change flow (free → trader → pro / pro → trader)

**Scope:** every code path that mutates a user's effective tier and every downstream consumer that should observe the change. Specifically the entry points (`POST /billing/subscribe`, `POST /admin/users/{id}/grant`, the Stripe webhook branches `customer.subscription.created` / `customer.subscription.updated` / `invoice.paid`, the cancellation/pause/resume routes), the cache invalidation contract (`cache/ttl.py:ttl_invalidate.on_subscription_change`), session attachment (`auth/middleware.py`, `auth/guards.py:read_hardened_session`), and downstream gates (`subproduct_access.py`, `middleware/subproduct.py`, `subproduct.has_subproduct_access`, the 60s `dashboards:user:{id}` / `settings:user:{id}` / `signal_search:user:{id}` async caches, the 5-min `subproduct_access._verify_cache`).

**Date:** 2026-05-15
**Branch:** `feature/platform-build`
**Tip:** `ad2c463`
**Hard rule:** synchronous bash only. Pre-release files (`gateway/server.py:prerelease_page`, prerelease CSS) are off-limits and not re-read.
**Auditor focus:**
- Are caches invalidated on every tier-change write site?
- Are sessions re-bound to the new tier (token rotation, `request.state.user` re-read)?
- Are downstream sub-product permissions consistent with the new tier?

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 1     |
| High     | 4     |
| Medium   | 3     |
| Low      | 3     |
| Info     | 2     |

The dominant pattern: **tier-change writes are split across at least four files** (`server.py`, `billing_routes.py`, `stripe_webhook_routes.py`, `admin_routes.py`/`server.py` admin grant), and the cache-invalidation / downstream-bust hook is **only wired in two of them**. The result is a 60-second blast radius (async cache) plus a 5-minute blast radius (subproduct access) where a user is the wrong tier for paywalls, sidebar badges, dashboard cards, and the gated Signal Search nav item.

---

## Tier-change entry points (canonical map)

| Entry point | File:line | DB write | `ttl_invalidate.on_subscription_change` | `subproduct_access.invalidate_user` |
|---|---|---|---|---|
| `POST /billing/subscribe` (in-app upgrade UI; free→trader, trader→pro, pro→trader scheduled) | `server.py:4523` | direct `subscriptions` INSERT/UPDATE + `db.upsert_subscription` loop | **NO** | **NO** |
| `POST /admin/users/{id}/grant` (super-admin gift) | `server.py:6194` | `db.upsert_subscription` | **NO** | **NO** |
| Stripe `customer.subscription.created` | `stripe_webhook_routes.py:84 _grant_access` | direct `subscriptions` INSERT | **NO** | **NO** |
| Stripe `customer.subscription.updated` | `stripe_webhook_routes.py:126 _update_plan` | UPDATE `subscriptions.plan` + `.status` | **NO** | **NO** |
| Stripe `invoice.paid` (past_due → active flip) | `stripe_webhook_routes.py:166 _record_payment` | UPDATE `subscriptions.status` | **NO** | **NO** |
| Stripe `customer.subscription.deleted` | `stripe_webhook_hardening.py:272 apply_subscription_cancelled` → `_update_subproduct_status` | UPDATE both `subscriptions` *and* `users.subproduct_subscriptions` JSON | YES (via `_update_subproduct_status`, ttl.py:424) | YES (line 320) |
| Stripe `invoice.payment_failed` | `stripe_webhook_hardening.py:364 apply_invoice_payment_failed` → `_update_subproduct_status` | JSON blob status = past_due | YES | YES (line 385) |
| `POST /settings/billing/cancel` (in-app cancel) | `billing_routes.py:1007` | UPDATE `subscriptions.status = cancelled` | YES | NO |
| `POST /settings/billing/pause` | `billing_routes.py:1080` | UPDATE `users.subscription_paused_until` | YES | NO |
| `POST /settings/billing/resume` | `billing_routes.py:1108` | UPDATE `users.subscription_paused_until = NULL` | YES | NO |
| `POST /settings/billing/resubscribe` | `billing_routes.py:1128` | UPDATE `subscriptions.status = active` | YES | NO |
| `POST /settings/billing/addon/cancel` | `billing_routes.py:1286` | `db.set_trading_addon(uid, False, None)` | YES | NO |
| Nightly `reconcile_subscriptions` job | `jobs/reconcile_subscriptions.py:131` | JSON blob status/period_end | YES on drift | NO |
| `set_user_intelligence_addon` (queries) | `queries/subscriptions.py:456` | UPDATE `users.intelligence_addon_active` | YES (line 467) | NO |
| `db.upsert_subscription` (canonical helper) | `queries/subscriptions.py:97` | INSERT … ON CONFLICT … UPDATE | **NO** | **NO** |
| `db.cancel_subscription` (canonical helper) | `queries/subscriptions.py:138` | UPDATE status = cancelled | **NO** | **NO** |

**Three observations from the map:**

1. The **forward upgrade path is the worst-covered** quadrant: every route a paying customer actually touches (`/billing/subscribe`, admin grant, Stripe `subscription.created` and `subscription.updated`) writes the row and walks away without busting any cache. The well-covered paths are the negative direction — cancel / pause / payment failure — which is exactly backwards from a UX perspective (a user is willing to wait 60s to lose access; they will refresh five times if they paid and the page still shows "Locked").

2. The canonical write helpers (`upsert_subscription` / `cancel_subscription` in `queries/subscriptions.py`) do **not** call `ttl_invalidate.on_subscription_change`. The neighbouring helper `set_user_intelligence_addon` in the same file *does* (line 467). Centralising the bust at the query layer was clearly considered for one column and dropped for the others. Every caller now reimplements the invalidation contract, and three of them (the upgrade routes) get it wrong.

3. `subproduct_access.invalidate_user` is called by the negative Stripe webhook branches only. The in-process `_verify_cache` (5-minute TTL) is therefore stale on every positive entry too.

---

## Top findings

### 1. [CRITICAL] `POST /billing/subscribe` upgrades free→trader, free→pro, and trader→pro without busting any cache or invalidating the subproduct access cache

**Location:** `gateway/server.py:4523-4581` (the entire route body)

The route writes `subscriptions` rows directly and via `db.upsert_subscription`, then 302-redirects to `/billing`. No call to `ttl_invalidate.on_subscription_change`. No call to `subproduct_access.invalidate_user`. No session rotation.

Concrete blast radius after a user clicks "Upgrade to Pro" and the route returns 302:

- `dashboards:user:{id}` async cache (`server.py:4079`, TTL 60s): contains the **old** `subs_list`. The next render of `/dashboards` will paint locked cards on subproducts the user just paid for. Tested by reading the cache key: `subs = {s["dashboard_key"]: s for s in _cached["subs_list"]}` at `server.py:4081`.
- `settings:user:{id}` async cache (`server.py:7142`, TTL 60s): contains the old plan info. The `/settings/billing` page reads from it; the very page the user just submitted from now shows their old plan for up to a minute, which looks like a failed mutation and triggers a re-submit (rate-limited to 20/h by `_billing_rate_limit`, but the user still sees confusion).
- `feed:user_{id}:*` sync cache (`cache/ttl.py:19` schema): contains the old tier's feed filter. A trader→pro upgrade does **not** unlock the Pro-only feed categories until the TTL of the cached page expires (60s).
- `best_bets:*` sync cache: tier-scoped but not user-keyed. A pro→trader downgrade may still serve the user pro-shaped best-bets payloads for up to 60s. Less severe (read-only data, no auth bypass) but mis-aligned with the UI's "Locked" badge.
- Sidebar render: `pinfo` is computed from the cached `subs` blob (`server.py:4084`, `_user_plan_info(user, subs, now)`), so the Pro-only Signal Search nav item (`server.py:4163: if pinfo["plan"] == "pro" or is_admin_user`) is stale for 60s after either direction.

**Reproduction without running the server (sync bash):**
```
$ grep -nA2 "def billing_subscribe" ~/Habbig/gateway/server.py | head -40
$ grep -c "on_subscription_change" ~/Habbig/gateway/server.py
```
The grep returns `0` matches in `server.py` even though four mutation sites (`server.py:4523`, `:4604`, `:6194`, `:8061` area) write subscription rows.

**Fix:** call `ttl_invalidate.on_subscription_change(user["user_id"])` immediately before each `return RedirectResponse(...)` in the route, and call `subproduct_access.invalidate_user(user["user_id"])`. Better: move both calls into `db.upsert_subscription` (see Finding #3) so every future writer is correct by default.

---

### 2. [HIGH] Stripe webhook `subscription.created` / `subscription.updated` / `invoice.paid` do not bust any cache

**Locations:** `gateway/stripe_webhook_routes.py:84-123` (`_grant_access`), `:126-163` (`_update_plan`), `:166-186` (`_record_payment`)

`_grant_access` (the production path for an external Stripe checkout completing) writes:
```python
"INSERT INTO subscriptions "
"(user_id, dashboard_key, plan, status, started_at, "
" stripe_sub_id, source) "
"VALUES (?, ?, ?, 'active', ?, ?, 'stripe') "
"ON CONFLICT(user_id, dashboard_key) DO UPDATE SET ..."
```
and returns. No `ttl_invalidate`, no `invalidate_user`. The negative twin `apply_subscription_cancelled` at `stripe_webhook_hardening.py:272` does both — `_update_subproduct_status` triggers the bust at line 424, and an explicit `invalidate_user(user_id)` runs at line 320. The asymmetry is unintentional: the docstring at `stripe_webhook_hardening.py:34` claims "If the DB is unavailable they log + allow the event through so a transient issue doesn't amplify into missed webhooks" but says nothing about cache. The cache-invalidate calls were added when the cancellation path was wired and never back-ported to the creation path.

`_update_plan` is the worse miss: it intentionally collapses Stripe lifecycle states (`incomplete`, `past_due`, `unpaid`, etc.) to local `active|inactive`, which is exactly a tier-shaped change, and never invalidates.

`_record_payment` flips `past_due` → `active` on `invoice.paid`. The Pro user whose card was retried is locked out of Signal Search for up to 60s after the payment lands.

**Stripe retries** non-2xx responses for 3 days, so the lost invalidation will not be repaired by a retry — Stripe sees 200, never resends the event, and the local cache survives the full TTL on the original event.

**Fix:** add the same three-line block already used in `_update_subproduct_status` (`stripe_webhook_hardening.py:421-426`) to each branch:
```python
try:
    from cache import ttl_invalidate
    ttl_invalidate.on_subscription_change(user_id)
except Exception:
    log.exception("ttl_invalidate.on_subscription_change failed (user=%s)", user_id)
try:
    from subproduct_access import invalidate_user
    invalidate_user(user_id)
except Exception:
    pass
```

---

### 3. [HIGH] `db.upsert_subscription` and `db.cancel_subscription` do not bust the cache — every caller has to remember

**Location:** `gateway/queries/subscriptions.py:97-135` and `:138-144`

Compare with the sibling helper `set_user_intelligence_addon` at the same file (`queries/subscriptions.py:456-471`) which *does* call `ttl_invalidate.on_subscription_change` directly inside the helper:
```python
def set_user_intelligence_addon(user_id, active, period_end=None):
    with db.conn() as c:
        c.execute("UPDATE users SET intelligence_addon_active = ?, ...")
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_subscription_change(user_id)
    except Exception:
        logging.getLogger(__name__).exception(
            "ttl_invalidate.on_subscription_change failed (user=%s)", user_id,
        )
```
This is the pattern that should also live in `upsert_subscription`. Today eight distinct call sites (see "Tier-change entry points" table above) reimplement the invalidation contract, and three of them are wrong. The current state actively *encourages* future bugs: a future engineer adds `db.upsert_subscription` from a new handler, doesn't think about cache, ships a stale-UI bug.

Note: `upsert_subscription` already has a non-trivial side effect — it lazily imports `db_referrals` and calls `mark_referral_converted` (lines 128-135). The precedent for adding cross-module side effects in the helper is established.

**Fix:** lift the `ttl_invalidate` + `subproduct_access.invalidate_user` block from `set_user_intelligence_addon` to the top of `upsert_subscription` and `cancel_subscription`, with the same lazy-import + swallowed-exception pattern. Add a regression test in `tests/test_cache.py` along the lines of the existing `test_on_subscription_change_scoped_to_user` (line 257).

---

### 4. [HIGH] `subproduct_access._verify_cache` (5-minute) is never busted on upgrade — paid users can keep hitting 402

**Location:** `gateway/subproduct_access.py:53` (cache definition), `:188-265` (the dependency)

`_verify_cache` keys `(user_id, slug)` → `(expires_at, verdict)` with `_VERIFY_TTL_SECONDS = 300`. The cache **only stores positive verdicts under PRODUCTION=1** (line 235 gates the live-verify path; non-prod returns early at line 236). But the cache *also* holds **negative** verdicts (line 246-249 raises 402 on `cached is False`), produced when a stale entry returned `live != "active"` (line 257-258 stores `ok = (live == "active")`).

If a user gets a `past_due` verdict cached (Stripe says inactive), then in-app `/billing/subscribe` corrects the local row, the cache is **still negative** for up to 5 minutes. The user pays, hits the dashboard, gets a 402 detail "X subscription inactive". The only positive bust today is `apply_invoice_payment_failed` → `invalidate_user`, which is the *negative* direction.

`apply_subscription_cancelled` calls `invalidate_user` (correct). `apply_invoice_payment_failed` calls `invalidate_user` (correct). `_grant_access` / `_update_plan` / `_record_payment` / `/billing/subscribe` / admin grant — none of them call `invalidate_user`.

**Fix:** every positive write site must also call `subproduct_access.invalidate_user(user_id)`. If Finding #3 is taken (centralise in `upsert_subscription`) this is one extra line.

---

### 5. [HIGH] `_pro_or_better` in `subproduct_access.py` reads `subscription_tier` from the user row, but no production code path puts that field on the row

**Locations:** `gateway/subproduct_access.py:58-71` (`_pro_or_better`), `gateway/auth/guards.py:32-53` (`read_hardened_session`), `gateway/server.py:2100-2173` (`current_user`)

`_pro_or_better` returns True if `is_admin >= 1` **OR** `"pro" in user["subscription_tier"]` **OR** `"enterprise" in user["subscription_tier"]`. But:

- `read_hardened_session` (the session resolver attached to `request.state.user`) returns a dict with `user_id, username, email, is_admin, is_super_admin, admin_level, session_id, session_token_hash` — **no `subscription_tier` key**. Verified at `auth/guards.py:42-53`.
- `current_user` (the legacy resolver) returns the same key set — no `subscription_tier`. Verified at `server.py:2114-2154`.
- `_field(user_row, "subscription_tier", "")` therefore always returns `""`, so `"pro" in ""` is False, so non-admin Pro users **never** hit the fast-path bypass.
- `users.subscription_tier` is **not a real column** (the only migration referring to it is `migrations/060_subproduct_subscriptions.py:5`, a comment). The tests that pass `subscription_tier="pro"` in `tests/test_subproduct_access.py:41,46,124,130` exercise dead code: the field never appears on a real user row at runtime.

The downstream consequence is that Pro users' access to a subproduct `dashboard` route gated by `require_subproduct_access(slug)` (`subproduct_dashboard_routes.py:120-129`) **falls through to `has_subproduct_access`**, which only checks the `users.subproduct_subscriptions` JSON blob. The JSON blob is written by the Stripe webhook for **subproduct-scoped checkouts** but **not** for the Pro bundle path. A user who buys Pro via `/billing/subscribe` or via a Stripe `subscription.created` with `metadata.plan='pro'` ends up with 12 `subscriptions` rows (one per dashboard, see `server.py:4537`) but **zero entries** in `users.subproduct_subscriptions`. Their access to `/dashboard/sports`, `/dashboard/crypto`, etc. is denied with 402 unless they happen to be `is_admin`.

This is partly mitigated by the parallel `subproduct.has_subproduct_access` (a different function, same name, at `gateway/subproduct.py:355`), which routes through `db.has_active_subscription` — that one *does* see the `subscriptions` rows. So legacy `/dashboards`-card routes work fine; the new `require_subproduct_access` dependency at `subproduct_dashboard_routes.py` is the one that breaks.

**Fix:** either (a) hydrate `subscription_tier` onto the session-resolved user dict in `read_hardened_session` by calling `db.get_user_subscription_tier(user_id)` (cheap; one SELECT), or (b) change `_pro_or_better` to call `db.get_user_subscription_tier` directly when the field is absent. Option (a) keeps the request-scope user dict self-contained. Option (b) avoids the extra session-attach work for paths that never need tier.

---

### 6. [MEDIUM] Sessions are not rotated on tier change — a tier-leaking session token (if it ever stores tier) would survive a downgrade

**Locations:** `gateway/auth/middleware.py:52-69`, `gateway/auth/guards.py:32-53`, `gateway/stripe_webhook_hardening.py:296-304`

Today the session blob contains only `user_id` + admin level (see Finding #5), so a "stale tier on a session" leak is not a present-tense bug — tier is re-derived per request via `db.get_user_subscription_tier`. But:

- The cancellation path *does* revoke all sessions (`stripe_webhook_hardening.py:296-304`, best-effort across three table names). The intent ("so they can't keep using stale cookies") only makes sense if cookies carried tier state. They don't, so the revocation looks like a leftover from an earlier session design and is now mostly cosmetic.
- The downgrade path (`/billing/subscribe` pro→trader at `server.py:4551-4570`) does **not** rotate the session. So if Finding #5 is fixed by hydrating `subscription_tier` onto the session dict, the dict will then be a stale-tier carrier for the rest of the session lifetime — exactly the bug `apply_subscription_cancelled` was trying to prevent.

**Fix (predicated on adopting Finding #5 option (a)):** on every tier change, force a re-read of the session by either rotating the cookie or (cheaper) by clearing any in-process per-request memoization of the user dict. Today `SessionMiddleware.dispatch` (`auth/middleware.py:62-68`) calls `attach_session_to_request` once per request, so re-issuing the cookie on the next request is sufficient — but only if the session row is also updated. Either way: if Finding #5 fix puts `subscription_tier` onto the session blob, also stamp `subscription_tier_version` and bump it on every tier change, and re-read on every request when the version changed.

---

### 7. [MEDIUM] `_user_plan_info` is computed from the cached `subs` dict, so the sidebar Pro badge / Signal Search link can be wrong for 60s

**Location:** `gateway/server.py:4063-4084` (cache fetch), `:4084` (`pinfo = _user_plan_info(user, subs, now)`), `:4163` (Signal Search gating on `pinfo["plan"] == "pro"`)

```python
_cached = await _async_cache.get_or_set(
    f"dashboards:user:{user_id}", _build_dashboards_data, ttl_seconds=60,
)
subs = {s["dashboard_key"]: s for s in _cached["subs_list"]}
...
pinfo = _user_plan_info(user, subs, now)
```
`pinfo` drives:
- Card badges ("Active" vs "Locked") (`server.py:4093-4097`).
- Card link target ("/dashboards" vs "/billing?dashboard={key}") (`server.py:4099-4111`).
- Signal Search nav-item visibility (`server.py:4163`).
- Sidebar role badge via `_role_badge(user)` (`server.py:4165`) — note this reads `user`, not `pinfo`, so the role string itself is fine, but the tier-gated nav surface around it is wrong.

For 60 seconds after `/billing/subscribe` returns, the user is one F5 from seeing all of these out of sync with reality. Finding #1's fix (call `ttl_invalidate.on_subscription_change` from the route) closes this.

---

### 8. [MEDIUM] Admin grant route shares the same blind spot

**Location:** `gateway/server.py:6194-6221` (`/admin/users/{user_id}/grant`)

`db.upsert_subscription(...)` is called from a super-admin handler, but there is no `ttl_invalidate` or `invalidate_user`. The admin's UI doesn't render the target user's pages (it would be impersonation), so this is only externally visible if the admin then re-impersonates the target user to confirm the grant landed — and the impersonation-rendered dashboards page reads from `dashboards:user:{target_id}` like any other request and shows the pre-grant state for 60s. Low-impact for admin UX but a true correctness bug.

Same fix as Finding #1: one line, or (better) Finding #3 centralises it.

---

### 9. [LOW] `subproduct_access._verify_cache` is module-global with `Lock()`, so unit tests sharing the import bleed verdicts across `TestCase` instances

**Location:** `gateway/subproduct_access.py:53-55`

Not directly a tier-change bug, but compounding: when Finding #4 is fixed by adding `invalidate_user` calls, the test harness needs a way to clear the cache between tests. Today `_verify_cache` is module-level (`dict[tuple[int,str], tuple[float,bool]]`) — a test that exercises Pro upgrades will leak verdicts into the next test. Add `invalidate_user(0)`-style global flush or expose `_verify_cache.clear()` for tests.

---

### 10. [LOW] `apply_subscription_cancelled` revokes sessions in *all three* possible table names (best-effort) but no positive write site does the inverse

**Location:** `gateway/stripe_webhook_hardening.py:296-304`

```python
for table in ("narve_sessions", "sessions", "user_sessions"):
    try:
        c.execute(
            f"UPDATE {table} SET revoked = 1 "
            f"WHERE user_id = ? AND COALESCE(revoked, 0) = 0",
            (user_id,),
        )
    except Exception:
        pass
```
The schema in production is `user_sessions` (per `migrations/007_user_sessions_hardening.py`). The other two table names appear to be defensive placeholders for unrolled-out migrations or earlier names. Either way, the multiplexed revoke is dead code for two of three names. If session rotation is added on tier change (Finding #6 fix), it should target `user_sessions` only.

---

### 11. [LOW] `cancel_subscription` and `upsert_subscription` mutate inside a `with db.conn()` block but emit the cache bust **after** the block

**Location (proposed fix to Finding #3):** `gateway/queries/subscriptions.py:97-135`

Worth flagging for the fix patch: the cache invalidation must happen **after** the transaction commits (i.e. after the `with db.conn()` block closes), otherwise a concurrent reader can populate the cache *with the pre-commit value* between the invalidation call and the actual visibility of the new row. `set_user_intelligence_addon` (line 456) gets this right: the `ttl_invalidate` call sits outside the `with` block. Any patch lifting that pattern to `upsert_subscription` must preserve the ordering.

---

### 12. [INFO] The downgrade scheduling logic at `server.py:4651-4666` plants a future-dated `__plan__` row but never schedules an invalidation for the cutover time

**Location:** `gateway/server.py:4651-4666`

When a pro user downgrades, the route inserts a `__plan__` row with `started_at = pro_end` (the future timestamp when Pro coverage ends). At `pro_end + 1s`, the user's effective tier silently shifts from pro to trader because `get_user_subscription_tier` (`queries/subscriptions.py:474`) computes tier from currently-active rows. No background job or scheduled invalidation drops the user's cached `dashboards:user:{id}` / `settings:user:{id}` at that moment. The user's cards will keep saying "Active" on Pro-only dashboards for up to 60s past the cutover, and Signal Search will keep appearing in the sidebar.

The 60s drift is benign for read-only surfaces but means a Pro→Trader scheduled downgrade hits a brief window where the user can hit a Pro-only API and get 200 (the API doesn't use the cache, it re-checks `db.has_active_subscription`), then the UI swaps to "Locked" — looks like a flaky paywall to the user. The nightly `reconcile_subscriptions` job will fix it after the next run.

Not a critical bug, but worth a `schedule_at = pro_end` task that calls `ttl_invalidate.on_subscription_change(user_id)` at the cutover. The existing scheduled-jobs registry (`gateway/jobs/registry.py`) is the appropriate place.

---

### 13. [INFO] Feature flags re-resolve tier per call — no stale tier on `is_feature_enabled`

**Location:** `gateway/features.py:57-72` (`_user_tier` → `db.get_user_subscription_tier`)

Each call to `is_feature_enabled` re-reads the tier through `db.get_user_subscription_tier`. That helper hits SQLite directly, doesn't go through any of the user-keyed caches, and reflects the current `subscriptions` table immediately. Feature flags therefore correctly reflect a new tier on the request right after `/billing/subscribe` commits. The bug is everything *else* that caches.

---

## Gaps (consolidated, ordered by urgency)

1. **`/billing/subscribe` does not invalidate caches on free→trader, free→pro, or trader→pro upgrades, nor on the pro→trader downgrade.** Critical. Two missing one-liners (`ttl_invalidate.on_subscription_change` and `subproduct_access.invalidate_user`) per branch.
2. **Stripe `subscription.created` / `subscription.updated` / `invoice.paid` webhooks do not invalidate caches.** High. The negative branches (`subscription.deleted`, `invoice.payment_failed`) do — the asymmetry must be closed.
3. **`db.upsert_subscription` and `db.cancel_subscription` should bust the cache themselves**, matching the sibling pattern at `set_user_intelligence_addon`. High. Centralising the contract removes the entire class of "future writer forgot to invalidate" bugs.
4. **`subproduct_access._verify_cache` (5-min) is never busted on positive tier changes**, so a paid user can keep seeing 402 from the in-process cache for up to 300 seconds. High.
5. **`_pro_or_better` checks a `subscription_tier` field that never exists on a real user row.** Non-admin Pro users fall through to a JSON-blob check (`has_subproduct_access`) that is only populated by the subproduct-scoped checkout path, not by `/billing/subscribe` Pro bundle. High. Hydrate the field on the session-resolved user dict, or rewrite the fast-path to call `db.get_user_subscription_tier` directly.
6. **Sessions are not rotated on tier change.** Medium today (sessions don't carry tier), high if Finding #5 fix puts tier on the session.
7. **`_user_plan_info` reads from the cached `subs` blob**, so the sidebar Pro badge and Signal Search nav item are wrong for 60s after every upgrade/downgrade. Medium. Closed by fixing #1.
8. **Admin grant route (`/admin/users/{id}/grant`)** has the same blind spot as `/billing/subscribe`. Medium.
9. **Scheduled pro→trader cutover has no invalidation hook at the cutover timestamp.** Info. Add to the jobs registry.

**Summary blast radii at HEAD (ad2c463):**
- `/billing/subscribe`: 60s on dashboards/settings/sidebar + 5m on subproduct gate.
- Stripe `subscription.created`: same, plus the user is locked into 402 on every protected subproduct route for whichever of (60s, 5m) is later.
- Admin grant: 60s on dashboards/settings; user sees no immediate change in their tabs.
- Pro→Trader scheduled downgrade: brief window at the cutover where UI lags reality by up to 60s.

The fix is small — call two helpers from the existing canonical write helpers in `queries/subscriptions.py` and forward the bust through the Stripe `subscription.created`/`updated`/`paid` branches. Test coverage in `tests/test_cache.py:257` already exercises the `on_subscription_change` helper; the missing tests are integration tests for `/billing/subscribe` and the Stripe webhook branches asserting the cache is dropped post-handler.
