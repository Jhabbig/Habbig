# Audit — `gateway/server.py` admin sections

Scope: every `_require_admin_user` call site in `gateway/server.py` and the immediately surrounding handler. Focus areas: admin guard correctness, CSRF, impersonation safeguards, audit-log entries. Out of scope: handlers registered by `admin_routes.py` and other extracted route modules (`admin_jobs_routes.py`, `admin_emails_routes.py`, …) — those have separate audits.

Method: read `server.py` lines ~5193–7090, the `_require_admin_user` helper at 5246, `_real_admin_user` at 2041, `_can_manage_user` at 6008, `_require_super_admin` at 6022, the `CSRFMiddleware` at 1261, the `ImpersonationMiddleware` block at ~1440–1602, `impersonation.py`, and `security/audit.py`.

`_require_admin_user` call sites located (22 total):

| Line | Handler | Method | Privilege model |
|------|----------|--------|------------------|
| 5681 | `admin_page` | GET `/admin` | admin level ≥1, `page=True` |
| 5699 | `admin_generate_token` | POST `/admin/tokens/generate` | admin level ≥1 |
| 5730 | `admin_revoke_token` | POST `/admin/tokens/revoke` | admin level ≥1 |
| 5756 | `admin_promote` | POST `/admin/users/{id}/promote` | admin + `_can_manage_user` |
| 5778 | `admin_demote` | POST `/admin/users/{id}/demote` | admin + `_can_manage_user` |
| 5800 | `admin_suspend` | POST `/admin/users/{id}/suspend` | admin + `_can_manage_user` |
| 5823 | `admin_unsuspend` | POST `/admin/users/{id}/unsuspend` | admin + `_can_manage_user` |
| 5846 | `admin_mark_enquiry_read` | POST `/admin/enquiries/{id}/read` | admin |
| 5853 | `admin_create_token_from_enquiry` | POST `/admin/enquiries/{id}/create-token` | admin |
| 5899 | `admin_logs_live` | GET `/admin/logs/live` | admin |
| 5925 | `admin_logs_errors` | GET `/admin/logs/errors` | admin |
| 5974 | `admin_logs_search` | GET `/admin/logs/search` | admin |
| 6016 | `_require_super_admin` helper | (called by admin_set_role, admin_grant_subscription, admin_delete_user) | — |
| 6048 | `admin_change_email` | POST `/admin/users/{id}/email` | admin + `_can_manage_user` |
| 6084 | `admin_revoke_user_token` | POST `/admin/users/{id}/revoke-token` | admin + `_can_manage_user` |
| 6107 | `admin_new_token_for_user` | POST `/admin/users/{id}/new-token` | admin + `_can_manage_user` |
| 6164 | `admin_grant_subscription` | POST `/admin/users/{id}/grant` | super admin |
| 6221 | `admin_bulk_users` | POST `/admin/users/bulk` | admin + per-uid `_can_manage_user` |
| 6523 | `admin_audit_log_page` | GET `/admin/audit-log` | admin |
| 6847 | `admin_audit_log_csv` | GET `/admin/audit-log/export.csv` | admin (+per-admin CSV rate limit) |
| 6908 | `admin_subproducts_page` | GET `/admin/subproducts` | admin |

The `_require_super_admin` helper (line 6022) wraps `_require_admin_user(request)` and bumps the floor to admin_level≥2 for: `admin_set_role` (6030), `admin_delete_user` (6196), `admin_grant_subscription` (6163), and the bulk-delete branch (6248).

---

## Severity summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 3 |
| Medium | 7 |
| Low | 6 |
| Info | 4 |
| **Total** | **20** |

---

## Top 5 (highest-impact)

