# app/ — Truth Research application package

The Python package powering the dashboard. Top-level files are the FastAPI
app, the SQLModel/SQLAlchemy data layer, and the APScheduler-driven pipeline
that wires the rest together.

```
app/
├── main.py           # FastAPI app — auth, routes, dashboard renderer
├── db.py             # Async SQLAlchemy engine, session factory, pragmas
├── models.py         # SQLModel tables and enums
├── scheduler.py      # APScheduler pipeline that runs scrapers → extractor → ranker → resolver
├── config.py         # Loads .env into a settings dict + parses config.yaml
├── config.yaml       # Tunables (keywords, credibility weights, scoring thresholds)
├── credibility/      # Source-credibility scoring engine
├── markets/          # Polymarket + Kalshi market clients
├── processing/       # Extractor (LLM/regex), ranker (EV/risk), resolver (mark predictions correct)
├── scrapers/         # X / TruthSocial scrapers
├── desktop/          # PyInstaller-bundled macOS desktop wrapper (rumps menu bar + pywebview)
├── templates/        # Jinja2 templates served by main.py
└── tests/            # pytest suite
```

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Marks this directory as a Python package. |
| `main.py` | FastAPI app — Fernet-encrypted cookie auth, dashboard route, refresh endpoint, login/logout, password reset, profile. Imports `models`, `db`, `scheduler`. |
| `db.py` | `create_async_engine` + `AsyncSession` factory. SQLite pragmas (`journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=30000`). |
| `models.py` | SQLModel tables: `RawPost`, `Source`, `Prediction`, `MarketSnapshot`, `ResolvedMarket`, `SourcePredictionRecord`, `CredibilitySnapshot`, plus `PlatformEnum` / `CategoryEnum` / etc. |
| `scheduler.py` | APScheduler `AsyncIOScheduler` running `run_pipeline()` on an interval. Pipeline: fetch raw posts → extract predictions → sync markets → resolve closed markets → recompute source credibility. |
| `config.py` | Loads `.env` via python-dotenv into a `settings` dict, parses `config.yaml` into `yaml_config`. |
| `config.yaml` | Tunables: scraping keywords (per category), credibility weights, scoring thresholds, market match threshold, decay half-life, etc. **Edit this** rather than hard-coding values. |

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `credibility/` | Source-credibility scoring engine | `credibility/README.md` |
| `markets/` | Polymarket + Kalshi clients | `markets/README.md` |
| `processing/` | Extractor / ranker / resolver | `processing/README.md` |
| `scrapers/` | X + TruthSocial scrapers | `scrapers/README.md` |
| `desktop/` | macOS desktop app wrapper | `desktop/README.md` |
| `templates/` | Jinja2 HTML templates | `templates/README.md` |
| `tests/` | Pytest suite | `tests/README.md` |
