---
description: Restart one dashboard cleanly — kill, wait, start, verify health
argument-hint: <dashboard-name>
---

Restart the `$ARGUMENTS` dashboard. Steps:

1. Resolve port + launch command from `start_dashboards.sh`.
2. **Kill cleanly** — prefer the PID file at `/tmp/dashboard_<name>.pid`; fall back to `lsof -ti :<port>`. Use `kill` (SIGTERM), wait up to 3 seconds for graceful shutdown, then `kill -9` if still alive. Remove the stale PID file.
3. **Verify port is free** — `lsof -ti :<port>` should return nothing.
4. **Start fresh** — activate `venv/`, run the same launch command from `start_dashboards.sh`, redirect stdout+stderr to `/tmp/dashboard_<name>.log`, write new PID to `/tmp/dashboard_<name>.pid`.
5. **Sleep 2 seconds**, then verify:
   - `lsof -ti :<port>` returns the new PID
   - `curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:<port>/healthz` returns 200 (give it up to 3 retries with 2-second sleeps in case of slow startup — crypto-dashboard takes ~90s to load ML ensembles)
6. **Report**: old PID → new PID, healthz status, URL.

If startup fails (no listener after 10 seconds, or healthz never returns 200), tail the last 30 lines of the log and stop. **Do not** try to fix without asking — surface the error and let the user decide.

Special case: if `$ARGUMENTS` is `gateway`, restart it **last** if other dashboards are also being restarted — but for a solo `/dash-restart gateway` just do it. The gateway works fine even if upstream dashboards are temporarily down (it just returns 502 for those subdomains).
