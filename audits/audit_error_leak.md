# Adversarial audit — Exception / stack-trace / DB-error leaks in user-facing responses

Scope: every Python route that builds an HTTP response from `Exception`, `ValueError`, `sqlite3.Error`, `httpx.HTTPError`, or `Exception.__str__()`. Cross-referenced with the global error handler (`gateway/error_handlers.py`) and the per-app handlers in `gateway/server.py:743-760`, `voters-dashboard/server.py:1473`.

Threat model: anonymous attacker probing endpoints for fingerprinting (DB engine, schema, library versions, internal paths), authenticated user pivoting after a 500, attacker enumerating internal state via crafted inputs that trip exceptions in known code paths.

## Severity tally

| Severity | Count |
|----------|-------|
| High     | 6 |
| Medium   | 24 |
| Low      | 12 |
| Info     | 4 |

**Total leak sites: 46.**

The gateway service is partially defended by `gateway/error_handlers.py:441-460`'s `_looks_like_trace()` heuristic, which scrubs HTTPException `detail` strings that match crude trace tokens ("Traceback", "sqlite3.", "IntegrityError", "UNIQUE constraint", >240 chars, etc.). However, this defence is **shallow** — short `ValueError("invalid foo")`-style messages and most `httpx.HTTPError` strings pass straight through, so the underlying `detail=str(exc)` calls still leak business-logic detail. Non-gateway dashboards (`sports-dashboard`, `voters-dashboard`, `world-health-dashboard`, `love-dashboard`, `disasters-dashboard`, `top-traders-dashboard`, `centralbank-dashboard`, `Dashboard-x-truth-research-prediction`) bypass that handler entirely and leak raw.

---

## HIGH severity findings

### H1. [HIGH] `top-traders-dashboard/server.py:78,101,130` — raw `httpx.HTTPError` leaked on every upstream failure, unauthenticated

Three public endpoints on the top-traders subproduct surface the full upstream exception verbatim to anonymous callers:

```py
except httpx.HTTPError as e:
    raise HTTPException(502, f"Polymarket leaderboard fetch failed: {e}")
```

