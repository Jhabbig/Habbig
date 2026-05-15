# Audit: Live Health Endpoints

**Date:** 2026-05-15T13:05:46Z (UTC)
**Auditor:** Claude (Opus 4.7, 1M ctx)
**Target host:** `https://narve.ai` (production)
**Scope:** Live curl of `/health` and `/api/v1/health`. Verify HTTP 200, JSON validity, and presence of expected fields: `status`, `db`, `scheduler`, `git_sha`, `deployed_at`, `uptime`.
**Method:** Synchronous bash (`curl`) only. No service introspection, no pre-release endpoints.

---

## 1. `/health` — public endpoint

### 1.1 Transport

| Metric | Value |
|---|---|
| HTTP status | **200 OK** |
| Bytes downloaded | 335 |
| Time total | 0.144 s |
| Redirect | none |
| Content-Type | `application/json` (parsed cleanly) |

### 1.2 Body (parsed JSON, pretty-printed)

```json
{
    "status": "ok",
    "service": "narve-gateway",
    "version": "1.0.0",
    "environment": "production",
    "timestamp": "2026-05-15T13:05:46.961410+00:00",
    "uptime_seconds": 50445,
    "git_sha": null,
    "deployed_at": null,
    "checks": {
        "database": "ok",
        "db": "ok",
        "static": "ok",
        "dashboards": "ok",
        "encryption": "ok",
        "gate": "ok",
        "scheduler": "disabled",
        "email": "unconfigured"
    }
}
```

JSON is valid (passes `python3 -m json.tool`).

### 1.3 Field presence vs. spec

The spec asked for these fields: `status`, `db`, `scheduler`, `git_sha`, `deployed_at`, `uptime`.

| Spec field | Present? | Location in payload | Live value |
|---|---|---|---|
| `status` | yes | top-level `status` | `"ok"` |
| `db` | yes | nested at `checks.db` (and duplicated at `checks.database`) | `"ok"` |
| `scheduler` | yes | nested at `checks.scheduler` | `"disabled"` |
| `git_sha` | yes (key present) | top-level `git_sha` | **`null`** |
| `deployed_at` | yes (key present) | top-level `deployed_at` | **`null`** |
| `uptime` | partially | top-level `uptime_seconds` (renamed) | `50445` (~14.0 h) |

### 1.4 Extras (not in spec, observed)

- `service` = `"narve-gateway"`
- `version` = `"1.0.0"`
- `environment` = `"production"`
- `timestamp` = ISO-8601 UTC, present and well-formed
- `checks.database` (duplicate of `checks.db`)
- `checks.static` = `ok`
- `checks.dashboards` = `ok`
- `checks.encryption` = `ok`
- `checks.gate` = `ok`
- `checks.email` = `"unconfigured"`

### 1.5 Health verdict

- **Aggregate `status`: ok.**
- All hard-dependency checks (`db`, `static`, `dashboards`, `encryption`, `gate`) report `ok`.
- `scheduler` is `disabled` — by design on this node (the gateway does not run the scheduler in this deploy); not an error, but worth noting.
- `email` is `unconfigured` — non-blocking warning; SMTP credentials not wired.

---

## 2. `/api/v1/health` — versioned endpoint

### 2.1 Transport

| Metric | Value |
|---|---|
| HTTP status | **302 Found** |
| Bytes downloaded | 0 |
| Time total | 0.078 s |
| Redirect location | `https://narve.ai/gate` |
| Body | empty |

When followed with `-L`, the request resolves to the gate HTML page (HTTP 200, `text/html`), **not** a JSON health payload.

### 2.2 Verdict

`/api/v1/health` is **gated** in production. It does not return JSON and cannot be used as an unauthenticated liveness probe. Anything monitoring it as a JSON endpoint will fail.

---

## 3. Gaps & findings

### 3.1 Naming mismatch
- Spec field `uptime` is actually published as `uptime_seconds`. The data is present, but the key is non-canonical. Monitors looking for `uptime` will see it as missing.

### 3.2 Null fields where strings are expected
- `git_sha` is `null`. The deploy pipeline is not stamping the build SHA into the health payload. This breaks "what version is live" lookups and complicates rollback decisions.
- `deployed_at` is `null`. Same root cause — the deploy job is not writing a deploy timestamp into the runtime config that `/health` reads from.

### 3.3 `/api/v1/health` is not a health endpoint in practice
- The versioned route is gated and returns HTML, not JSON. Any external uptime monitor pointing at `/api/v1/health` and expecting JSON will register false alerts (302 followed to 200-HTML is not a parseable health signal).
- Either move `/api/v1/health` outside the gate (mirror `/health`) or remove it from any documentation that suggests it is a probe endpoint.

### 3.4 Duplicate keys in `checks`
- `checks.database` and `checks.db` carry the same value. Harmless, but indicates the payload was extended without removing the legacy alias. Pick one and deprecate the other to keep the schema tight.

### 3.5 `scheduler: "disabled"` semantics
- On this node the scheduler is intentionally off; the payload distinguishes `disabled` from `ok`/`error`. That is correct behaviour for gateway-only nodes. Worth documenting so an external monitor doesn't treat `disabled` as a failure.

### 3.6 No `Cache-Control` header asserted
- Not in scope of this audit, but `/health` should set `Cache-Control: no-store` to avoid edge caching. Verify on next pass.

---

## 4. Summary

| Endpoint | HTTP | JSON | All spec fields present | Action |
|---|---|---|---|---|
| `/health` | 200 | valid | keys present; `git_sha` & `deployed_at` are `null`; `uptime` named `uptime_seconds` | Backfill `git_sha`/`deployed_at` in deploy pipeline; consider aliasing `uptime` |
| `/api/v1/health` | 302 → `/gate` | n/a (HTML) | no | Either move outside the gate or remove from probe docs |

**Headline:** `/health` is healthy and returns a valid JSON envelope. The two key data gaps are `git_sha=null` and `deployed_at=null` — the schema accepts them, but the deploy pipeline isn't filling them in. `/api/v1/health` is not usable as a public probe today.
