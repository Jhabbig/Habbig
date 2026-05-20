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
| `server.py` | FastAPI app — RSS news polling (defusedxml), X feed (optional), infrastructure map endpoints, gateway SSO middleware, Analyst Mode endpoints incl. natural-language query. |
| `infrastructure_data.py` | Hand-curated coordinates for undersea cables, oil/gas pipelines, oil/rare-earth fields. Imported by `server.py`. |
| `analyst_db.py` | SQLite-backed entity/event/source/pinboard store for Analyst Mode. Schema, CRUD, dedupe, timeline buckets, link-analysis subgraph, baseline gazetteer. |
| `event_extractor.py` | Event extractor — heuristic backend by default; routes to `llm_extractor` when `WORLD_STATE_LLM_EXTRACT=1` and `ANTHROPIC_API_KEY` are set. Heuristic still runs as a fallback for items the LLM didn't cover. |
| `llm_extractor.py` | Claude-based event extractor — uses the Anthropic SDK with structured outputs and prompt caching on the gazetteer/ontology system prompt. Auto-upserts new entities the model proposes. |
| `nlq_translator.py` | Natural-language query → structured event filters. Uses Claude structured outputs when enabled; falls back to a regex-based heuristic that handles time windows (`last 48h`), event types, gazetteer aliases, and named regions (Middle East, Red Sea, …). |

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

## Analyst Mode

Entity-centric event view layered on top of the existing dashboard. Data flows
from `fetch_news()` → `event_extractor.extract_batch()` → `analyst_db` so every
RSS poll opportunistically promotes typed events into the store.

UI additions (`index.html`):

- **Timeline strip** (fixed bottom) — stacked event-density chart over the active window (1h/6h/24h/7d/30d).
  - **Scrubbing → map-state replay** — click/drag the timeline to move a playhead cursor; map pins for events after the cursor dim. A `LIVE` / `REPLAY @ <ts>` badge in the timeline header indicates mode (click to return to live).
- **Filter rail** (right edge) — natural-language query input at the top, event-type chips, actor search, saved pinboards.
  - **Ask** — type a question in English (`russian missile strikes on ukraine last 48h`, `earthquakes in middle east`, `sanctions on iran`); the server translates it into structured filters and shows the interpretation below the input.
- **Map pins** — severity-colored markers (sev-4 pulses) for each event with geo.
- **Dossier drawer** — slides in on actor / event click; lists recent events, sources with provenance, and co-occurring entities (link analysis).
- **Pinboards** — save current filters as a named view, restore in one click.

API surface (`/api/analyst/*`):

| Path | Notes |
|---|---|
| `GET  /events`            | `since`, `until`, `types=Strike,Statement,...`, `actor`, `bbox=W,S,E,N`, `limit`. |
| `GET  /event/{id}`        | Full event with hydrated actors + sources. |
| `GET  /entity/{id}`       | Dossier: entity, recent events, 1-hop co-occurrence graph. |
| `GET  /entities?q=`       | Alias / name search. |
| `GET  /timeline`          | Bucketed counts by type for stacked-bar timeline. |
| `GET  /graph/{id}`        | Pure subgraph (no events). |
| `GET/POST/DELETE /pinboards` | Saved views (filters + window). |
| `POST /query`             | `{q: "natural language question"}` → `{filters, interpretation, events, llm_enabled, count}`. |
| `GET  /stats`             | Row counts. |

Storage: `analyst.db` (SQLite, gitignored). Re-seeded with a baseline gazetteer
of ~30 states/orgs on first run.

### LLM extraction (optional)

The heuristic extractor runs out-of-the-box (no API key). For richer extraction
— typed actors with roles, automatic gazetteer expansion, better date /
geo parsing — set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export WORLD_STATE_LLM_EXTRACT=1
# optional model overrides (defaults shown):
# export WORLD_STATE_EXTRACTOR_MODEL=claude-opus-4-7
# export WORLD_STATE_NLQ_MODEL=claude-opus-4-7
```

Failures fall back to the heuristic transparently. The system prompt is
prompt-cached so per-request cost is dominated by the small per-batch user
message. To run cheaper, set `WORLD_STATE_EXTRACTOR_MODEL=claude-haiku-4-5`.