1. **HIGH — `IMPERSONATION_START / END / BLOCKED` audit entries silently fail-open.** Referenced at `admin_routes.py:148`, `admin_routes.py:176`, `server.py:1586` but `AuditAction` class in `security/audit.py:26-74` does not define those constants. The `getattr` lookup raises `AttributeError` inside the `try: … except Exception: pass` wrapper at `audit.py:222-239`. Result: no row in `audit_log` for impersonation start, end, or blocked-request events. Per-impersonation paper trail still lives in `impersonation_actions` (via `db.record_impersonation_action`), but the unified audit-log surface and the CSV export at `/admin/audit-log/export.csv` are missing every impersonation event. Also breaks the admin filter dropdown that enumerates `ACTION_LABELS`.

2. **HIGH — `_require_admin_user` docstring claims a 2FA redirect path that doesn't exist.** Lines 5246-5273. Docstring says callers should return a `RedirectResponse` "when 2FA is required" and raise 303 for API routes — but the function body never inspects 2FA state, never constructs a 303, and never returns a `Response`. Three call sites still defensively branch on `isinstance(user, Response)` (5723, 6533, 6878) — that branch is unreachable. There is no admin-2FA enforcement on any admin POST in this file. Combined with rate-limit-only protection (30 mutations / 5 min per admin), a stolen admin session cookie can promote/demote/suspend/grant/email-change/bulk-delete users at the rate-limit ceiling with no second-factor barrier. Either remove the dead docstring & dead branches or wire real 2FA gating before mutating routes.

3. **HIGH — `admin_set_role` (line 6029) lets a super admin demote themselves to level 0, losing super-admin status with no cool-off.** The handler enforces `_require_super_admin` and accepts `level ∈ [0, 2]` but never checks `user_id == admin["user_id"]`. Combined with `set_user_role` in `queries/auth.py:415` calling `revoke_all_user_sessions` after the UPDATE, a single mistaken click drops the sole super admin out of the role and signs them out. If only one super admin exists, the platform now has zero super admins — only a direct SQL edit can restore the role. Same vulnerability path inside the bulk handler (`admin_bulk_users`, 6248) for the `delete` action: no `uid == admin["user_id"]` self-check there either (the single-user `admin_delete_user` at 6197 does have it). Bulk delete relies only on the magic `uid != 1` filter at 6233 — fragile (assumes user_id 1 is the seed super admin) and doesn't help if user_id 1 was ever removed.

4. **MED — Level-1 admins can promote arbitrarily many regular users to level-1 admin.** `admin_promote` (5754) and the `bulk_action == "promote"` branch (6240) gate only on `_can_manage_user(admin, uid)` which permits level-1 → level-0 (`server.py:6010-6011`). `set_user_admin(uid, True)` then calls `set_user_role(uid, 1)`. Result: a single compromised level-1 cookie can manufacture 30 fresh level-1 admins inside the global mutation cap, each then able to do the same. `_can_manage_user` should distinguish "manage" (suspend/email/token) from "elevate" (promote requires super admin), or the per-action rate ceiling should be tighter than 30/300s for promote.

5. **MED — `admin_bulk_users` (6219) widens admin reach far beyond the per-admin mutation cap of 30/5min.** The handler accepts an arbitrary `user_ids[]` list from the form and applies the chosen action to every uid that survives `_can_manage_user`. Only the *handler call* counts toward the 30/300s cap in `_require_admin_user`. A compromised admin can suspend, demote, or (if super) delete every user in the system in one request — the per-user audit-log entries are also collapsed into a single `USER_BULK_ACTION` row with no per-uid `before/after` snapshot (6258-6265). Forensic reconstruction of the bulk operation depends solely on the `user_ids` JSON in the `after` blob; if the array is large enough to spill, the snapshot of *what each user looked like before the action* is irretrievable.

---

## Full findings (HIGH → INFO)

### HIGH

#### H-1 — Impersonation audit entries silently dropped (see Top 5 #1)
`security/audit.py:26-74` defines `AuditAction` constants but omits `IMPERSONATION_START`, `IMPERSONATION_END`, `IMPERSONATION_BLOCKED`. Three call sites reference them:
- `admin_routes.py:148` (start)
- `admin_routes.py:176` (end)
- `server.py:1586` (blocked)

