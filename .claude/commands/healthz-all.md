---
description: Curl /healthz on every dashboard (local AND prod) — table report
---

Report `/healthz` status of every dashboard, both locally and on prod. Two columns: local + prod. For each of the 12 dashboards (gateway:7000, crypto:8000, stock:8050, midterm:8051, traders:8052, weather:5050, sports:8888, world:7050, voters:7051, climate:7052, health:7053, cb:7060):

**Local:**
- `curl -sf -o /dev/null -w "%{http_code}\n" --max-time 3 http://localhost:<port>/healthz`

**Prod:** use the `narve-prod` MCP tool `prod_curl_local` with `url=http://localhost:<port>/healthz`.

Render as one table:

```
Dashboard       Port   Local    Prod     Notes
gateway         7000   200      200      —
climate         7052   200      000      prod missing
weather         5050   timeout  200      local down
...
```

End with a one-line summary: `Local N/12, Prod M/12`. If any dashboard returns 5xx or timeout, surface the count in the summary.

Do not try to fix anything — read-only diagnostic.
