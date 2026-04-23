# narve.ai

Prediction market intelligence platform. Credibility-scored signals from
social-media sources cross-referenced against live Polymarket and Kalshi
markets, with a paid-subscriber layer for per-market community analysis
("Takes"), user predictions, intelligence chat, and portfolio sync.

## Status

Private, invite-only. Active development on `feature/platform-build`.
Production at https://narve.ai. See [RUNBOOK.md](RUNBOOK.md) for deploy
topology.

## Stack

- **FastAPI** (Python 3.11+) on `uvicorn`, single-process per environment.
- **SQLite** (WAL mode, FTS5, JSON1) — one DB file per environment:
  `auth.db` (prod), `auth-staging.db` (staging).
- **Cloudflare Tunnel** terminates TLS and proxies to `127.0.0.1:7000`.
- **Tailscale** SSH for admin access to the Ubuntu host.
- **Anthropic Claude** for intelligence chat, prediction extraction,
  retrospective scoring.
- **Inter** (self-hosted woff2 subset) + a strict monochrome palette —
  see [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md).
- **ARQ** (with an in-process fallback) for background jobs.

## Quickstart (local)

```bash
git clone https://github.com/Jhabbig/Habbig.git
cd Habbig
python3 -m venv venv && source venv/bin/activate
pip install -r gateway/requirements.txt
cp gateway/.env.example gateway/.env   # edit as needed
cd gateway && python3 -m uvicorn server:app --reload --port 7000
```

Visit http://localhost:7000/token to start the auth flow with a dev invite
token (emitted once to stdout on first run when no tokens exist).

## Structure

| Path | What it is |
| --- | --- |
| `gateway/` | Main FastAPI app. Everything runs from here. |
| `gateway/server.py` | App factory + top-level routes + middleware. |
| `gateway/*_routes.py` | Feature route modules (affiliate, embed, takes, etc.). |
| `gateway/db.py` | Low-level SQLite helpers; `db.conn()` context. |
| `gateway/db_takes.py`, `db_collections.py`, … | Per-feature DB layers. |
| `gateway/queries/` | Read-only query modules shared across routes. |
| `gateway/ai/` | Claude integrations + cached prompt helpers. |
| `gateway/jobs/` | Background jobs + cron registration. |
| `gateway/scraper/` | Social-media ingestion (Twitter, Metaculus, Substack, TruthSocial). Standalone worker. |
| `gateway/intelligence/` | Prediction extraction, retrospective, backtester. |
| `gateway/credibility/` | Source credibility scoring engine. |
| `gateway/realtime/` | WebSocket + SSE fan-out. |
| `gateway/portfolio/` | Polymarket + Kalshi position sync. |
| `gateway/forensics/` | Per-response watermarking + leak-attribution. |
| `gateway/static/` | HTML templates, CSS, JS, fonts. |
| `gateway/migrations/` | Numbered `NNN_slug.py` migrations. 130 applied. |
| `gateway/tests/` | 100+ pytest test files. |
| `extension/` | Chrome extension (separate build step). |
| `scripts/` | Operator CLIs (benchmarking, backfill, coverage). |

## Deploy

See [RUNBOOK.md](RUNBOOK.md).

## Design

See [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md). In-depth posture: [gateway/NARVE_SECURITY_AUDIT.md](gateway/NARVE_SECURITY_AUDIT.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