Each wrapped in `try/except Exception: pass`, so the `AttributeError` is swallowed. Add the constants and corresponding `ACTION_LABELS` entries; without that, the audit-log filter dropdown also won't surface impersonation events as a choice.

The CSV export (`admin_audit_log_csv`, 6878) also uses a string-literal action `"audit.csv_export"` not declared on the `AuditAction` class. That one persists fine (the literal is passed straight through to `insert_audit_log`), but it's not in `ACTION_LABELS` either, so it shows up unstyled in the UI.

#### H-2 — Dead 2FA branch + no real 2FA gating on admin mutations (see Top 5 #2)
`_require_admin_user` docstring at 5246-5258 describes a 2FA redirect that the function body doesn't implement. Three callers (5723, 6533, 6878) defensively branch on `isinstance(user, Response)` for that dead path. Removing the dead branches is one option; the bigger gap is that admin-only routes have no second-factor requirement. Currently the only defence is the 30-mutation/5-minute per-admin-email rate limit (5266-5272). For a stolen admin cookie the meaningful blast radius is therefore "burn 30 promotions, 30 emails-changed, 30 trading-addons, 30 grants — each across separate buckets — in the first 5 minutes before the auditor notices".

#### H-3 — `admin_set_role` allows super admin self-demotion → potential admin lock-out (see Top 5 #3)
Line 6029. Add `if user_id == admin["user_id"]: raise HTTPException(400, "Cannot change your own role")`. The `admin_delete_user` handler at 6197 already has the analogous self-check (`if user_id == admin["user_id"]: raise 400`) — `admin_set_role` should mirror it. Also consider preventing demotion of the last super admin (`count(level=2) == 1`).

### MED

#### M-1 — Horizontal admin proliferation via `admin_promote` (see Top 5 #4)
Lines 5754 and 6240. Either promote should require super admin (matching role-change at 6029) or the rate cap for promote should be far tighter than the generic 30/300s.

#### M-2 — Bulk handler bypasses the per-handler mutation cap (see Top 5 #5)
Line 6219. Either cap `len(user_ids)` to e.g. 25, or count `len(user_ids)` against a separate sliding window keyed on the admin, not just the request.

#### M-3 — `admin_change_email` is a level-1 power despite the inline comment claiming "Super admin"
Line 6053. `_require_admin_user` + `_can_manage_user` admit any level-1 admin to change a level-0 user's email. `log.info("Super admin %s changed email …", admin["email"])` at 6068 is misleading — the actor may be a level-1 admin. Either restrict to super admin (consistent with the `_require_super_admin`-gated grant/delete/role routes) or correct the log line. Changing a user's email is auth material; on top of that, `revoke_all_user_sessions` runs *after* the UPDATE, so a long-lived cookie issued before the change cannot be exercised — good — but the level of authority to change an authentication identifier should match the destructive grant/delete routes.

#### M-4 — Bulk delete has no `uid == admin["user_id"]` self-check
Line 6248. `admin_delete_user` (6197) checks this; the bulk handler relies only on `uid != 1` (6233) and `target_level < 2` (6250). A super admin (level 2) who is *not* user_id 1 can bulk-include themselves in the delete set, since `_can_manage_user` returns True for super-admin-managing-anyone and `target_level >= 2` is already filtered. Wait — line 6250 filters `target_level < 2`, so a super admin *is* protected from being deleted. But a level-1 admin who got their is_admin set to 0 mid-operation (race) could be re-checked. Lower-confidence than M-1/M-2; still worth tightening.

#### M-5 — `admin_grant_subscription` writes an audit `after` blob without a `before` snapshot
Line 6163. The audit row records `after={dashboard_key, plan, duration_days}` but never captures the previous subscription state for that dashboard. If a subscription already existed and was over-written by the grant (e.g. extending the period from 30 days to 365), reconstructing the pre-state is impossible. `db.upsert_subscription` is a write; `before` should call `db.list_subscriptions(user_id)` (or a focused per-dashboard query) and snapshot the matching row.

