---
description: Thoroughly audit a file or folder — report ALL findings, not just criticals
argument-hint: <path>
---

Audit `$ARGUMENTS` thoroughly. Per project convention: **fix all findings, not just criticals.** Don't cherry-pick.

## Scope

If `$ARGUMENTS` is a file → audit that file.
If a folder → audit every source file in it (skip `__pycache__`, `node_modules`, `venv`, `.git`, `cache/`, `.snapshots`).
If empty → ask the user what to audit.

## Categories to check (every category, every file)

1. **Bugs** — wrong logic, off-by-ones, missing returns, unawaited async, exception swallowing, race conditions, wrong types, NaN/None mishandling.
2. **Security** — secrets in code, missing auth checks, SQL injection, path traversal, unvalidated input, weak crypto, exposed admin routes, CORS holes, missing CSRF on state-changing routes. The gateway is the auth boundary — dashboards trusting `X-Gateway-User-*` headers should reject requests where they're absent.
3. **Reliability** — silent failures, missing error handling, retries that infinite-loop, timeouts missing, resource leaks (open files / connections / event listeners), no graceful shutdown.
4. **Performance** — N+1 queries, blocking I/O in async contexts, unnecessary re-renders, large in-memory loads, unbounded caches.
5. **Correctness drift** — code that no longer matches its docstring/comment, dead branches, stale TODOs, duplicate logic across files.
6. **Conventions** — violates the rules in `CLAUDE.md` (e.g. dashboards depending on each other, `gateway/config.json` keys renamed, healthz pattern broken).
7. **Lint** — run `ruff check $ARGUMENTS` and include any F821 hits as criticals.

## Reporting format

Group findings by **severity** (critical / high / medium / low) within each category. For each finding:
- `path:line` location
- One-sentence description of the problem
- One-sentence recommended fix

End with a **fix plan** — ordered list of every finding, all severities, that you will address. Then ask: *"Fix all N findings now? (y/skip-low/select)"*

- `y` → fix everything in order, run `ruff check` after, summarize changes
- `skip-low` → fix critical/high/medium only
- `select` → user picks which to fix

**Do not** silently downgrade or omit findings. The user will check.

**Do not** commit or push. Just edit files in place.
