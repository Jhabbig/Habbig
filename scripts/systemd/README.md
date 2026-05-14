# narve.ai systemd units

Production unit files for the gateway and the seven subproducts that live in this repo. Authored here so they ship with the code; copied to `/etc/systemd/system/` on the prod box (`julianhabbig@`) once vetted.

These replace the ad-hoc `nohup` / `setsid` launches that don't survive reboots.

## Units in this directory

| Unit | Port | Working dir |
| --- | --- | --- |
| `narve-gateway.service` | 7000 | `~/Habbig` (runs `uvicorn server:app --app-dir gateway`) |
| `narve-voters.service` | 7051 | `~/Habbig/voters-dashboard` |
| `narve-climate.service` | 7052 | `~/Habbig/climate-dashboard` |
| `narve-disasters.service` | 7060 | `~/Habbig/disasters-dashboard` |
| `narve-whale.service` | 8053 | `~/Habbig/whale-dashboard` |
| `narve-centralbank.service` | 7061 | `~/Habbig/centralbank-dashboard` |
| `narve-health.service` | 7053 | `~/Habbig/world-health-dashboard` |
| `narve-love.service` | 7062 | `~/Habbig/love-dashboard` (server.py not yet present — enable after the service has a runnable entrypoint) |

The port assignments are baked into each subproduct's `server.py`; the unit files don't override them, so the unit and the code must stay in sync.

## Out of scope

Six original subproducts live ONLY on the server, not in this repo: **sports, weather, world, crypto, midterm, top-traders**. Their unit files (if you want them) must be authored on the server directly against whatever path their code lives in.

## Install on prod

```bash
# From the repo root on the prod box
sudo cp scripts/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## Enable on boot

```bash
sudo systemctl enable \
  narve-gateway \
  narve-voters \
  narve-climate \
  narve-disasters \
  narve-whale \
  narve-centralbank \
  narve-health
# narve-love only once love-dashboard/server.py exists
```

## Start / stop / restart

```bash
sudo systemctl start narve-gateway
sudo systemctl stop  narve-gateway
sudo systemctl restart narve-gateway     # use after a deploy
sudo systemctl status narve-gateway
```

A typical post-deploy is:

```bash
sudo systemctl restart narve-gateway narve-voters narve-climate ...
```

## Logs

All stdout/stderr is routed to journald. Tail with:

```bash
journalctl -u narve-gateway -f
journalctl -u narve-voters --since "10 min ago"
journalctl -u narve-gateway -p err          # errors only
```

The `SyslogIdentifier=` line in each unit means the journal tag matches the service name (`narve-gateway`, `narve-voters`, ...).

## Design notes

- **Single shared env file.** All units load `/home/julianhabbig/Habbig/gateway/.env` (with the `-` prefix so a missing file is non-fatal). Subproducts pull from the same env as the gateway today; if that ever diverges, add a per-service `EnvironmentFile=` line.
- **`Restart=on-failure` + 5s `RestartSec`.** Crashes restart automatically. The `StartLimitBurst=3` / `StartLimitIntervalSec=60` pair prevents a crashloop from hammering the box — three failures inside a minute and systemd gives up until manually kicked.
- **Hardening.** `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only` are belt-and-suspenders. Each service gets `ReadWritePaths=` only for its own dashboard dir and `/tmp`. The gateway gets the whole `~/Habbig` tree because it touches multiple subproduct paths.
- **Gateway dependency on Tailscale.** `narve-gateway.service` has `After=tailscaled.service` because the gateway's internal health checks reach subproducts over Tailscale; starting before `tailscaled` is up causes spurious failures at boot.
- **No `WantedBy=` chain between units.** Each service is independent; the gateway proxies to subproducts but does not require them to be running to start. This keeps a single subproduct failure from cascading.

## Verify

After install:

```bash
sudo systemctl is-enabled narve-gateway        # enabled
sudo systemctl is-active  narve-gateway        # active
ss -ltnp | grep -E '7000|7051|7052|7053|7060|7061|8053'
```
