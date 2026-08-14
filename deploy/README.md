# deploy/ — systemd service units

Systemd unit files for the Ubuntu production box. These run each dashboard as
a long-lived service that auto-restarts on crash. Used **instead of** Docker
on the live box; the Mac dev workflow uses `docker-compose.yml` or
`start_dashboards.sh`.

Install on the production server:

```bash
sudo bash deploy/install-services.sh
sudo systemctl start narve-crypto narve-stock narve-weather narve-midterm \
     narve-sports narve-world narve-traders narve-centralbank narve-truth \
     narve-airace narve-crypto-trackers narve-religion narve-whale narve-voters
sudo systemctl start narve-gateway
```

`install-services.sh` is the source of truth for what runs: the whole fleet —
15 `narve-*` units (gateway + 14 dashboards) — is live, and the script
installs and enables every one of them. The only retired unit is
`narve-disasters`, which was merged into the Weather dashboard; its unit file
is kept under `parked/` for reference, and the installer stops, disables, and
removes it from any box still carrying it from an earlier deploy. The gateway
301-redirects the retired subdomain to the absorbing product, and the
`/admin/fleet` dashboard shows live state plus the lasting effects of the
merge (captured redirects, parked-page visits, subs still attached).

## Files in this directory

| File | Purpose |
|---|---|
| `install-services.sh` | Installs all 15 live `narve-*.service` files into `/etc/systemd/system/`, stops/disables/removes merged ones (`narve-disasters`), runs `systemctl daemon-reload`, enables everything on boot. Must be run as root. |
| `narve-gateway.service` | Runs `gateway/server.py` on port 7000. Depends on Redis. |
| `narve-crypto.service` | Runs `crypto-dashboard/server.py` on port 8000 (Market Edge). |
| `narve-stock.service` | Runs `stock-dashboard/stock_dashboard.py` on port 8050 (Market Edge). |
| `narve-midterm.service` | Runs `midterm-dashboard/backend/main.py` on port 8051. |
| `narve-weather.service` | Runs `polymarket_weather_dashboard/server.py` on port 5050 — includes the merged Disasters + Climate sections. |
| `narve-sports.service` | Runs `sports-dashboard/sports_dashboard.py` on port 8888. |
| `narve-world.service` | Runs `world-state-dashboard` via uvicorn on port 7050. |
| `narve-traders.service` | Runs `top-traders-dashboard/server.py` on port 8052. |
| `narve-centralbank.service` | Runs `centralbank-dashboard` via uvicorn on port 7060. |
| `narve-truth.service` | Runs `Dashboard-x-truth-research-prediction` via uvicorn on port 18789. |
| `narve-airace.service` | Runs `ai-race-dashboard/server.py` on port 7070. |
| `narve-crypto-trackers.service` | Runs `crypto-trackers-dashboard` via uvicorn on port 7054. |
| `narve-religion.service` | Runs `religion-dashboard/server.py` on port 7062. |
| `narve-whale.service` | Runs `whale-dashboard/backend/main.py` on port 8053. |
| `narve-voters.service` | Runs `voters-dashboard/server.py` on port 7051. |
| `narve-health-monitor.sh` | Cron probe of the fleet + public URLs. Derives its service list from the installed `narve-*.service` units at runtime, so new units are monitored automatically. |
| `parked/` | Unit files for merged dashboards — currently only `narve-disasters.service` (absorbed into Weather). Kept for reference; not installed. To revive one, move it back up and add it to `SERVICES` in `install-services.sh`. |

## Conventions baked into every unit

- `User=julianhabbig`
- `WorkingDirectory=/home/julianhabbig/Polymarket/<service>`
- `ExecStart=/home/julianhabbig/Polymarket/venv/bin/python <entry_script>`
- `EnvironmentFile=/home/julianhabbig/Polymarket/gateway/.env.production` (shared)
- `Restart=always`, `RestartSec=5`
- Sandboxing: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`, `ProtectKernelTunables`, `RestrictNamespaces`, etc.

If you change a port or entry script, edit the `.service` file here AND on
the production box, then `sudo systemctl daemon-reload && sudo systemctl
restart narve-<name>`.
