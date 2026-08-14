---
description: Show health of every dashboard — port up/down, healthz status
---

Report the live status of every dashboard in the suite.

For each of the 12 dashboards (gateway:7000, crypto:8000, stock:8050, midterm:8051, traders:8052, weather:5050, sports:8888, world:7050, voters:7051, climate:7052, health:7053, cb:7060):

1. Is anything listening on the port? (`lsof -ti :<port>`)
2. If yes, what's the PID? Is there a `/tmp/dashboard_<name>.pid` file matching it?
3. Curl `http://localhost:<port>/healthz` with a 2-second timeout and capture the HTTP code.
4. If the dashboard is down, check `/tmp/dashboard_<name>.log` exists and grab the last 3 lines.

Output as a single table:

```
Dashboard       Port   PID      Health    Notes
gateway         7000   12345    200       —
climate         7052   —        —         not running (last log: ImportError: ...)
weather         5050   23456    503       up but unhealthy
...
```

End with a one-line summary: `N/12 up, M unhealthy, K down`.

Do **not** try to fix anything — this is a status read, not a repair. If something looks broken, just surface it; the user decides next steps.
