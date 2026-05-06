# Regulators Dashboard

Tracks the movement of financial regulators around the world — enforcement
actions, rule proposals, speeches, personnel changes — sortable by
jurisdiction and searchable across titles + summaries. Differentiates from
`centralbank-dashboard` (monetary policy) and `world-state-dashboard`
(physical geopolitics) by being **regulatory-action time-series**.

Port: **7080**.

## What's built

| Version | View | Data source |
|---|---|---|
| **v0** | **Action feed** — unified table of last 90 days across SEC / FCA / ESMA: date, jurisdiction badge, body, headline (links to source), summary. Jurisdiction chips and free-text search. Per-source status row. | RSS — `defusedxml`-parsed |

All views graceful-degrade when their data source is unreachable (the
per-source status row flips to red; other sources keep working).

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/feed?days=90&jurisdiction=&source=&q=` | 30 min | Unified action feed with filters |
| `GET /healthz` | — | Liveness probe |

Filter semantics:
  - `days` — clamp 1..365, default 90
  - `jurisdiction` — comma-separated codes (`US,UK,EU`), case-insensitive
  - `source` — comma-separated source codes (`SEC,FCA,ESMA`), case-insensitive
  - `q` — case-insensitive substring match on title or summary

## Run locally

```bash
cd regulators-dashboard
cp .env.example .env       # DEV_MODE=1 lets you skip gateway auth
pip install -r requirements.txt
python3 server.py
# http://localhost:7080
```

Or via Docker from the repo root:

```bash
docker compose up --build regulators
```

Smoke-test individual modules:

```bash
python3 -m ingestion.sec_rss          # SEC press releases
python3 -m ingestion.fca_rss          # FCA news
python3 -m ingestion.esma_rss         # ESMA news
python3 -m ingestion.unified_feed     # merged feed + per-source status
```

## Files

```
regulators-dashboard/
├── server.py                       FastAPI + gateway-SSO middleware + 2 routes
├── ingestion/
│   ├── _rss.py                     Shared RSS/Atom fetcher + parser (defusedxml)
│   ├── sec_rss.py                  SEC press-release feed (US)
│   ├── fca_rss.py                  FCA news feed (UK)
│   ├── esma_rss.py                 ESMA news feed (EU)
│   └── unified_feed.py             Per-source try/except + 30-min cache
├── index.html                      Single-file UI: filter chips + action table, no JS deps
├── Dockerfile                      Python 3.12-slim, non-root, port 7080
├── requirements.txt                fastapi, uvicorn, defusedxml
├── .env.example
└── README.md                       (this file)
```

## How each piece works

### v0 — unified action feed

Each source module declares an `RssSource` (code, name, jurisdiction,
URL) and delegates to `_rss.fetch_source()`. The shared parser handles both
RSS 2.0 and Atom (Atom uses `<entry>` and href-attribute links, RSS uses
`<item>` and text-node links — both shapes covered). Output is normalized
to:

```json
{
  "id": "SEC::https://www.sec.gov/news/...",
  "source": "SEC",
  "source_name": "U.S. Securities and Exchange Commission",
  "jurisdiction": "US",
  "title": "...",
  "link": "https://...",
  "summary": "...",
  "published": "2026-05-01T13:00:00+00:00",
  "tags": []
}
```

`unified_feed.get_cached()` calls every source, tags failures rather than
short-circuiting, sorts by `published` desc, and caches 30 min. The
per-source status (with `ok` flag and error string) is exposed in the API
response so the UI can render which feeds are healthy.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| **v0**   | ✓ done | Action feed (SEC + FCA + ESMA, RSS) |
| v0.1 | open  | Auto-classifier — type tag (`enforcement` / `rulemaking` / `speech` / `guidance` / `personnel`) via keyword rules; matched phrases shown inline. Same transparent rule-based pattern as `centralbank-dashboard/analysis/stance_keywords.py`. |
| v0.2 | open  | Severity score — fine-amount regex + bucketing (<$1M, $1M–10M, $10M–100M, $100M+) for items tagged `enforcement` |
| v0.3 | open  | Jurisdiction heatmap — per-week bar chart of action counts per regulator, stacked by type tag |
| v0.4 | open  | Topic clusters — keyword index (`crypto`, `etf`, `aml`, `disclosure`, `marketstructure`, `privatefunds`, `cyber`, `climate`); drill-down per topic |
| v0.5 | open  | Polymarket / Kalshi overlay — match actions to active markets ("SEC approves X ETF", "Binance settles with DOJ") with same `Trade Poly →` / `Trade Kalshi →` deep-links as `centralbank-dashboard` v0.5 |
| v0.6 | open  | Personnel tracker — chairs, commissioners, term-end dates, succession watch (hand-curated YAML) |
| v0.7 | open  | Speech stance ladder — per-regulator hawkish/dovish-style scoring (SEC `pro-enforcement ↔ light-touch`, FCA `pro-innovation ↔ consumer-first`, ESMA `prescriptive ↔ principles-based`) |
| **v1.0** | open  | All of v0–v0.7 polished + extended source list (CFTC, FinCEN, OFAC, BaFin, FINMA, MAS, HKMA, JFSA, ASIC) |
| v1.1 | open  | Auto-scrape Senate Banking / House FS confirmation calendars to refresh personnel table |
| v1.2 | open  | Statement diff viewer (compare two SEC speeches side-by-side) |
| v1.3 | open  | Court-filing tracker (PACER scraper for SEC litigation releases) |
| v1.4 | open  | OFAC SDN delta-per-day UI |
| v1.5 | open  | Email/RSS alert digest — daily summary keyed on user's filter set |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7080` | Override listen port. |

## Caveats / known limits

- **RSS coverage is uneven across regulators.** SEC / FCA / ESMA have
  reliable feeds and are in v0. JFSA / FINMA need HTML scraping (no public
  RSS) and are deferred to v1.0 — those modules will use `_rss.py`-shaped
  scrapers with the same graceful-degradation fallback.
- **ESMA's RSS path has changed before.** If their feed stops parsing,
  v0 still loads with SEC + FCA showing data; confirm the new URL on
  https://www.esma.europa.eu/news-publications and update
  `ingestion/esma_rss.py`.
- **No type/severity tags in v0.** Every item is rendered raw; classifier
  lands in v0.1. Until then, "Wells notice" and "annual report
  publication" are visually equivalent — sort by date and read.
- **Polymarket overlay coverage is thin** outside crypto ETFs and
  big-name settlements, so v0.5 will only annotate a small fraction of
  action cards. That's expected.
- **No CFTC / FinCEN / OFAC in v0.** They're listed in the v1.0 source
  expansion. v0 keeps to three RSS sources to surface the architecture
  without spending the iteration budget on per-regulator feed quirks.
