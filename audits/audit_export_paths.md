# GDPR Export — Code Path Convergence Audit

Cross-references the two GDPR data-export ZIP-build implementations in the gateway and identifies which is wired into the running server vs. which is unreachable. Companion to `audits/audit_data_export.md` (which scopes the wired path's contents) and `audits/audit_gdpr_export_completeness.md`.

## Files audited

- `gateway/export_routes.py` — defines `_build_zip(user_id, zip_path) -> int` at L161–263, plus the FastAPI routes, the in-process `ThreadPoolExecutor` (`_executor` at L46), and the worker shim `_run_export(request_id)` at L266–298 / `_enqueue_export(request_id)` at L301–303.
- `gateway/exports/generator.py` — defines `build_zip(user_id, target_path) -> dict` at L846–959, plus `_collect(user_id)` at L241–709 (51 sections), `generate(export_id)` at L965–1045, and `sign_download_url` / `verify_download_token`.
- `gateway/exports/__init__.py` — re-exports `generate`, `sign_download_url`, `verify_download_token`, `EXPORT_DIR`, `EXPORT_TTL_SECONDS` from `exports.generator`.
- `gateway/jobs/export_jobs.py` — declares `@register_job("generate_data_export")` and `@register_job("cleanup_expired_data_exports")` ARQ entry points that defer to `exports.generate`.
- `gateway/jobs/__init__.py` — the public `jobs` package init.
- `gateway/jobs/worker.py` — the ARQ worker entry point (`WorkerSettings`).
- `gateway/server.py` — route-mount site.

## Verdict

**`gateway/export_routes.py:_build_zip` is the only path reachable in production.**
**`gateway/exports/generator.py:build_zip` (and therefore the entire `exports/` package) is dead code outside the test suite.**

Three independent confirmations:

1. **No production import chain reaches `exports/`.** Every non-test reference to `from exports` or `import exports` (a single one: `gateway/jobs/export_jobs.py:23`) is itself gated behind a module that is never imported (see point 2).
2. **`gateway/jobs/__init__.py` does not load `export_jobs`.** The `jobs` package init at `gateway/jobs/__init__.py:29–132` explicitly imports `email_jobs`, `embed_jobs`, `notification_jobs`, `pipeline_jobs`, `resolution_jobs`, `status_jobs`, `forecast_sync`, `take_resolution_jobs`, `sync_portfolios`, `reconcile_subscriptions`, `telegram_sends`, `invite_replenish`, `share_retention`, `newsletter_blast_jobs`, and the loop at L103–132 covers `claude_cost_check`, `compute_source_relationships`, `movement_jobs`, `generate_weekly_reports`, `insider_jobs`, `backtest_jobs`, `ai_maintenance`, `compute_churn_signals`, `feedback_digest`, `db_maintenance`, `perf_baseline`. `export_jobs` is **not in either list.** Therefore `@register_job("generate_data_export")` at `export_jobs.py:20` never executes at import time, the job name never enters `job_registry`, and no caller can enqueue it. `gateway/jobs/worker.py:32` also imports only `email_jobs, notification_jobs, pipeline_jobs` directly, confirming the worker won't pull `export_jobs` either.
3. **`exports.generator` references DB helpers that do not exist.** `generate(export_id)` at L965–1045 calls `db.get_export_request(...)`, `db.update_export_status(...)`, and the cleanup job at `export_jobs.py:35` calls `db.expire_old_exports()`. A repo-wide grep for `def get_export_request|def update_export_status|def expire_old_exports` returns zero matches. The actual helpers in `gateway/db.py:1060–1066` are named `create_data_export_request`, `get_data_export_request`, `list_user_data_exports`, `last_user_data_export_ts`, `update_data_export_request` (re-exported from `queries/data_exports.py`) — these are what `export_routes.py` uses. Even if a caller did manage to fire `generate_data_export`, it would crash at the first `db.get_export_request(export_id)` call.

The wired path is anchored at `gateway/server.py:6347–6354` (the `_export_routes.register(app)` block in the `Data export (GDPR)` section), which mounts the four routes from `export_routes.py:register()` (L481–491): `POST /api/account/export`, `GET /api/account/exports`, `GET /api/account/export/{id}/download`, `GET /settings/privacy`.

## Callers per path

### Path A — `gateway/export_routes.py:_build_zip` (WIRED, production)

Synchronous in-process pipeline. ZIP build runs on a 3-worker `ThreadPoolExecutor` (`gateway/export_routes.py:46`).

**Direct callers of `_build_zip`:**
- `gateway/export_routes.py:276` — `_run_export(request_id)` thread-pool worker.

**Callers of `_run_export`:**
- `gateway/export_routes.py:303` — `_enqueue_export(request_id)` (`_executor.submit(_run_export, request_id)`).

**Callers of `_enqueue_export`:**
- `gateway/export_routes.py:325` — `api_request_export(request)` (handles `POST /api/account/export`).

**Callers of `api_request_export` (and the sibling route handlers):**
- `gateway/export_routes.py:481–491` — `register(app)` mounts the four FastAPI routes.
- `gateway/server.py:6347–6354` — production route mount via `_export_routes.register(app)`.
- `gateway/tests/test_export_routes.py:80` — test harness calls `export_routes.register(app)` on a minimal FastAPI app (tests at L42–397).
- `gateway/admin_routes.py:2411` — documentation comment only, no call (references the file by name in a docstring).

**Test references to `_build_zip`-style helpers (non-callers of the function itself):**
- `gateway/tests/test_export_routes.py` — exercises `_export_secret`, `_sign`, `_verify`, signed-URL hardening, and the route handlers end-to-end. Tests do **not** call `_build_zip` directly; coverage of zip contents comes via the route POST/GET path.
- `gateway/tests/test_data_export.py:242` — defines `test_build_zip_produces_valid_archive` but the imported `build_zip` at L243 is from `exports.generator`, **not** `export_routes._build_zip`. The test name is misleading; it covers Path B (see below).

### Path B — `gateway/exports/generator.py:build_zip` (DEAD, test-only)

Designed as an out-of-process ARQ job pipeline (`generate(export_id)` at L965 was intended to be called by an ARQ worker). Never reaches a running gateway.

**Direct callers of `build_zip`:**
- `gateway/exports/generator.py:988` — `generate(export_id)` inside the same module (the public driver).
- `gateway/tests/test_data_export.py:243, 252, 279, 291, 303, 318, 431, 552, 563` — multiple unit tests build a ZIP directly and assert on contents, redaction, and the manifest.

**Callers of `exports.generator.generate` (and `exports.generate` re-export at `__init__.py:11`):**
- `gateway/jobs/export_jobs.py:23` — `from exports import generate; return generate(export_id)`. **This is the only production-shaped caller and it is unreachable** because `jobs/__init__.py` does not import `export_jobs`, so `@register_job("generate_data_export")` at L20 never runs, so the job name `generate_data_export` is never in `job_registry`, so no `enqueue_job("generate_data_export", ...)` call exists anywhere in the codebase (grep for `"generate_data_export"|'generate_data_export'` returns only `export_jobs.py` lines 4, 20, 21).

**Callers of `_collect` (the per-user table fetcher in `exports/generator.py`):**
- `gateway/exports/generator.py:848` — only called by `build_zip` in the same file.
- `gateway/tests/test_data_export.py:362` — one test imports `_collect` directly.
- `gateway/tests/test_exports_generator.py:28–86` — tests for `_safe_query` schema-drift behaviour (imports `from exports import generator`).

**Callers of `sign_download_url` / `verify_download_token` (the signing helpers in `exports/generator.py`):**
- `gateway/exports/generator.py:992` — used inside `generate()` to mint the download link written to `db.update_export_status` (which itself does not exist — see point 3 above).
- `gateway/tests/test_data_export.py:65, 68, 72, 78, 81, 85, 87, 91, 755` — round-trip / tampering tests.

No non-test caller imports `sign_download_url` or `verify_download_token` either. The signing helpers in `exports/generator.py` are independent of the signing helpers `_sign` / `_verify` in `export_routes.py:115–122`, which are the only ones the production routes use.

## Why two paths exist

Reconstructed from `audits/audit_data_export.md` and the commit context: the `exports/` package was the planned successor — designed for ARQ + Redis once the queue moves out-of-process — and covers 51 sections, including 50+ PII-bearing tables that the production `_build_zip` misses (see `audits/audit_data_export.md` CRIT-1). The wired `export_routes.py:_build_zip` was the initial single-process implementation that shipped first; the successor never got plumbed in. The signing-key handling is also forked: `export_routes.py:_export_secret` and `exports.generator._signing_secret` apply slightly different precedence and fallback rules (the latter is the harder-failing version).

## Recommendation — converge on `exports.generator`

Adopt Path B as the canonical implementation. The wired `_build_zip` is functionally a stub: it produces a 6-section ZIP, references a non-existent `db.list_user_notifications`, ships a README that overstates contents (see `audits/audit_data_export.md` CRIT-1), and lacks the schema-drift `errors` manifest that `exports.generator` adds. Path B is the right shape — it just needs to be reachable.

Convergence steps (ordered, each step keeps the system green):

1. **Adapter, not rewrite — bridge `_run_export` to `exports.generator.build_zip`.** In `gateway/export_routes.py:_run_export` (L266–298) replace the `_build_zip(user_id, zip_path)` call at L276 with `manifest = build_zip(user_id, zip_path); size = zip_path.stat().st_size`. `build_zip` returns a manifest dict instead of a byte count, so capture `size` from the resulting file. This keeps the in-process `ThreadPoolExecutor` model (no Redis dependency added) while picking up the 51-section bundle, secret-fallback hardening, and schema-drift `errors` list.
2. **Delete `_build_zip` from `export_routes.py`** along with `_write_json`, `_write_csv`, `_rows_to_list` (L133–158) which become dead helpers once step 1 lands. Keep the route handlers, the executor, `_run_export` (now calling the adapter), the secret helpers, the signing helpers, the rate-limit logic, and `register()`.
3. **Unify the signing key.** Pick one of `_export_secret` (`export_routes.py:81`) vs `_signing_secret` (`exports/generator.py:75`). The latter has the harder-failing semantics already documented in audit findings — reuse it. Currently the routes sign URLs with `_export_secret`, so changing the source needs a coordinated rotation; document the cut-over.
4. **Delete the dead ARQ scaffolding.** `gateway/jobs/export_jobs.py` references DB helpers that don't exist (`get_export_request`, `update_export_status`, `expire_old_exports`). After step 1, the file's `generate_data_export` shim is redundant (the in-process thread pool is the queue). Either (a) delete `gateway/jobs/export_jobs.py` outright, or (b) keep it as a thin wrapper that calls the same adapter and add it to `gateway/jobs/__init__.py` once the missing `db.expire_old_exports` / `get_export_request` / `update_export_status` helpers are added to `queries/data_exports.py`. Option (a) is the cleaner first move; option (b) is only worth the work if you're about to wire Redis.
5. **Delete `gateway/exports/__init__.py`'s re-export of `generate`** if step 4(a) is taken — nothing else imports it.
6. **Re-point tests.** `gateway/tests/test_data_export.py` is already written against `exports.generator.build_zip` — the tests stay green automatically once the adapter lands. The `test_export_routes.py` integration tests already exercise the wired routes and only assert on signing / auth, not zip contents, so they don't need to change.

Do not converge in the opposite direction (port `exports/` functionality into `_build_zip`). That would mean rewriting 700 lines of `_collect`, three scrubbers, the schema-drift manifest, and the conversation-to-markdown renderer back into a thread-pool-coupled module. The successor exists in a clean shape — wire it.

## Risk if left as-is

The wired GDPR export is materially incomplete (omits ~50 PII tables — see `audits/audit_data_export.md` CRIT-1) and the README at `export_routes.py:172–189` falsely advertises sections the code doesn't produce. A regulator-side Subject Access Request audit treats this as a fulfilment failure (GDPR Art. 15, CCPA §1798.110). The dead `exports/` package compounds the risk: every future change made to `exports/generator.py` (additional tables, redaction rules) reads as production code in review, but ships zero behavior change.

## Out of scope

- Pre-release deploy gating (`audits/audit_prerelease*.md`) is intentionally untouched.
- Wider GDPR completeness gaps are tracked in `audits/audit_data_export.md` and `audits/audit_gdpr_export_completeness.md`.
- Signed-URL forgery and impersonation findings — see `audits/audit_data_export.md` CRIT-2 and CRIT-3.
