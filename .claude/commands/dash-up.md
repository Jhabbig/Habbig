---
description: Start one dashboard, tail its log, and open it in the browser
argument-hint: <dashboard-name>
---

Bring up a single dashboard from the Polymarket suite.

Argument: `$ARGUMENTS` (e.g. `climate`, `weather`, `crypto`, `gateway`, `centralbank`).

Steps:

1. Open `start_dashboards.sh` and locate the launch block for `$ARGUMENTS`. Extract the **port**, **script path**, and **launch command** (some are direct `python3 file.py`, others use `python3 -m uvicorn server:app --host 127.0.0.1 --port N`).
2. Check whether anything is already listening on that port: `lsof -ti :<port>`. If yes, ask the user before killing it. If no, proceed.
3. Activate the project venv (`source venv/bin/activate`) before launching.
4. Start the dashboard in the background, redirecting stdout+stderr to `/tmp/dashboard_<name>.log`. Save the PID to `/tmp/dashboard_<name>.pid` (matches the convention in `start_dashboards.sh`).
5. Sleep ~2 seconds, then `tail -20 /tmp/dashboard_<name>.log` to verify it booted cleanly.
6. Confirm `lsof -ti :<port>` shows the new PID.
7. Report the local URL: `http://localhost:<port>` and the subdomain test URL: `http://<subdomain>.localhost:7000` (look up the subdomain in `gateway/config.json`).
8. Do **not** open a browser tab automatically — just print the URL.

If `$ARGUMENTS` is empty, list the available dashboard names from `start_dashboards.sh` and ask the user to pick one.

If the dashboard fails to boot (port stays empty / log shows traceback), surface the last 30 log lines and stop — don't try to fix it without asking.
