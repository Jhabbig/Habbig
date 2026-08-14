---
description: Deploy one (or all) dashboards to the Ubuntu prod box, with snapshot first
argument-hint: <dashboard-name> [--message "<note>"]
---

Deploy to production using the existing `deploy.sh` flow.

Argument: `$ARGUMENTS` — typically a site name (e.g. `gateway`, `crypto-dashboard`, `climate-dashboard`). If empty, deploys **all** sites. May include `--message "<note>"` to label the pre-deploy snapshot.

Steps:

1. **Pre-flight checks** — fail fast and tell the user, don't try to fix:
   - `DEPLOY_SERVER` env var is set (e.g. `julianhabbig@100.69.44.108`).
   - SSH to the server works: `ssh -o ConnectTimeout=5 -o BatchMode=yes "$DEPLOY_SERVER" 'echo ok'`.
   - The named site (if specified) appears in the `SITES` array in `deploy.sh`.
   - Working tree is clean (`git status --porcelain` empty) — if not, **show the dirty files and ask** before proceeding. Don't auto-stash.
2. **Show the diff** that's about to ship: `git log --oneline @{upstream}..HEAD 2>/dev/null` (or `git diff --stat HEAD` if no upstream). One-line summary.
3. **Run deploy.sh** verbatim — `./deploy.sh $ARGUMENTS`. Stream its output. The script handles snapshot + rsync.
4. **Post-deploy verify** — for the gateway or any dashboard with a public subdomain, curl its `/healthz` endpoint via the public URL and report the HTTP status.
5. **Reminder** the script prints already: services on the server may need restart. If the user mentioned restarting, ssh and run `sudo systemctl restart polymarket-<name>`. Otherwise just remind them.

Do **not** push to git. Do **not** edit any files. This command only runs the existing deploy script after safety checks.

If something fails mid-deploy, the user can revert via:
```
ssh "$DEPLOY_SERVER" "cd ~/Polymarket && ./snapshot.sh restore <id>"
```
Surface that command in your output.
