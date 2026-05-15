# Adversarial audit: `gateway/subproduct.py` + `gateway/middleware/subproduct.py`

Date: 2026-05-15
Scope: host-header validation, subscription-check bypass via direct-origin,
CF-Connecting-IP enforcement in production, slug-to-subdomain mapping
consistency, expired-subscription read-blocking, feature-flag-driven gating.
Files reviewed:

- `/Users/shocakarel/Habbig/gateway/subproduct.py`
- `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py`
- `/Users/shocakarel/Habbig/gateway/subproduct_access.py`
- `/Users/shocakarel/Habbig/gateway/subproduct_signup_routes.py`
- `/Users/shocakarel/Habbig/gateway/subproduct_dashboard_routes.py`
- `/Users/shocakarel/Habbig/gateway/pwa_middleware.py`
- `/Users/shocakarel/Habbig/gateway/server.py` (proxy_request, middleware
  registration, host helpers, get_subdomain)
- `/Users/shocakarel/Habbig/gateway/realtime/channels.py`
- `/Users/shocakarel/Habbig/gateway/features.py`
- `/Users/shocakarel/Habbig/gateway/queries/subscriptions.py`
- `/Users/shocakarel/Habbig/gateway/config.json`

No code changes. Findings only.

---

## Severity legend

- C  Critical (immediate production exploit, paywall bypass, or RCE)
- H  High    (exploit needs minor preconditions; significant impact)
- M  Medium (defence-in-depth gap; bypassable with care or staging-only)
- L  Low    (hygiene / hardening / contradicted comments)
- I  Informational

## Severity counts

- Critical: 0
- High:     2
- Medium:   5
- Low:      4
- Informational: 3

Total: 14 findings.

---

## Top 3 (rank-ordered)

1. **H-1** — `realtime/channels.py::_has_subproduct_access` passes
   `user.get("user_id")` (an `int`) where `subproduct_access.has_subproduct_access`
   expects a user-row dict. Result: every `subproduct:{slug}` WebSocket
   subscription is **always denied**, including for admins and Pro users.
   Realtime feed is dead on subproduct channels in production.

2. **H-2** — `SubproductMiddleware` trusts the `CF-Connecting-IP` header by
   presence only. It does NOT validate that `request.client.host` is a
   loopback / trusted peer before honoring the header. If the origin process
   ever becomes externally reachable (firewall rule slip, dev `--host 0.0.0.0`
   while `PRODUCTION=1`, etc.) an attacker can defeat the CF-origin check by
   adding a single header. The trusted-peer check in `_get_client_ip`
   (server.py:1731) shows the project already understands this pattern;
   the subproduct middleware just doesn't apply it.

3. **M-1** — `subproduct.py::subproduct_for_host` (the standalone helper)
   does NOT validate the FQDN suffix. Any host whose first label is
   `crypto`/`sports`/etc. — `crypto.evil.com`, `crypto.localhost.attacker`,
   even a forged `Host: crypto.evil` — resolves to the crypto subproduct.
   In production this is masked by `SubproductMiddleware`'s allowlist
   (returns 400 for unknown hosts), but the helper is also called from
   `pwa_middleware.py:238` and `server.py:3251` etc., and any future caller
   that runs **before** the middleware (or in a process that didn't install
   `SubproductMiddleware`) inherits the loose check. The duplicate
   implementation in `middleware/subproduct.py:90` IS strict.

---

## Findings

### H-1 — Realtime subproduct channels always deny (type mismatch)

File: `/Users/shocakarel/Habbig/gateway/realtime/channels.py:48-55`

```python
def _has_subproduct_access(user: dict, slug: str) -> bool:
    try:
        from subproduct_access import has_subproduct_access as _check
        return bool(_check(user.get("user_id"), slug))
    except Exception:
        return False
```

`subproduct_access.has_subproduct_access(user_row, slug)` (at
`gateway/subproduct_access.py:100`) expects a user **row** (dict /
sqlite3.Row), not the user_id `int`. With an int passed in:

- `_pro_or_better(123)` → `_field(123, "is_admin", 0)` triggers the
  `except (KeyError, IndexError, TypeError)` arm of `_field`, returns the
  default `0`; same for `subscription_tier` (returns `""`). Returns False.
