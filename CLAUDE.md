# Polymarket / narve.ai — working notes for Claude

This repo is the **narve.ai** dashboard suite — a gateway plus N self-contained dashboards.
For full directory layout and setup, see [README.md](README.md). This file is the orientation and house rules.

## Architecture in one paragraph

The **gateway** at `gateway/server.py` (port 7000) is the single entry point. Subdomain routing: `<sub>.narve.ai` → reverse-proxies to a dashboard's local port. Apex `narve.ai` serves login / signup / billing pages. Each dashboard is its own FastAPI/Flask process owning its own folder, deps, DB, and `Dockerfile`. The gateway forwards `X-Gateway-User-Id` / `X-Gateway-User-Email` headers downstream so dashboards never auth themselves. Auth is custom (PBKDF2 + SQLite at `gateway/auth.db`); subscriptions are per-dashboard with Stripe price IDs in `gateway/config.json`.

## House rules (read before editing)

- **Audit thoroughly.** When asked to audit, fix **all** findings, not just criticals. The user expects thoroughness — don't cherry-pick.
- **Each dashboard stays self-contained.** Don't merge code across dashboard folders. The gateway routes; it does not import.
- **Never rename top-level keys** in `gateway/config.json` (e.g. `crypto`, `weather`, `top_traders`). They're tied to the `subscriptions` DB table. The `subdomain` value inside is renameable.
- **Don't add comments unless the *why* is non-obvious.** No "what does this code do" prose.
- **Trust the existing healthcheck pattern** in `docker-compose.yml`. Don't replace it with curl-based variants.

## Stack

- Python 3 (venv at `venv/`). FastAPI for most dashboards (uvicorn); Flask for `polymarket_weather_dashboard`. SQLite per-service. Optional Redis (used by gateway).
- **Lint:** `ruff check <path>` — config in `ruff.toml` (200-char lines, F821 only — bugs, not style).
- Each dashboard exposes `/healthz` returning 200 — required for docker-compose dependency ordering.

## Ports (canonical — also in [start_dashboards.sh](start_dashboards.sh))

| Port | Service | Subdomain |
|---|---|---|
| 7000 | gateway | apex (narve.ai) |
| 8000 | crypto-dashboard | crypto |
| 8050 | stock-dashboard | markets |
| 8051 | midterm-dashboard | midterm |
| 8052 | top-traders-dashboard | traders |
| 5050 | polymarket_weather_dashboard | weather |
| 8888 | sports-dashboard | sports |
| 7050 | world-state-dashboard | world |
| 7051 | voters-dashboard | voters |
| 7052 | climate-dashboard | climate |
| 7053 | world-health-dashboard | (TBD) |
| 7060 | centralbank-dashboard | cb |

## Common commands

```bash
./start_dashboards.sh start|stop|restart|status   # local launcher (no Docker)
docker compose up --build                          # full stack via Docker
./deploy.sh                                        # rsync to Ubuntu prod box
./snapshot.sh save all "<label>"                   # local backup before risky changes
ruff check <dir>                                   # lint
```

Local subdomain testing: `http://crypto.localhost:7000` — browsers auto-resolve `*.localhost`.

## Adding a new dashboard

1. Create `<name>-dashboard/` with `server.py`, `requirements.txt`, `Dockerfile`, `static/`, `/healthz` route. Bind to its own port.
2. Add an entry to [gateway/config.json](gateway/config.json) under `dashboards`: pick a stable internal `key`, the `subdomain`, the `target` port, display fields, and Stripe price IDs (use `TODO_*_STRIPE_*` placeholders if not minted yet).
3. Add a launch block in [start_dashboards.sh](start_dashboards.sh) and a service in [docker-compose.yml](docker-compose.yml) (with the same healthz pattern).
4. Add a Cloudflare DNS route for the subdomain (production only).

The dashboard handles its own logic; the gateway only routes and gates.

## Gotchas

- **Gateway port 7000 collides with macOS AirPlay Receiver.** `start_dashboards.sh` honours `GATEWAY_PORT=7001 ./start_dashboards.sh start` if needed.
- **Frontend `lib/` directories are silently swallowed by `.gitignore`** unless explicitly unignored. See `midterm-dashboard/frontend/src/lib/` for the existing exception. If you add another frontend, replicate the rule.
- **Gateway is the sole auth enforcement point.** Internal dashboard login pages exist (legacy crypto/sports) but sit unused — the gateway's `X-Gateway-User-*` headers are authoritative.
- **WebSocket support is per-dashboard.** Set `supports_websocket: true` in `config.json` for crypto and sports; default is false.
- **Production = Ubuntu box** (`100.69.44.108` via Tailscale). Reached via `ssh julianhabbig@100.69.44.108`. Systemd units in `deploy/` (one per dashboard, all named `polymarket-*`).
- **Backups:** big pre-gateway snapshot at `../Polymarket_backup_20260404_pre_gateway/` (sibling dir). Don't rsync into it.

## When you finish a task

- Run `ruff check` on the dirs you touched. Only F821 errors should ever block.
- For UI changes, verify with the preview tools, not by claiming success blindly.
- Don't commit or push unless the user explicitly asks.
