# World State Dashboard — Geopolitical Feed

Real-time geopolitical dashboard. Renders undersea cables, oil/gas pipelines,
and rare-earth fields on a MapLibre map alongside news and (optionally) X
posts.

Port: **7050**. Lives behind the gateway at `world.narve.ai` in production.

## Run locally

```bash
cd world-state-dashboard
cp .env.example .env       # X_BEARER_TOKEN optional
pip install -r requirements.txt
python3 server.py
# http://localhost:7050
```

Or via Docker from the repo root:

```bash
docker compose up --build world
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `server.py` | FastAPI app — RSS news polling (defusedxml), X feed (optional), infrastructure map endpoints, gateway SSO middleware. |
| `infrastructure_data.py` | Hand-curated coordinates for undersea cables, oil/gas pipelines, oil/rare-earth fields. Imported by `server.py`. |

**Frontend**
| File | Purpose |
|---|---|
| `index.html` | Map + feed UI served at `/`. References the vendored static assets in `static/`. |
| `static/` | Vendored JS/CSS — Chart.js and MapLibre GL (see `static/README.md`). |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `world` service. |
| `.dockerignore` | Excludes logs and `__pycache__` from the Docker build context. |
| `requirements.txt` | Python deps. Includes `defusedxml` (required). |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth for local dev. |
| `X_BEARER_TOKEN` | empty | X (Twitter) API v2 bearer token. Leave blank to disable the X feed. |

## Notes

- All XML parsing goes through `defusedxml` to avoid XXE — required dependency.
- When neither `GATEWAY_SSO_SECRET` nor `DEV_MODE=1` is set, the server rejects every request.
