# Audit — `gateway/preferences_routes.py`

**Date:** 2026-05-15
**Auditor:** automated adversarial pass
**Scope requested:** preference-key allowlist, email-unsubscribe token validation, density/theme value-set bounds
**Status:** **TARGET FILE NOT FOUND**

---

## 0. Result summary

| Severity | Count |
|----------|------:|
| Critical | 0 |
| High     | 0 |
| Medium   | 0 |
| Low      | 0 |
| Info     | 1 |

**Top 3 findings:**

1. **INFO-1 — Audit target does not exist.** `gateway/preferences_routes.py` is not present in the working tree and has never been committed (verified via `git log --all --full-history`). No allowlist, token-validation, or value-bound logic can be audited in a file that does not exist.
2. **(none)** — second finding slot intentionally empty; no additional findings produced because there is no source to inspect against the requested scope.
3. **(none)** — third finding slot intentionally empty; see "Re-aim suggestions" below for likely intended targets the user may have meant.

No code changes were made (per task hard rule).

---

## 1. Verification of absence

Commands run from repo root `/Users/shocakarel/Habbig`:

```
ls gateway/preferences*.py                # zsh: no matches found
find . -name 'preferences_routes.py' \
       -not -path '*/venv/*' \
       -not -path '*/.git/*'              # (empty)
find . -name '*pref*routes*.py' \
       -not -path '*/venv/*' \
       -not -path '*/.git/*'              # (empty)
git log --all --full-history -- '**preferences_routes.py'   # (empty)
git log --all --diff-filter=D --name-only --pretty=format: \
       | grep -i preference                                # (empty)
```

The file has never existed under any commit reachable from `origin/main` or any other ref. It was not recently deleted.

---

## 2. Re-aim suggestions (adjacent preference surfaces actually present)

If the auditor wants the same three scope items (allowlist / unsubscribe token / theme bounds) audited against the **real** preference surface, the candidate files are:

| File | Endpoints | Relevance to requested scope |
|------|-----------|------------------------------|
| `gateway/notification_routes.py` lines 202–234 | `GET /api/notifications/preferences`, `PATCH /api/notifications/preferences` | Closest match for "preference-key allowlist". Uses `**kwargs` splat into `db.set_notification_preferences`; worth checking for arbitrary key writes. |
| `gateway/environmental_routes.py` line 204; `gateway/intelligence_routes.py` line 303 | `PATCH /api/user/preferences/environmental` | Bounded value-set check (show flag + unit) — same shape as the "density/theme bounds" item. |
| `gateway/migrations/002_email_unsubscribes.py` + handler in `gateway/server.py` | Email unsubscribe token flow | Direct match for "email-unsubscribe token validation". |
| `gateway/server.py` around lines 7193–7768 | Landing-preference, environmental prefs, bankroll/Kelly prefs | Catch-all for user-pref writes — needs the same allowlist sanity. |
| `gateway/admin_shell.py:200`, `gateway/pwa_middleware.py:62–73` | `data-theme` cookie / CSS attribute | Theme value comes from `narve-theme` / `betyc-theme` cookie; not validated server-side at read sites — relevant to "theme value-set bounds" if there is ever a server write. |

Density preference does not appear to be persisted server-side; only referenced in a comment at `gateway/server.py:7193` and in design copy at `gateway/pwa_middleware.py:141`. If density is supposed to round-trip to the server, that endpoint also does not exist yet.

---

## 3. Scope-by-scope nil report

### 3.1 Preference-key allowlist
**Not applicable.** No `preferences_routes.py` to inspect. If `notification_routes.py:210` is the intended target, the PATCH handler accepts a JSON body and forwards it via `**kwargs` to a DB setter — that is the place to verify an explicit allowlist exists. Out of scope for this audit per the file path requested.

### 3.2 Email-unsubscribe token validation
**Not applicable.** No `preferences_routes.py`. The actual unsubscribe handler is reached via the migration at `gateway/migrations/002_email_unsubscribes.py` and a server route under `gateway/server.py` / `gateway/notifications.py`. Audit those separately.

### 3.3 Density / theme value-set bounds
**Not applicable.** No `preferences_routes.py`. Theme is currently a client-side cookie read in `pwa_middleware.py` and `admin_shell.py`; density is not persisted server-side at all. There is no write endpoint to bound.

---

## 4. Recommendation

Re-issue the audit task against one of:

- `gateway/notification_routes.py` (for the allowlist concern), or
- `gateway/server.py` lines 7180–7800 (for the cluster of user-pref writes), or
- whichever file the auditor expected `preferences_routes.py` to be — please confirm path.

No further work performed on this file.
