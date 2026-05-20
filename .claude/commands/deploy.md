---
description: Snapshot the local DBs, then rsync-deploy to the Ubuntu box. Pass a site name (e.g. `/deploy gateway`) to deploy just one; no arg deploys all.
argument-hint: "[site]"
---

You're running the production deploy flow. Be careful — this rsyncs to the live
Ubuntu box and restarts systemd services.

**Site to deploy:** `$ARGUMENTS` (empty = all sites)

## Pre-flight checks (do these first, in parallel)

Run these and surface any failure to the user BEFORE running any deploy command:

1. `git rev-parse --abbrev-ref HEAD` — confirm we're on `main`. If not, STOP
   and ask the user whether they really want to deploy a feature branch.
2. `git status --porcelain` — confirm the working tree is clean. If there are
   uncommitted changes, STOP and show them; deploys should only ship committed
   code.
3. `git fetch origin main && git log HEAD..origin/main --oneline` — confirm we
   are not behind `origin/main`. If we are, STOP and tell the user to pull first.
4. `echo "$DEPLOY_SERVER"` — confirm the env var is set. If empty, STOP.

If any check fails, do not proceed without explicit user confirmation.

## Deploy

Once the checks pass:

1. **Snapshot first.** Run `./snapshot.sh save all "before-deploy-$(date +%Y%m%d-%H%M)"`
   (or scope to the single site if `$ARGUMENTS` is set).
2. **Deploy.** Run `./deploy.sh $ARGUMENTS` (no arg = all sites).
3. Report the snapshot ID and the deploy.sh output to the user.

## Rules

- **Never** pass `--no-verify`, `--force`, or any flag the user didn't ask for.
- If `deploy.sh` fails partway, do not retry automatically — report the error
  and let the user decide.
- Don't touch `auth.db` or any other gitignored DB on the local side; the
  snapshot script handles those via `sqlite3 .backup`.
- If the user asks to deploy `gateway`, remind them this is the auth path
  and confirm once more before proceeding.