#### M-6 — Trading-addon grant is level-1; trading is a financial privilege
Line 6192. `admin_toggle_trading_addon` is `_require_admin_user` + `_can_manage_user` (level-1 can target level-0 users). Activating the add-on sets `period_end = now + 30 days`. Granting access to trading flows is at least the same risk class as granting subscription, which is `_require_super_admin`. Consider raising to super admin or wiring a separate audit-action ceiling.

#### M-7 — `admin_logs_live / errors / search` (5919, 5952, 5998) expose log buffer contents with no audit-log entry
Reading from the in-memory ring buffer can surface request paths with `user_id` query params, error-message bodies containing user emails (PII), session-related warnings, etc. The CSV export of `audit_log` is itself audited (6876) — log-tail viewing should be similarly attributed so an investigator can tell which admin pulled the buffer that contained a leaked password reset link.

### LOW

#### L-1 — `admin_audit_log_page` (6523) and `admin_subproducts_page` (6908) are not audited
GET-side access to the forensic surface itself goes unrecorded. Reading the audit log is itself a sensitive event during a credential rotation. Compare to the CSV export (6876) which *is* logged.

#### L-2 — Per-admin rate-limit key uses `email` with `user_id` fallback
Line 5269: `f"admin_mut:{user.get('email') or user.get('user_id')}"`. If `email` is somehow empty/None and `user_id` is reused later, two different admins could ever share a bucket. Very low-probability; keying on `user_id` exclusively is more deterministic and matches the per-user-email security model.

#### L-3 — `admin_bulk_users` per-uid silent skip
Line 6238: `if not _can_manage_user(admin, uid): continue`. The handler doesn't log which uids were rejected. If an admin tries to escalate by stuffing an unrelated uid into the bulk POST, you can't see in `audit_log` that the attempt happened — only that the action succeeded against a smaller set than was requested.

#### L-4 — Magic `uid != 1` filter in bulk handler
Line 6233. Implicit assumption that the seed super admin is at user_id 1. If user_id 1 is ever removed or rotated, this protection silently fails. Replace with `if uid == admin["user_id"]: continue` plus an explicit check against `(target["is_admin"] or 0) >= 2` for the destructive branches.

#### L-5 — `admin_create_token_from_enquiry` (5851) has no rate-limit beyond the generic mutation cap
Single endpoint that does `create_invite_token + mark_enquiry_read`. Per-admin generation already has a dedicated 30/min cap on `admin_generate_token` (5704). The enquiry-driven variant goes around it. Apply the same `admin-tokens-gen:` rate-limit key here too.

#### L-6 — `admin_change_email` does not normalise / lowercase the existing-user check before comparing
Line 6058 lowercases `new_email`, then queries `db.get_user_by_email(new_email)`. Good. But if the DB stores mixed-case in legacy rows, the uniqueness check may miss conflicts. Quick verification of the column collation would clear this; on SQLite default collation is BINARY so `Foo@x` and `foo@x` are distinct. Lower-confidence finding.

### INFO

#### I-1 — `_real_admin_user` dev-bypass auto-creates an admin on localhost
Line 2041, branch at 2071. When the gateway runs against a request whose `is_local_host(request)` returns True, the helper creates a dev user with `is_admin=True` and full subscriptions (`ensure_dev_user`). This is gated by `IS_PRODUCTION`-aware code paths elsewhere, but worth flagging: tests / dev tooling running against a binding addressable from outside localhost would inherit admin powers. Not a server.py bug per se; called out so it's not forgotten.

#### I-2 — Impersonation `BLOCKED_PATH_PATTERNS` are `re.search`, not `re.fullmatch`
`impersonation.py:45-83`. Documented at top of file. `r"/admin"` matches any path containing `/admin`, including e.g. `/admin/impersonations/end` — but that's explicitly allowlisted at 108. Other false-positive collisions (e.g. an unrelated future route containing `/checkout` as a substring) would over-block. Behaviour by design; low-confidence concern.

