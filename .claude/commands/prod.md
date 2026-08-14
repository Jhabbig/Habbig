---
description: One-shot prod state dump — services, ports, uptime, disk
---

Print a quick state dump of the Ubuntu prod box. Use the `narve-prod` MCP tools, in this order:

1. `prod_systemctl_status_all` — every polymarket-* unit's active state + since-when
2. `prod_listening_ports` — what's actually listening
3. `prod_uptime` — uptime + load + memory
4. `prod_disk_free` — disk usage

Render as a single readable report with section headers. End with a one-line **assessment** — green/yellow/red and what's wrong (e.g. "🟢 all green — 12/12 services active, 11/12 ports listening, RAM 8.2/16 GB, disk 32%").

If anything is `failed` or missing a port, **also** call `prod_journalctl_tail` for that unit (last 20 lines) and include the tail in the report.

Do **not** restart anything. Do **not** edit anything. Read-only assessment.
