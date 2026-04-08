# Contributing

## Prerequisites

- Python 3.12+
- Docker & Docker Compose (recommended)
- Git

## Quick start (Docker)

The fastest way to get everything running:

```bash
# 1. Clone the repo
git clone <repo-url> && cd Polymarket

# 2. Copy env files (fill in your keys — see .env.example at the root for guidance)
cp gateway/.env.example gateway/.env
cp crypto-dashboard/.env.example crypto-dashboard/.env
cp polymarket_weather_bot/.env.example polymarket_weather_bot/.env

# 3. Start everything
docker compose up --build
```

Gateway runs at `http://localhost:7000`. All dashboards are accessible through it.

## Quick start (manual / no Docker)

```bash
# 1. Create a virtualenv
python3 -m venv venv && source venv/bin/activate

# 2. Install all dependencies
pip install -r gateway/requirements.txt
pip install -r crypto-dashboard/requirements.txt
pip install -r stock-dashboard/requirements.txt
pip install -r midterm-dashboard/backend/requirements.txt
pip install -r top-traders-dashboard/requirements.txt
pip install -r polymarket_weather_dashboard/requirements.txt
pip install -r sports-dashboard/requirements.txt
pip install -r world-state-dashboard/requirements.txt

# 3. Copy env files
cp gateway/.env.example gateway/.env
cp crypto-dashboard/.env.example crypto-dashboard/.env

# 4. Start all services
./start_dashboards.sh
```

## Project layout

| Directory | Port | Description |
|-----------|------|-------------|
| `gateway/` | 7000 | Central auth + reverse proxy |
| `crypto-dashboard/` | 8000 | BTC/crypto signals + ML |
| `stock-dashboard/` | 8050 | Stock market dashboard |
| `midterm-dashboard/` | 8051 | Election predictions |
| `top-traders-dashboard/` | 8052 | Whale tracking |
| `polymarket_weather_dashboard/` | 5050 | Weather bot UI |
| `sports-dashboard/` | 8888 | Sports arbitrage |
| `world-state-dashboard/` | 7050 | Geopolitical feed |

## Branch workflow

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/my-change
   ```
2. Make your changes and test locally.
3. Push and open a PR against `main`. CI will run linting and a Docker build check.
4. Get a review before merging.

## Working on a single dashboard

You don't need to run everything. To work on just one dashboard:

```bash
# Docker — only build and run what you need
docker compose up --build crypto

# Manual — run one service directly
cd crypto-dashboard && python3 server.py
```

The gateway is only needed if you want auth/subscription gating. Individual dashboards work standalone on their own ports.

## Environment variables

See the root `.env.example` for a complete list of all keys across every service. Each service directory also has its own `.env.example` with only the keys it needs.

## Code style

- Python: we use `ruff` for linting (runs in CI). Check locally with:
  ```bash
  pip install ruff
  ruff check gateway/ crypto-dashboard/ stock-dashboard/
  ```
- No frontend framework — dashboards serve static HTML. Don't add JS build tooling.

## Logs

- Manual mode: logs at `/tmp/dashboard_*.log`
- Docker mode: `docker compose logs -f <service>`