#### I-3 — CSRF middleware soft-warns PATCH/PUT/DELETE during rollout
Lines 1102-1120, 1287-1356. `CSRF_PATCH_DELETE_ENFORCE` defaults to `false`. Today this matters because the only admin routes in `server.py` that use those verbs would still see a logged-only warning rather than a 403. Audit of `_require_admin_user` call sites finds *all current admin handlers in this file are POST or GET*, so the soft-warn window doesn't expose any of the admin surface in this audit. Worth flipping the flag (`CSRF_PATCH_DELETE_ENFORCE=true`) before any admin PATCH/DELETE route is added.

#### I-4 — `admin_audit_log_csv` rate-limit (6886) keys on `email` only; combine with `user_id` like L-2
Same pattern as L-2 but for CSV export — `f"audit_csv:{user.get('email') or user.get('user_id')}"`. Six exports per 5 minutes is generous for forensic work; combining the key with user_id would be more robust.

---

## CSRF coverage

CSRF middleware (`server.py:1261-1374`):
- Double-submit cookie (`_csrf` cookie + `_csrf` form field or `x-csrf-token` header).
- Enforces on POST always; PATCH/PUT/DELETE in soft-warn mode behind `CSRF_PATCH_DELETE_ENFORCE` (default false).
- Same-origin / same-apex Origin check applies to every mutating verb in production (1329-1342).

All POST admin handlers identified in this audit (every entry in the call-site table whose Method is POST) flow through `CSRFMiddleware.dispatch` and are not in `_CSRF_EXEMPT_POSTS` or `_CSRF_EXEMPT_POST_PREFIXES`. They are CSRF-protected.

GET admin handlers (`/admin`, `/admin/logs/*`, `/admin/audit-log`, `/admin/audit-log/export.csv`, `/admin/subproducts`) are read-only and not in scope for CSRF.

The impersonation banner injection at `server.py:2683-2693` passes `csrf_field=context.get("raw_csrf_field", "")`; the "End session" form in the banner submits to `POST /admin/impersonations/end` which is wrapped in `_ALWAYS_ALLOWED` (impersonation.py:108) and goes through CSRF middleware like any other POST. As long as the rendered `raw_csrf_field` carries a valid token, this is correct.

---

## Impersonation safeguards

Verified in `impersonation.py` and `ImpersonationMiddleware` (`server.py:~1440-1601`):
- Separate cookie `narve_impersonation`, never modifies the admin's session.
- 4-hour TTL; auto-expires; cookie cleared on stale/expired sessions.
- `_BLOCKED_PATTERNS` (45-83) covers password, email, 2fa, api-keys, payment, billing, subscribe, checkout, admin, predictions delete, widgets, intelligence, ai. Method-gated to POST/PUT/PATCH/DELETE.
- `_READ_ALSO_BLOCKED_PATTERNS` (91-98) blocks GET on the destructive UI (account delete page, 2FA QR, API keys, admin).
- Allowlist: only `/admin/impersonations/end` survives the `/admin` blanket block (108).
- Every request during impersonation is recorded in `impersonation_actions` (1554-1559, 1576-1580).
- `_require_admin_user` correctly prefers `_real_admin_user(request)` (5260) so the admin keeps reaching the panel while impersonating.
- `impersonate_start` (`admin_routes.py:113`) enforces 4-char minimum reason, blocks self-impersonation, blocks equal-or-higher admin impersonation (privilege laundering defence).

Gaps:
- Impersonation audit entries to `audit_log` are silently dropped — see H-1 above. Impersonation history is recoverable only via the `impersonation_actions` table & the `/admin/impersonations/{id}` detail page.
- `_BLOCKED_PATTERNS` uses `re.search` not `re.fullmatch` — see I-2.

---

## Audit-log entries — per-handler coverage