This subproduct has NO global error handler installed (only the dashboard's own `FastAPI()` instance — no `app.add_exception_handler(...)` call exists in the file). The raw `httpx.HTTPError` string includes the resolved internal URL (`https://lb-api.polymarket.com/volume`), the User-Agent, sometimes the response body, and on TLS failures the certificate chain. Hitting `/api/leaderboard?window=invalid` then `/api/leaderboard?window=all` with the upstream down lets an attacker fingerprint the exact CLOB endpoint Polymarket-side and the dependency version (`httpx/0.27.x` patterns).

File: `top-traders-dashboard/server.py:78` (top of leaderboard route), `:101` (trades route), `:130` (top-traders combo route).

### H2. [HIGH] `Dashboard-x-truth-research-prediction/app/main.py:599` — raw exception rendered into HTMLResponse for `/sync-now`

```py
except Exception as exc:
    return HTMLResponse(f'<div class="text-red-400 text-xs">Error: {_esc(str(exc))}</div>')
```

`/sync-now` is invoked by HTMX from the dashboard navbar. The handler catches **any** exception from `run_pipeline()` — including `OperationalError`, `IntegrityError`, ORM session leaks, missing-environment crashes, and 3rd-party API key errors — and HTML-encodes the str into the visible page. `_esc()` only escapes HTML entities; it doesn't redact stack frames, file paths, SQL fragments, or token-shaped substrings. Any user who can reach the dashboard page gets the leak when the upstream pipeline breaks.

File: `Dashboard-x-truth-research-prediction/app/main.py:599`.

### H3. [HIGH] `sports-dashboard/sports_dashboard.py:2240` — raw `Exception` from Polymarket call returned as JSON 500, post-auth but pre-validation

```py
@app.get("/api/orderbook/{token_id}")
async def get_orderbook(token_id: str, request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        resp = await asyncio.to_thread(
            lambda: requests.get(f"{POLYMARKET_HOST}/book", params={"token_id": token_id}, timeout=10)
        )
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
```

`token_id` is taken from the path without sanitisation and concatenated into the upstream URL via `params=`. An attacker registers a free account, crafts a malformed token_id, and reads the resulting `requests.exceptions.ConnectionError` / `HTTPError` / JSON-decode error — leaking the exact Polymarket host (which the rest of this dashboard already exposes, so the marginal damage is lower) plus library version detail. **Worse**: a `ReadTimeout` or DNS-failure exception under network blip would leak the system's resolver state.

File: `sports-dashboard/sports_dashboard.py:2240`.

### H4. [HIGH] `gateway/intelligence_routes.py:278` — `ValueError` from `db.set_user_env_preferences` returned verbatim to user

```py
try:
    db.set_user_env_preferences(user["user_id"], show=show, unit=unit)
except ValueError as exc:
    return JSONResponse({"error": str(exc)}, status_code=400)
```

This is a `JSONResponse` (not an `HTTPException`), so the gateway's `http_exception_handler` does NOT run and `_looks_like_trace` is bypassed. The raw `ValueError` message lands in the user's response body. `set_user_env_preferences` raises with internal column-validation strings; any DB-side `CHECK` constraint message or future refactor that surfaces `IntegrityError` text into the `ValueError` becomes a direct leak.

File: `gateway/intelligence_routes.py:278`.

### H5. [HIGH] `gateway/jobs/pipeline_jobs.py:187,189` — backtest failure stores raw `str(e)` in DB; surfaced verbatim via `GET /api/backtests/{id}`

```py
except Exception as e:
    log.exception("Backtest %d failed: %s", backtest_id, e)
    with _db.conn() as c:
        c.execute(
            "UPDATE backtests SET status = 'failed', result = ?, completed_at = ? WHERE id = ?",
            (_json.dumps({"error": str(e)}), now, backtest_id),
        )
    return {"backtest_id": backtest_id, "status": "failed", "error": str(e)}
```

`api_get_backtest` (`gateway/intelligence_routes.py:138-156`) returns the JSON-decoded `result` field directly to the requesting user. Any exception from the backtest pipeline (engine import error, SQLite `OperationalError`, missing market data, NumPy traceback) becomes the value of `result.error` in the API response. Pro-tier user only, so reduced blast radius, but still an authenticated user gets the raw exception. There's no `[:200]` truncation either, so the full stack frame is preserved if `str(e)` is multiline.

File: `gateway/jobs/pipeline_jobs.py:187,189` (write); `gateway/intelligence_routes.py:138-156` (read).

### H6. [HIGH] `gateway/server_features.py:1563` — `HTTPException.detail` from `clean_text` re-wrapped into a JSONResponse, bypassing global handler

```py
except HTTPException as exc:
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)
```

The author re-emits the HTTPException as a `JSONResponse`, which means the global handler that would normally apply `_looks_like_trace` and the safe-status copy in `gateway/error_handlers.py:345-389` is **never invoked** for this code path. Whatever `clean_text` raises (input-hygiene errors can include user-controlled byte-class names) reaches the network as the literal `error` field. This is on the public registration endpoint, so an unauthenticated attacker can probe `clean_text` exception messages.

File: `gateway/server_features.py:1563`.

---

## MEDIUM severity findings

### M1. [MEDIUM] `gateway/affiliate_routes.py:383,624,669` — three `HTTPException(detail=str(e))` on affiliate / link-create paths

```py
except ValueError as e:
    raise HTTPException(status_code=400, detail=str(e))
```

`da.create_affiliate_link`, `da.create_affiliate_account`, and `da.update_affiliate_account` raise `ValueError("commission_rate must be between 0 and 1")`-style messages that are fine to surface; **but** the same `except ValueError` would also pass through anything raised by future refactors (e.g. SQLite `IntegrityError` re-raised as ValueError, or a `Decimal` parser leak). The handler does pass through `_looks_like_trace`, so the worst trace-shaped leaks are scrubbed — but `_looks_like_trace` does NOT catch short ValueError messages that incidentally contain table/column names. Tighten by emitting a fixed string and logging the raw exception.

Files: `gateway/affiliate_routes.py:383`, `:624`, `:669`.

### M2. [MEDIUM] `gateway/collections_routes.py:228,281,283,294,314,318,335,355,366` — nine `detail=str(exc)` sites across the public Collections API

All nine sites pass `str(exc)` from `coll.*` calls into `HTTPException(detail=...)`. They share the same threat profile as M1: the global handler scrubs trace-shaped strings but lets short `ValueError`/`PermissionError` messages through. PermissionError messages in `coll.delete_collection`, `coll.add_item`, etc. are user-facing and intentional, but any future refactor leak (e.g. `PermissionError(f"row {row['id']} owned by {row['owner_user_id']}")`) is one commit away from leaking another user's ID or internal row state.

Files: `gateway/collections_routes.py:228`, `:281`, `:283`, `:294`, `:314`, `:318`, `:335`, `:355`, `:366`.

### M3. [MEDIUM] `gateway/take_routes.py:293,310,346,391,449,634` — six `detail=str(e)` sites on takes / reports / votes

Same pattern. The takes API is heavily used and `db_takes.*` ValueErrors carry actionable validation messages, but the catch-all `except ValueError as e: raise HTTPException(..., detail=str(e))` is fragile. Line 293 stores `str(e)` inside an idempotency record (`_detail`) that the surrounding `with_idempotency` wrapper re-emits to the next caller with the same Idempotency-Key — meaning the original ValueError text is **cached and replayed** to whoever retries with the same key.

Files: `gateway/take_routes.py:293`, `:310`, `:346`, `:391`, `:449`, `:634`.

### M4. [MEDIUM] `gateway/admin_emails_routes.py:618` — `f"resend failed: {exc}"` on admin POST /admin/emails/{id}/resend

```py
except Exception as exc:
    log.warning("admin email resend failed for id=%s: %s", email_id, exc)
    raise HTTPException(status_code=500, detail=f"resend failed: {exc}")
```

Admin-only, so blast radius is low — but the catch is bare `Exception`, meaning SMTP library exceptions (which can include SMTP relay hostnames, error 5xx codes with bounce reasons, and recipient addresses) flow into the response. A compromised admin already has the keys; this finding is about reducing the trail a compromised admin can extract via UI rather than direct DB.

File: `gateway/admin_emails_routes.py:618`.

### M5. [MEDIUM] `gateway/ai_routes.py:98,150` — `sqlite3.Error` exposed as `f"claude_usage_log read failed: {exc}"` on admin AI dashboard

```py
except sqlite3.Error as exc:
    raise HTTPException(status_code=500, detail=f"claude_usage_log read failed: {exc}")
```

Admin-only, but `sqlite3.Error.__str__` returns strings like `no such column: foo` or `database is locked` — useful fingerprinting on schema rollouts. Same finding as M4 about trail reduction.

Files: `gateway/ai_routes.py:98`, `:150`.

### M6. [MEDIUM] `gateway/admin_jobs_routes.py:269,281,293` — `f"pause failed: {exc}"` / `resume` / `trigger`

```py
except Exception as exc:
    raise HTTPException(status_code=404, detail=f"pause failed: {exc}")
```

Admin-only. APScheduler exceptions (e.g. `JobLookupError`) carry the requested job name plus the internal job-store backend. Already discussed in `audit_admin_jobs_routes.md` finding #1.

Files: `gateway/admin_jobs_routes.py:269`, `:281`, `:293`.

### M7. [MEDIUM] `gateway/admin_integrations_routes.py:71,92,132,157,172` — five `error: str(exc)[:200]` sites on `/api/admin/integrations/{slug}/test`

```py
except Exception as exc:
    return {"ok": False, "error": str(exc)[:200], ...}
```

Admin-only. The `[:200]` truncation is good practice, but the body of the error still includes the upstream URL on `httpx` exceptions, the Anthropic SDK error class names, and Stripe API error JSON when their balance endpoint 401s. Treat as a defense-in-depth fix: emit a fixed `"upstream unavailable"` and log the rest.

Files: `gateway/admin_integrations_routes.py:71`, `:92`, `:132`, `:157`, `:172`.

### M8. [MEDIUM] `gateway/webhooks_routes.py:282,415` — `error: str(exc)[:200]` returned from `/webhooks/{id}/test` and dead-letter replay

Webhook owner sees raw delivery exception. The library is `httpx`, so the exception strings include resolved subscriber URLs (which the user already owns) and the error class names. Lower severity because the user owns the URL, but Anthropic / Stripe / internal-service replays via the DLQ admin path leak the subscriber state.

Files: `gateway/webhooks_routes.py:282`, `:415`.

### M9. [MEDIUM] `gateway/status_routes.py:308` — `detail=str(exc)` on the public `/api/status/subscribe` endpoint

```py
try:
    sub = status_db.create_subscription(email, components)
except ValueError as exc:
    raise HTTPException(status_code=400, detail=str(exc))
```

Public, unauthenticated endpoint (status page subscriptions). `status_db.create_subscription` raises `ValueError` for malformed emails / duplicate subscriptions — fine — but the catch is broad enough to surface DB-side `IntegrityError` re-wrapped as `ValueError` if the schema changes.

File: `gateway/status_routes.py:308`.

### M10. [MEDIUM] `disasters-dashboard/server.py:785` — `error: str(e)` per source in `/api/sources`

```py
try:
    d = fetcher() or {}
    out.append({"source": name, "ok": bool(d), "count": d.get("count", 0), ...})
except Exception as e:
    out.append({"source": name, "ok": False, "error": str(e)})
```

Public, unauthenticated endpoint. Each upstream source (USGS, EMSC, GDACS, NOAA, FIRMS) raises its own library-specific exceptions on outage. The raw `str(e)` includes `requests.exceptions.HTTPError: 503 Server Error: ...` with the resolved URL, or `json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` if the upstream returns HTML. Disasters dashboard has NO global handler.

File: `disasters-dashboard/server.py:785`.

### M11. [MEDIUM] `world-health-dashboard/server.py:298,319` and `love-dashboard/server.py:238,261` — `error: str(e)` on YAML parser failure leaked to public JSON

```py
except Exception as e:
    return {"diseases": [], "count": 0, "target": 508, "error": str(e)}
```

Public JSON endpoint. If `diseases.yaml` / `metrics.yaml` / `sources.yaml` is malformed (deploy issue), the raw PyYAML exception including the file path, line/column number, and offending bytes is in every response. Pure information disclosure but reveals the on-disk path of the data fixtures.

Files: `world-health-dashboard/server.py:298`, `:319`; `love-dashboard/server.py:238`, `:261`.

---

## LOW severity findings (job results / internal status surfaces)

### L1. [LOW] `gateway/portfolio/polymarket.py:293` and `gateway/portfolio/kalshi.py:216` — `error: str(exc)` in portfolio refresh

Returned to authenticated user via portfolio fetch routes; raw `httpx.HTTPError` / SDK errors. Both are also written to the `error_text` DB column for the user.

Files: `gateway/portfolio/polymarket.py:293`, `gateway/portfolio/kalshi.py:216`.

### L2. [LOW] `gateway/backtest.py:295` — `error: str(exc)` in backtest engine result

Already partially covered by H5 (`pipeline_jobs.py` is the typical wrapper). Same exposure when this is the entry point.

File: `gateway/backtest.py:295`.

### L3. [LOW] `gateway/reports/weekly.py:454` — `error: str(exc)` in weekly report run result

Surfaced via admin reports dashboard / job result API. Admin-only.

File: `gateway/reports/weekly.py:454`.

### L4. [LOW] `gateway/jobs/email_jobs.py:355`, `gateway/jobs/notification_jobs.py:295,133`, `gateway/jobs/resolution_jobs.py:47,211`, `gateway/jobs/backtest_jobs.py:41`, `gateway/jobs/db_maintenance.py:82,257,289`, `gateway/jobs/ai_maintenance.py:166`, `gateway/jobs/compute_source_relationships.py:168`, `gateway/jobs/claude_cost_check.py:57,73`, `gateway/jobs/insider_jobs.py:84,90` — pattern of `return {"error": str(...)}` from background jobs

All of these become the value of `jobs_run.result_json` (or equivalent) and are surfaced via `GET /admin/jobs/{name}/runs` to admin. Treat as admin-only information disclosure. None are user-reachable directly.

Files: as listed above.

### L5. [LOW] `gateway/exports/generator.py:951` — `error: str(e)[:200]` in export job result, also stored as `error=str(e)[:500]` in `data_export_requests` table

User can poll `/api/account/export` to check the status of their own export but `export_routes.py:338-352` does NOT echo the `error` column to the user — the row is only used for status flag and download. So the leak is internal-only. Confirmed by reading `api_get_export_status` flow.

Files: `gateway/exports/generator.py:951`; `gateway/export_routes.py:263` (DB write).

### L6. [LOW] `gateway/observability/sentry_api.py:161` — `error: str(e)[:200]` in admin Sentry summary

Admin only.

File: `gateway/observability/sentry_api.py:161`.

### L7. [LOW] `gateway/email_system/service.py:289` — `{"subject": f"[preview error: {exc}]", "html": ""}`

Used by admin template preview keystroke endpoint. Admin only.

File: `gateway/email_system/service.py:289`.

### L8. [LOW] `gateway/scraper/scrapers/substack.py:163`, `gateway/scraper/scrapers/metaculus.py:128` — `error: str(e)` in scraper-availability probes

These are scraper-internal status methods; the only call sites are internal job runs. No HTTP exposure was found in the gateway routes.

Files: `gateway/scraper/scrapers/substack.py:163`, `gateway/scraper/scrapers/metaculus.py:128`.

### L9. [LOW] `gateway/jobs/backend.py:175` — stores `f"{type(e).__name__}: {e}\n{traceback.format_exc()[:800]}"` as `last_error`

Stored in the `jobs_run` table, surfaced to admin via `/admin/jobs/{name}/runs`. **This is the only place in the codebase where a real `traceback.format_exc()` ends up reachable from an HTTP response** (admin-only).

File: `gateway/jobs/backend.py:175`.

### L10. [LOW] `gateway/backend/markets/polymarket_client.py:174` — `return {"error": str(e)}` from market fetch helper

Used internally; bubbled up via `gateway/jobs/ai_jobs.py:203` `return {"error": "market fetch failed", "detail": str(exc)}` to job result.

Files: `gateway/backend/markets/polymarket_client.py:174`, `gateway/jobs/ai_jobs.py:203`.

### L11. [LOW] `annoyance-dashboard/server.py:93,132` — `db.upsert_source_status("reddit", ok=False, error=str(e)[:500])`

Stored in DB and surfaced via the annoyance-dashboard sources status endpoint. Subproduct.

Files: `annoyance-dashboard/server.py:93`, `:132`.

### L12. [LOW] `polymarket_weather_bot/clob_client.py:130` — `error: str(e)` in CLOB client response

Internal bot, not a public HTTP path.

File: `polymarket_weather_bot/clob_client.py:130`.

---

## INFO (intentional / handled / not user-facing)

- `gateway/error_handlers.py:441-460` — `_looks_like_trace()` heuristic. **Conservative**: catches Traceback / sqlite3.* / IntegrityError / OperationalError / FOREIGN KEY / UNIQUE constraint / >240 char. Does NOT catch short `ValueError("invalid timestamp")` or `httpx.HTTPError` strings. Improve by switching all `detail=str(e)` sites to emit a static safe message + log raw.
- `gateway/server.py:743-760` — `_JSONDecodeError`, `_RequestValidationError`, and `Exception` handlers all return generic safe messages. Good.
- `gateway/server.py:3255-3257` — health-check check errors only included in payload when `not IS_PRODUCTION`. Good production posture.
- `bots/` — no exception leaks to chat output found (formatters.py sanitises responses).

---

## Top 3 worst leaks (overall)

1. **`top-traders-dashboard/server.py:78,101,130`** — unauthenticated upstream `httpx.HTTPError` strings to the public internet, three sites, no global handler installed on the subproduct. Fingerprints the exact Polymarket data-API endpoint and library version on outage.
2. **`Dashboard-x-truth-research-prediction/app/main.py:599`** — any caller of `/sync-now` (HTMX-triggered, dashboard-page-level) sees the full `str(exc)` of the entire ingest pipeline rendered into HTML. SQL, ORM session state, third-party API key errors, traceback fragments — all reachable.
3. **`sports-dashboard/sports_dashboard.py:2240`** — authenticated user can use path-parameter `token_id` to deliberately trip exceptions in the upstream Polymarket call and read back the raw `Exception.__str__()`, including resolver / connection / SSL diagnostics, on a 500.

## Recommended remediation pattern

For every site flagged above, replace:

```py
except SomeError as e:
    raise HTTPException(status_code=400, detail=str(e))
```

with:

```py
except SomeError as e:
    log.warning("operation failed for user_id=%s: %s", user_id, e)
    raise HTTPException(status_code=400, detail="That request couldn't be processed.")
```

Add `app.add_exception_handler(StarletteHTTPException, ...)` + `Exception` handler to every subproduct dashboard (sports, voters, world-health, love, disasters, top-traders, centralbank, Dashboard-x-truth-research-prediction) that doesn't currently install one. Reuse the `gateway/error_handlers.py` helpers — they're already imported-and-installed by the gateway and demonstrably handle this correctly.

---

## Search methodology

Patterns scanned across all `.py` files under `Habbig/` (excluding `venv`, `.venv`, `__pycache__`, `site-packages`):

- `detail=str(e)` / `detail=str(exc)` — 17 hits
- `HTTPException(..., f"...{e}...")` / `f"...{exc}..."` — 9 hits in trace-relevant catches (43 total f-string hits incl. user-input echoes)
- `JSONResponse({"error": str(...)})` direct returns — 2 hits in route handlers
- `{"error": str(e/exc)}` returned from job functions reachable via admin / authenticated API — 25 hits (catalogued in L4)
- `HTMLResponse(f"...{exc}...")` — 1 hit
- `traceback.format_exc()` in response paths — 0 (only 1 hit in `gateway/jobs/backend.py:175`, admin-only, stored in DB)

No `return JSONResponse(str(exc))` (positional raw-string) sites were found. No `raise HTTPException(detail=traceback.format_exc())` sites were found.
