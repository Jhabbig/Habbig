# deploy/ — systemd service units

Systemd unit files for the Ubuntu production box. These run each dashboard as
a long-lived service that auto-restarts on crash. Used **instead of** Docker
on the live box; the Mac dev workflow uses `docker-compose.yml` or
`start_dashboards.sh`.

Install on the production server:

```bash
sudo bash deploy/install-services.sh
sudo systemctl enable --now narve-gateway narve-crypto narve-weather \
                            narve-sports narve-world narve-midterm \
                            narve-traders narve-stock
```

## Files in this directory

| File | Purpose |
|---|---|
| `install-services.sh` | Copies every `narve-*.service` file into `/etc/systemd/system/` and runs `systemctl daemon-reload`. Must be run as root. |
| `narve-gateway.service` | Runs `gateway/server.py` on port 7000. Depends on Redis. |
| `narve-crypto.service` | Runs `crypto-dashboard/server.py` on port 8000. |
| `narve-stock.service` | Runs `stock-dashboard/stock_dashboard.py` on port 8050. |
| `narve-midterm.service` | Runs `midterm-dashboard/backend/main.py` on port 8051. |
| `narve-traders.service` | Runs `top-traders-dashboard/server.py` on port 8052. |
| `narve-weather.service` | Runs `polymarket_weather_dashboard/server.py` on port 5050. (And/or `polymarket_weather_bot/main.py` depending on which unit you enable.) |
| `narve-sports.service` | Runs `sports-dashboard/sports_dashboard.py` on port 8888. |
| `narve-world.service` | Runs `world-state-dashboard/server.py` on port 7050. |

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