| Handler | Audited? | Notes |
|---------|----------|-------|
| `admin_page` | No | GET; no audit |
| `admin_generate_token` | Yes (5713-5723) | `TOKEN_GENERATE`, target=token prefix |
| `admin_revoke_token` | Yes (5741-5750) | `TOKEN_REVOKE` |
| `admin_promote` | Yes (5763-5772) | `USER_PROMOTE_ADMIN`, before/after snapshot |
| `admin_demote` | Yes (5785-5794) | `USER_DEMOTE_ADMIN`, before/after snapshot |
| `admin_suspend` | Yes (5808-5817) | `USER_SUSPEND` |
| `admin_unsuspend` | Yes (5831-5840) | `USER_UNSUSPEND` |
| `admin_mark_enquiry_read` | **No** | Read-flag flip — low impact; could add for completeness |
| `admin_create_token_from_enquiry` | **No** | Mints a real token; **should** log `TOKEN_GENERATE` — see L-5 |
| `admin_logs_live / errors / search` | **No** | Sensitive read — see M-7 |
| `admin_set_role` | Yes (6039-6048) | `USER_ROLE_CHANGE`, notes=level |
| `admin_change_email` | Yes (6069-6078) | `USER_EMAIL_CHANGE` |
| `admin_revoke_user_token` | Yes (6098-6107) | `TOKEN_REVOKE`, notes=revoke_from_user |
| `admin_new_token_for_user` | Yes (6125-6134) | `TOKEN_GENERATE`, notes=replacement_token |
| `admin_grant_subscription` | Partial (6177-6188) | After-only; missing `before` snapshot — see M-5 |
| `admin_toggle_trading_addon` | Yes (6178-6188) | `USER_TRADING_ADDON`, after-only (no prior period_end captured) |
| `admin_delete_user` | Yes (6206-6221) | `USER_DELETE_COMPLETED`, before snapshot |
| `admin_bulk_users` | Partial (6256-6266) | Single rolled-up `USER_BULK_ACTION` with no per-uid before/after — see Top 5 #5 |
| `admin_audit_log_page` | **No** | Reading the audit log itself is unaudited — see L-1 |
| `admin_audit_log_csv` | Yes (6876-6885) | `"audit.csv_export"` string literal; not in `AuditAction`/`ACTION_LABELS` |
| `admin_subproducts_page` | **No** | Read-only MRR overview |

---

## Things that look fine

- Admin guard correctly prefers `_real_admin_user` over `current_user` during impersonation (5260) so an admin impersonating a target user still passes admin gating without privilege-laundering through the target.
- Per-handler rate limits stack with the generic 30/300s mutation cap on every state-changing admin route.
- `admin_delete_user` (6197) checks self-delete, blocks deletion of another super admin (6202-6205), and cascades cleanup across `sessions` + `subscriptions`.
- `admin_change_email` revokes all sessions for the target user (6064) after changing auth material.
- CSRF middleware Origin check applies to every mutating verb in production regardless of the PATCH/DELETE rollout flag.
- Audit log table is append-only (no `DELETE FROM audit_log` anywhere in the gateway tree).
- Impersonation blocked-action UI is hard-coded HTML (no template injection surface).
- `_can_manage_user` fail-closes when target user is missing (6011) — no silent privilege escalation on a 404.

---

## Suggested follow-ups (not changes — flags only)

1. Land `IMPERSONATION_START / END / BLOCKED` constants in `security/audit.py` (H-1).
2. Either remove the dead 2FA-redirect branches or wire real 2FA on admin mutations (H-2).
3. Add `user_id == admin["user_id"]` and "last super admin" check to `admin_set_role` (H-3).
4. Restrict `admin_promote` to super admin (or apply a tight per-action ceiling) (M-1).
5. Cap `len(user_ids)` in `admin_bulk_users` and count each as an individual mutation (M-2).
6. Decide whether `admin_change_email` (M-3) and `admin_toggle_trading_addon` (M-6) should be super-admin-only.
7. Audit-log entries for `admin_logs_*` reads, `admin_audit_log_page` views, and `admin_create_token_from_enquiry` (M-7, L-1, L-5).
8. Add `before` snapshot to `admin_grant_subscription` (M-5).
9. Replace `uid != 1` magic with `uid == admin["user_id"]` self-check + explicit `(target["is_admin"] or 0) >= 2` block (L-4).
10. Unify rate-limit keys on `user_id` (L-2, I-4).