- `_blob_entry(123, slug)` does `_field(123, "subproduct_subscriptions", "")`
  → returns `""`, which is falsy → function returns None.
- Final return: False.

Impact: every authenticated user (including super-admins with
`is_admin=2` and Pro subscribers) is denied access to `subproduct:{slug}`
WebSocket channels. The test in `tests/test_realtime.py:87` only patches
`_has_subproduct_access` to a constant, so this bug doesn't show up there.

Fix: pass the full user row (or refactor the helper to fetch by id).

Severity: **High** — broken feature, not a paywall bypass, but a complete
silent failure of a documented capability.

---

### H-2 — `CF-Connecting-IP` enforcement does not validate client peer

File: `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:126-136`

```python
if _is_production():
    if not request.headers.get("cf-connecting-ip"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
```

The middleware checks the header is **present**; it does not check the
TCP peer's address. The server.py-level `_get_client_ip` (line 1731)
correctly gates trust on `request.client.host in _TRUSTED_PROXY_HOSTS`
(loopback only). The subproduct middleware does not.

Threat model: today the gateway listens on 127.0.0.1 behind a Cloudflare
Tunnel, so off-tunnel hits are not network-reachable. If that posture
ever changes — operator slips `--host 0.0.0.0`, dev VPS reachable,
container exposed on a public port — an attacker can bypass the CF-origin
check by sending `CF-Connecting-IP: 1.2.3.4` from anywhere on the
internet. The middleware's stated purpose ("the WAF rules make this
unreachable from the internet, but the middleware is the second layer")
is then trivially defeated. A peer-IP guard would actually make this a
real second layer.

