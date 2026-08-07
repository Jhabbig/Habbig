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
| `install-services.sh` | Copies every `narve-*.service` file into `/etc/systemd/system/`, runs `systemctl daemon-reload`, and bootstraps the per-service venvs. Must be run as root. |
| `bootstrap-venvs.sh` | Creates one venv per service under `deploy/venvs/<service>/` from that service's pinned `requirements.txt`. Idempotent — re-runs only reinstall when the requirements changed. |
| `narve-gateway.service` | Runs `gateway/server.py` on port 7000. Depends on Redis. |
| `narve-crypto.service` | Runs `crypto-dashboard/server.py` on port 8000. |
| `narve-stock.service` | Runs `stock-dashboard/stock_dashboard.py` on port 8050. |
| `narve-midterm.service` | Runs `midterm-dashboard/backend/main.py` on port 8051. |
| `narve-traders.service` | Runs `top-traders-dashboard/server.py` on port 8052. |
| `narve-weather.service` | Runs `polymarket_weather_dashboard/server.py` on port 5050. (And/or `polymarket_weather_bot/main.py` depending on which unit you enable.) |
| `narve-sports.service` | Runs `sports-dashboard/sports_dashboard.py` on port 8888. |
| `narve-world.service` | Runs `world-state-dashboard/server.py` on port 7050. |
| `narve-litestream.service` | Runs `litestream replicate` against `/etc/litestream.yml` — continuous off-site SQLite replication. |
| `litestream.yml` | Litestream config: which DBs replicate, to which bucket paths, with what retention. Installed to `/etc/litestream.yml`. |
| `install-litestream.sh` | Installs the litestream binary (pinned v0.3.13), the config, and the service. Run as root. |

## Conventions baked into every unit

- `User=julianhabbig`
- `WorkingDirectory=/home/julianhabbig/Polymarket/<service>`
- `ExecStart=/home/julianhabbig/Polymarket/deploy/venvs/<service>/bin/python <entry_script>` (per-service venv from `bootstrap-venvs.sh`)
- `EnvironmentFile=/home/julianhabbig/Polymarket/gateway/.env.production` (shared)
- `Restart=always`, `RestartSec=5`
- Sandboxing: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`, `ProtectKernelTunables`, `RestrictNamespaces`, etc.

If you change a port or entry script, edit the `.service` file here AND on
the production box, then `sudo systemctl daemon-reload && sudo systemctl
restart narve-<name>`.

## Backups (Litestream)

`narve-litestream.service` continuously replicates these SQLite databases to
the S3/B2 bucket (paths on the prod box, replica paths in the bucket under
`narve/`):

| Database | Replica path |
|---|---|
| `gateway/auth.db` | `narve/auth.db` |
| `Dashboard-x-truth-research-prediction/predictions.db` | `narve/truth-research/predictions.db` |
| `centralbank-dashboard/data/key_store.db` | `narve/centralbank/key_store.db` |
| `midterm-dashboard/backend/data.db` | `narve/midterm/data.db` |
| `forecast-dashboard/data/forecast.sqlite` | `narve/forecast/forecast.sqlite` |

A DB listed in `litestream.yml` that doesn't exist yet on the box is fine —
Litestream v0.3.x idles until the file appears (relevant for
`forecast-dashboard`, which is still being built).

### Verifying replication when the prod box is back

One command — lists every configured DB and its snapshots in the bucket
(each DB should show at least one snapshot no older than an hour):

```bash
set -a; source /etc/default/litestream; set +a; \
litestream databases -config /etc/litestream.yml | awk 'NR>1 {print $1}' \
  | xargs -I{} sh -c 'echo "== {}"; litestream snapshots -config /etc/litestream.yml {}'
```

The `source /etc/default/litestream` is required when running litestream by
hand — the config references `${LITESTREAM_*}` env vars that systemd normally
injects via `EnvironmentFile`. To check a single DB:

```bash
litestream snapshots -config /etc/litestream.yml /home/julianhabbig/Polymarket/gateway/auth.db
```

Restore procedure is in the trailer of `install-litestream.sh` (stop the
owning service + litestream, `litestream restore`, start both).

### .env bootstrap for new services

`deploy.sh` **excludes `.env` and `*.db` from rsync** (and the `P .env*`
protect filter keeps `--delete` from removing box-only env files such as
`.env.production`), and `gateway/.env.production` exists only on the box
(the repo carries `gateway/.env.production.example` as the template) —
secrets and data never travel via deploy. Consequence: on a fresh box, or when adding a new dashboard
service, you must create the env files by hand **before** the first
`systemctl start`:

1. `/home/julianhabbig/Polymarket/gateway/.env.production` — the shared
   `EnvironmentFile` every dashboard unit points at (start from
   `.env.production.example`). The units use the `-` prefix, so a missing
   file does **not** block startup — the service comes up silently missing
   its secrets, which is worse. Create it first.
2. `/etc/default/litestream` — bucket credentials for replication
   (`chmod 600`; format in the header of `install-litestream.sh`). No `-`
   prefix here: `narve-litestream` refuses to start until this file exists.

After creating a new dashboard's DB-producing service, no litestream restart
is needed if its DB was already listed in `litestream.yml` — replication
starts as soon as the file appears. If you add a *new* DB entry to
`litestream.yml`, reinstall the config and restart:
`sudo install -m 0644 deploy/litestream.yml /etc/litestream.yml && sudo systemctl restart narve-litestream`.
