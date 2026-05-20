# Repo guide for Claude

Monorepo of prediction-market dashboards + trading bots, unified behind one
auth/billing gateway. Each dashboard is its own service on its own port.
Production target: a single Ubuntu box deployed via rsync.

## Layout (port → directory)

| Port  | Directory                                  | Stack                 |
|-------|--------------------------------------------|-----------------------|
| 7000  | `gateway/`                                 | Flask, auth/proxy     |
| 7050  | `world-state-dashboard/`                   | Flask                 |
| 7052  | `climate-dashboard/`                       | Flask                 |
| 7060  | `centralbank-dashboard/`                   | Flask                 |
| 5050  | `polymarket_weather_dashboard/`            | Flask + PWA           |
| 8000  | `crypto-dashboard/`                        | Flask + ML            |
| 8050  | `stock-dashboard/`                         | Flask                 |
| 8051  | `midterm-dashboard/`                       | FastAPI + React       |
| 8052  | `top-traders-dashboard/`                   | Flask                 |
| 8888  | `sports-dashboard/`                        | Flask                 |
| 18789 | `Dashboard-x-truth-research-prediction/`   | Flask                 |
| —     | `polymarket_weather_bot/`, `polymarket-bot/` | Headless bots       |

Each dashboard has its own `README.md`, `.env.example`, and `requirements.txt`.
`midterm-dashboard` is the only one with a JS build (React frontend);
everything else serves static HTML — **do not add JS build tooling** to the
others.

## Running things

```bash
# Whole stack
docker compose up --build

# One service (Docker)
docker compose up --build crypto

# One service (manual)
cd crypto-dashboard && python3 server.py

# All services (manual, with PID files + /tmp logs)
./start_dashboards.sh start    # also: stop | restart | status
```

Manual logs: `/tmp/dashboard_*.log`. Docker logs: `docker compose logs -f <svc>`.

## Lint / test

```bash
ruff check gateway/ crypto-dashboard/ stock-dashboard/   # etc.
```

Ruff is configured to flag **F821 only** (undefined names) with a 200-char
line limit. Don't reformat for style — the loose config is intentional.
There is no project-wide test runner; each dashboard has (or lacks) its own
ad-hoc tests. Verify changes by running the affected service and hitting it.

## Conventions that bite

- **`.gitignore` `lib/` rule has a deliberate unignore** for
  `midterm-dashboard/frontend/src/lib/`. If you add a `src/lib/` to another
  frontend, add it to the unignore list or it will be silently swallowed.
- **Secrets**: `.env`, `*.db`, `*.key`, `*.pem`, `credentials*` are gitignored.
  Never commit any of these. New env keys go in both the service's
  `.env.example` and the root `.env.example`.
- **Auth DBs (`auth.db`)** contain live PBKDF2 hashes + session tokens.
  They live on the server, not in the repo. Don't delete or check in.
- **`workdir/`** is scratch — mostly stale copies of crypto-dashboard files.
  Don't edit code there expecting it to take effect.
- **`polymarket-bot/`** is tightly coupled to `crypto-dashboard/` — changes
  to one can break the other.
- **Redis** requires `REDIS_PASSWORD` env var; docker-compose fails fast
  without it.

## Deploy

```bash
./snapshot.sh save all "before-XYZ"   # always before deploy
./deploy.sh                            # all sites
./deploy.sh gateway                    # one site
```

Needs `DEPLOY_SERVER` env var (e.g. `user@host`). Deploy is rsync to
`~/Polymarket` on the Ubuntu box; systemd units in `deploy/` run the services.

## Branch + PR flow

- Feature branch off `main`, PR back to `main`.
- CI runs ruff + a Docker build check.
- Don't push directly to `main`.

## What Claude should default to

- **Plan first** for anything touching multiple dashboards, the gateway, or
  deploy/auth.
- **Run the service** after a change — type-check + ruff don't catch
  template/route/JSON-shape bugs, which are the common failure mode here.
- **Snapshot before destructive ops** on local DBs (`./snapshot.sh save`).
- **Ask before** editing `gateway/` auth code, `deploy.sh`, or anything
  touching live auth DBs / secrets.
- **Stay in scope** — this monorepo invites drive-by "while I'm here" edits
  across dashboards. Don't. One dashboard per change unless asked.
