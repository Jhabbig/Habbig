# deploy/ — systemd service units

Systemd unit files for the Ubuntu production box. These run each dashboard as
a long-lived service that auto-restarts on crash. Used **instead of** Docker
on the live box; the Mac dev workflow uses `docker-compose.yml` or
`start_dashboards.sh`.

Install on the production server:

```bash
sudo bash deploy/install-services.sh
sudo systemctl enable --now narve-gateway narve-crypto narve-stock \
                            narve-weather narve-midterm
```

`install-services.sh` is the source of truth for what runs: it installs and
enables the live units, and **stops + disables any parked/merged unit** still
present from an earlier deploy. The gateway serves a parked notice (or a
301 redirect for merged dashboards) on retired subdomains, and the
`/admin/fleet` dashboard shows live state plus the lasting effects of the
trim (captured redirects, parked-page visits, subs still attached).

## Files in this directory

| File | Purpose |
|---|---|
| `install-services.sh` | Installs the live `narve-*.service` files into `/etc/systemd/system/`, stops/disables parked ones, runs `systemctl daemon-reload`. Must be run as root. |
| `narve-gateway.service` | Runs `gateway/server.py` on port 7000. Depends on Redis. |
| `narve-crypto.service` | Runs `crypto-dashboard/server.py` on port 8000 (Market Edge). |
| `narve-stock.service` | Runs `stock-dashboard/stock_dashboard.py` on port 8050 (Market Edge). |
| `narve-midterm.service` | Runs `midterm-dashboard/backend/main.py` on port 8051. |
| `narve-weather.service` | Runs `polymarket_weather_dashboard/server.py` on port 5050 — includes the merged Disasters + Climate sections. |
| `narve-health-monitor.sh` | Cron probe of the live services + public URLs. |
| `parked/` | Unit files for parked/merged dashboards (sports, world, traders, centralbank, disasters, crypto-trackers, whale). Kept for reference; not installed. Move one back up and add it to `SERVICES` to revive it. |

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