Fix: only honor `cf-connecting-ip` when `request.client.host in
_TRUSTED_PROXY_HOSTS`; otherwise 403. Equivalently: validate the source
IP is in Cloudflare's published IP ranges
(https://www.cloudflare.com/ips/) — pull the list at startup and refresh
periodically.

Severity: **High** in the failure mode; **Low** today given current
network posture. Treated as High because the file's own docstring
promises "second layer" defense that does not exist.

---

### M-1 — Loose `subproduct_for_host` in `gateway/subproduct.py`

File: `/Users/shocakarel/Habbig/gateway/subproduct.py:336-352`

```python
def subproduct_for_host(host: str) -> Optional[dict]:
    bare = host.split(":")[0].strip().lower()
    first = bare.split(".")[0]
    if first == "staging":
        return None
    return SUBPRODUCTS.get(first)
```

This implementation takes the first DNS label and looks it up in the
catalogue with **no suffix check**. `crypto.evil.com`, `crypto`,
`crypto.localhost`, `crypto.localhost.attacker.example` all resolve
to the crypto subproduct dict. The stricter middleware version at
`/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:90-104`
correctly requires `.endswith(".narve.ai")`.

Today the loose function is called from:
- `pwa_middleware.py:238` (already-validated host header — fine)
- `server.py:3237, 3543, 3573, 3661, 4...` (post-middleware — fine)

The risk is structural: two implementations diverge. Any new caller (tests,
admin scripts, off-path tools) inheriting `subproduct.py::subproduct_for_host`
inherits the loose check. The test fixtures
(`tests/test_subproducts.py:91`) do assert `blog.narve.ai → None` but
miss the cross-TLD case (`crypto.evil.com → None`); the helper's
current behaviour would fail such an assertion.

Fix: either delete the loose helper and re-export the strict one, or
add a `.endswith(".narve.ai")` / ALLOWED_DOMAINS suffix guard inside
`subproduct.py::subproduct_for_host`.

Severity: **Medium** — masked by the production allowlist, but a
single misconfigured route that runs before `SubproductMiddleware`
(e.g. a health/diagnostic endpoint outside the middleware chain) would
expose the bypass.

---

### M-2 — `SubproductMiddleware` import-failure soft-fails to no allowlist

File: `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:41-46`
+ `/Users/shocakarel/Habbig/gateway/server.py:1477-1481`

```python
try:
    from subproduct import SUBPRODUCTS as _CATALOG
except Exception:
    _CATALOG = {}
```

If the `subproduct` module ever raises at import (syntax error in a
commit, missing dep), `_CATALOG = {}`. Then `_subproduct_hosts()`
returns the empty set and `allowed_hosts()` only includes apex + dev.
Every legitimate `<slug>.narve.ai` request gets a 400 — full outage of
the subproduct surface. Better than fail-open, but the outage is
silent (just a `log.warning` in the server.py registration path
when the import fails, no health-check signal).

Furthermore, the server.py registration block:

```python
try:
    from middleware.subproduct import SubproductMiddleware as _SubMW
    app.add_middleware(_SubMW)
except Exception as _sub_exc:
    log.warning("SubproductMiddleware import failed: %s — continuing without it", _sub_exc)
```

— if THIS try/except triggers, the server runs with **no host-header
validation and no CF-IP enforcement**. The fallback is fail-open, not
fail-closed. A typo in middleware/subproduct.py (caught at import) would
take CF-origin enforcement offline in production with only a warning
log line.

Fix: either fail-closed (sys.exit on production) or surface a `/health`
flag so an external monitor catches it.

Severity: **Medium**.

---

### M-3 — Subproduct access in `subproduct.py` does not check `expires_at` directly

File: `/Users/shocakarel/Habbig/gateway/subproduct.py:355-391`

The dependency-injected `has_subproduct_access` delegates entirely to
`has_active_subscription(user_id, dashboard_key)`. That helper
(`queries/subscriptions.py:29-44`) does check `expires_at > now OR
expires_at IS NULL`. **`expires_at IS NULL` means never-expires** —
any subscription row written without an expiry (admin-granted comp,
script-seeded test data, partial Stripe webhook) becomes a permanent
entitlement. The webhook path in `subproduct_access._blob_entry` is
stricter (a missing `period_end` is rejected, see
`subproduct_access.py:117-125`), but this stricter path is not used by
`subproduct.py::has_subproduct_access` — meaning **two access-check
codepaths exist with different expiry semantics**.

Impact: a comped/seeded sub written via legacy paths
(`upsert_subscription` with `duration_days=None`,
`queries/subscriptions.py:55-56`) is forever; the same user evaluated
via `subproduct_access.has_subproduct_access` would be denied if
`subproduct_subscriptions` JSON has no `period_end`.

Fix: pick one source of truth (the JSON blob is the documented one in
migration 060) and have both checks agree. At minimum, never write
`expires_at IS NULL` outside of the `__plan__` row.

Severity: **Medium**.

---

### M-4 — `subproduct_access.has_subproduct_access` does not check `_pro_or_better` correctly for sqlite3.Row

File: `/Users/shocakarel/Habbig/gateway/subproduct_access.py:58-72`

```python
def _pro_or_better(user_row: Any) -> bool:
    ...
    level = _field(user_row, "is_admin", 0) or 0
    if int(level) >= _ADMIN_LEVEL:
        return True
    tier = (_field(user_row, "subscription_tier", "") or "").lower()
    return "pro" in tier or "enterprise" in tier
```

Substring match: a future tier string like `"unsupported"`,
`"experimental_pro_trial"`, or `"enterprise_revoked"` would match.
`"pro_legacy_disabled"` matches. `"non_pro"` matches. Risk is low
because tier strings are operator-controlled, but the loose match
makes lockouts impossible to express via the tier string (you can't
write `"pro_disabled"` to revoke access). Combined with the broad
exception swallowing in this module, makes audit/forensics harder.

Fix: explicit allowlist of tiers (`{"pro", "pro_monthly", "pro_annual",
"enterprise_team", ...}`).

Severity: **Medium**.

---

### M-5 — `subproduct_access.has_subproduct_access` returns True on
Stripe-unreachable for non-pro users in production

File: `/Users/shocakarel/Habbig/gateway/subproduct_access.py:251-263`

```python
entry = _blob_entry(user, slug) or {}
live = await _live_stripe_status(entry)
if live is None:
    # Stripe unreachable — trust the DB (we already passed
    # has_subproduct_access above). Don't cache either verdict.
    return
```

If Stripe is unreachable for 6+ minutes, every protected request for a
user whose DB row still says "active" returns True even if the
subscription has been cancelled at Stripe in that window. Webhook
back-pressure would normally close this. But the failure mode is
fail-open: a sustained Stripe outage means cancelled-at-Stripe subs
continue to grant access until the webhook catches up. Combined with
`_VERIFY_TTL_SECONDS = 300` positive cache, a single transient Stripe
hiccup re-extends a cancelled-but-stale row by up to 5 minutes.

Fix: when Stripe is unreachable, *fail closed* if the DB-blob's
`stripe_sub_id` is set; only fail open for users who never had a Stripe
sub (admin-comped). Or surface the unreachable state to a monitor and
kill-switch the verify path.

Severity: **Medium**.

---

### L-1 — Middleware docstring overstates ordering

File: `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:9-12`

> An arbitrary Host header is a signal the request came from outside
> Cloudflare — possibly a direct-to-origin scan we want to reject
> with 400 before any auth/DB work happens.

The middleware is added at server.py:1479; middlewares registered AFTER
that (ImpersonationMiddleware at 1582, GlobalRateLimitMiddleware at
1811, etc.) run **outermost** in Starlette. `ImpersonationMiddleware`
does a DB lookup on every request with an impersonation cookie
(`db.get_impersonation_session_by_token`, server.py:1503) BEFORE the
host check runs. The "before any auth/DB work happens" promise is
therefore not strictly true.

Severity: **Low** — documentation correctness; the DB lookup is cheap
and the cookie has to exist.

---

### L-2 — Host header normalisation does not strip trailing dot or
whitespace-with-port

File: `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:84-87`

```python
def _strip_port(host: str) -> str:
    return host.split(":", 1)[0].strip().lower()
```

- A trailing dot (RFC-valid FQDN form: `narve.ai.`) bypasses the
  allowlist because `narve.ai.` ∉ `_APEX_HOSTS`.
- `Host: narve.ai ` (trailing space) is stripped → fine.
- `Host: narve.ai\r\nX-Bypass: 1` (header smuggling) depends on the
  ASGI server; uvicorn rejects but worth recording.

Impact: legitimate browsers don't send trailing-dot Hosts. An
allowlist bypass would only be available to a curl user — and they'd
still 400 because trailing-dot isn't in the set, so it's not a bypass,
just a parity gap.

Severity: **Low**.

---

### L-3 — `landing_context` lets template injection through stat_pills format string

File: `/Users/shocakarel/Habbig/gateway/subproduct.py:394-422`

```python
pills.append(template.format(**{k: stats.get(k, "—") for k in _placeholders(template)}))
```

`template` is hard-coded inside `SUBPRODUCTS` (operator-controlled),
not user input — so this isn't directly exploitable. But if a future
admin-editable template ever lands in this dict, `template.format()`
is susceptible to `str.format` attribute-access exploits
(`{a.__class__.__mro__[1].__subclasses__}`) which can expose
internals. The HTML output is escaped in `_format_stat_pills`
(server.py:3360), so DOM injection is blocked at that level — but
the format-string path itself remains a concern if the catalogue
becomes operator-editable later.

Fix: pre-validate placeholders via `_placeholders()` then build the
string with `.replace()` or a custom mini-formatter that refuses
attribute access.

Severity: **Low** (informational hardening).

---

### L-4 — `subproduct.py::has_subproduct_access` swallows all exceptions
in `has_pro_plan(user)` AND in `has_active_subscription`

File: `/Users/shocakarel/Habbig/gateway/subproduct.py:380-391`

```python
try:
    if has_pro_plan(user):
        return True
except Exception:
    pass
...
try:
    return bool(has_active_subscription(user["user_id"], dashboard_key))
except Exception:
    return False
```

A DB connection error → silently denies access. A bug in `has_pro_plan`
→ silently falls through to the subscription check (instead of failing
loudly). Combined with the inconsistent expiry semantics in M-3, this
swallowing makes incidents hard to triage. The dependency-injected
design is otherwise good.

Severity: **Low**.

---

### I-1 — Slug-to-subdomain mapping is consistent, with one caveat

`gateway/subproduct.py::SUBPRODUCTS` defines 13 slugs:
`sports, weather, world, crypto, midterm, traders, voters, whale, cb,
climate, disasters, health, love`.

`gateway/config.json::dashboards` keys: `sports, weather, world, crypto,
midterm, top_traders, whale, voters, climate, disasters, centralbank,
world_health, love`. Subdomains: same as slugs except
`top_traders→traders`, `centralbank→cb`, `world_health→health`.

`DASHBOARD_KEY_FOR_SLUG` (subproduct.py:324-326) bridges those three
mismatches correctly. `SUBDOMAIN_TO_KEY` (server.py:78) bridges the
other direction. `proxy_request` uses `SUBDOMAIN_TO_KEY` (server.py:7831)
so the subscription check at line 7847 hits the right dashboard_key.
`subproduct_dashboard_routes.register()` whitelists only 8 slugs (line
120: `sports, weather, world, crypto, midterm, traders, climate,
voters`) — voters/whale/cb/disasters/health/love are **catalogued but
NOT installed as `/dashboard/<slug>` routes** in that module. That
might be intentional (they're proxied via the legacy `proxy_request`
path) but is a divergence worth tracking.

Severity: **Informational**.

---

### I-2 — `dispatch` parses Host once but `request.headers.get("host")`
returns the raw header verbatim

File: `/Users/shocakarel/Habbig/gateway/middleware/subproduct.py:117-118`

```python
host_header = request.headers.get("host", "")
host = _strip_port(host_header)
```

A duplicate / list-valued Host header is impossible at the ASGI layer
(uvicorn collapses to one). Starlette's `request.headers` is
case-insensitive but case-preserving on output. If a client sends `Host: NARVE.AI`,
`_strip_port` lowercases it — fine. Empty Host (`Host:`) → `host = ""`.
The middleware's `if host and host not in allow:` then **passes
through** (because `host` is falsy). The empty Host is not in the
allowlist but the `and host` short-circuits the rejection. Net effect:
a request with no Host header is allowed through to downstream
handlers.

In practice ASGI servers reject empty Host on HTTP/1.1, but HTTP/2 and
HTTP/3 don't require it. Worth gating.

Fix: `if host not in allow:` (drop the `host and`).

Severity: **Informational** — depends on ASGI behavior, unlikely to be
exploitable but contradicts the "every legitimate request has one of a
handful of Host values" docstring.

---

### I-3 — `subproduct_signup_routes::subproduct_signup` does NOT
enforce request.state.subproduct match server-side beyond
"prefer-state-then-form"

File: `/Users/shocakarel/Habbig/gateway/subproduct_signup_routes.py:184-223`

```python
attached = getattr(request.state, "subproduct", None)
slug = (attached or subproduct or "").strip()
```

If the user is on the apex `narve.ai` (no subproduct attached) and the
form posts `subproduct=crypto`, the form value wins. That's currently
benign — the route uses `_stripe_price_id(slug)` to validate against
the catalogue — but the docstring claim that the middleware-attached
value is "trusted over the form field so a scraped form can't
cross-buy" is only true on `*.narve.ai` hosts. On apex, a scraped form
can still pre-fill any catalogue slug and trigger checkout for it.
That's not a security bug (the user is choosing what to buy) but the
docstring is slightly aspirational.

Severity: **Informational**.

---

## Confirmation of stated controls

- Host allowlist works as advertised in production: 400 for any host not
  in `{apex, www, api, admin, staging}.narve.ai ∪ {slug}.narve.ai ∪
  {localhost, 127.0.0.1, testserver}`. Tested by
  `tests/test_subproduct_middleware.py:76`.
- `request.state.subproduct` is set even for apex hosts (None) so
  downstream `getattr(request.state, "subproduct", None)` is safe.
- `require_subproduct_access(slug)` correctly enforces sub-brand
  cross-routing — a user with sports access on `crypto.narve.ai` gets
  402 (subproduct_access.py:212-216).
- Stripe live-verify cache is per-user-slug, 5 min TTL, invalidated
  on webhook (`subproduct_access.invalidate_user`).
- Expired subs in the JSON blob path are correctly rejected
  (subproduct_access.py:120-125).
- Feature-flag scope is correctly per-subproduct (queries/admin.py:561)
  with per-subproduct row overriding global.

## Recommended quick wins (no rewrite required)

1. Patch the realtime channel bug (H-1) — one-line fix.
2. Add a peer-IP guard to `SubproductMiddleware`'s CF-IP check (H-2).
3. Replace the loose `subproduct.py::subproduct_for_host` with a
   re-export of the strict middleware version (M-1).
4. Make `SubproductMiddleware` import failure crash boot when
   `PRODUCTION=1` (M-2) instead of warning-and-continuing.
5. Tighten `_pro_or_better` to an allowlist (M-4).
6. Fail closed on Stripe outage when `stripe_sub_id` is set (M-5).

End of audit.
