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
| **v0**   | **Action feed** — unified table of last 90 days across SEC / FCA / ESMA: date, jurisdiction badge, body, headline (links to source), summary. Jurisdiction chips and free-text search. Per-source status row. | RSS — `defusedxml`-parsed |
| **v0.1** | **Type classifier** — every item tagged as `enforcement` / `rulemaking` / `guidance` / `speech` / `personnel` / `other` via rule-based keyword matching on title + summary. Color-coded chip per row; matched phrases shown on hover; type-filter chips. Multi-tag honest (an item matching two categories surfaces both). | rules — `analysis/classifier_keywords.py` |

All views graceful-degrade when their data source is unreachable (the
per-source status row flips to red; other sources keep working).

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/feed?days=90&jurisdiction=&source=&tag=&q=` | 30 min | Unified action feed with filters |
| `GET /healthz` | — | Liveness probe |

Filter semantics:
  - `days` — clamp 1..365, default 90
  - `jurisdiction` — comma-separated codes (`US,UK,EU`), case-insensitive
  - `source` — comma-separated source codes (`SEC,FCA,ESMA`), case-insensitive
  - `tag` — comma-separated category tags (`enforcement,rulemaking,guidance,speech,personnel,other`), matches `primary_tag` or any element of `tags`. The literal `other` matches items where the classifier scored zero.
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
python3 -m ingestion.unified_feed     # merged feed + per-source status + classifier tags
python3 -m analysis.classifier        # 11 fixture headlines across all 6 categories
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
│   └── unified_feed.py             Per-source try/except + 30-min cache + classifier hook
├── analysis/
│   ├── classifier_keywords.py      Six-category phrase dictionary (tunable)
│   └── classifier.py               Rule-based scorer + 11 fixture self-test
├── index.html                      Single-file UI: filter chips + tag chips + action table, no JS deps
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

### v0.1 — type classifier

`analysis/classifier.py` runs after the merge step. For each item it
computes `score(category) = Σ (weight × count)` over the phrase dictionary
in `classifier_keywords.py` (six categories), then attaches:

  - `primary_tag` — single highest-scoring category, or `"other"` if none
  - `tags` — every category scoring `> 0`, sorted desc by score
  - `matched_phrases` — `{category: [phrase, …]}` showing the top-5 matches
    per category that fired, surfaced as a hover tooltip on the chip in the
    UI so a reader can sanity-check why an item got tagged what it did

Multi-tag is honest: a "Wells notice followed by settlement" headline
scores high on `enforcement`; a "speech announcing proposed rule" scores
on both `speech` and `rulemaking` — both surface, with `primary_tag`
picking the highest scorer.

**Tuning is one-file**: edit `classifier_keywords.py` and re-run
`python3 -m analysis.classifier` to see the fixture pass-rate. Avoid
single-word phrases that collide with English stop-words ("its", "names")
— the file's docstring spells out the gotchas.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| **v0**   | ✓ done | Action feed (SEC + FCA + ESMA, RSS) |
| **v0.1** | ✓ done | Auto-classifier — type tag (`enforcement` / `rulemaking` / `speech` / `guidance` / `personnel` / `other`) via keyword rules; matched phrases shown on hover; type-filter chips |
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
- **Classifier is rule-based and Anglophone.** It matches headlines as
  written by SEC / FCA / ESMA in English. Translated headlines from
  jurisdictions added later (BaFin, JFSA, FINMA) will need their own
  phrase dictionaries — the architecture supports this; the dictionary
  doesn't yet.
- **Severity scoring lands in v0.2.** Until then, "$200M settlement" and
  "$5,000 fine" both render as `enforcement` with no magnitude signal.
- **Polymarket overlay coverage is thin** outside crypto ETFs and
  big-name settlements, so v0.5 will only annotate a small fraction of
  action cards. That's expected.
- **No CFTC / FinCEN / OFAC in v0.** They're listed in the v1.0 source
  expansion. v0 keeps to three RSS sources to surface the architecture
  without spending the iteration budget on per-regulator feed quirks.
