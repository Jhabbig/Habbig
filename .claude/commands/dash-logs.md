---
description: Tail the log for one dashboard
argument-hint: <dashboard-name> [lines]
---

Tail the log for `$ARGUMENTS` — usage:
- `/dash-logs climate` → last 50 lines of `/tmp/dashboard_climate.log`
- `/dash-logs climate 200` → last 200 lines
- `/dash-logs climate -f` → follow (tail -f) for ~30 seconds, then stop

Resolve the log path: `/tmp/dashboard_<name>.log`. If the file doesn't exist, check whether the dashboard is even running (`lsof -ti :<port>`) and report:
- not running and no log → "<name> hasn't been started this session"
- running but no log → check process command line via `ps -p <pid> -o command=` and report what you find

After tailing, if the log shows obvious issues (tracebacks, "ERROR" lines in last 20 lines, repeated retry messages), surface them in a one-paragraph summary at the bottom. Don't try to fix — just flag.

Special case: `gateway` log is `/tmp/dashboard_gateway.log` (matches the convention).
