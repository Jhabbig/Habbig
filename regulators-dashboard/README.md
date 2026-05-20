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
| **v0.2** | **Severity score** — enforcement-tagged items get a fine amount extracted via context-anchored regex (USD / GBP / EUR), bucketed `low (<$1M)` / `medium ($1M–10M)` / `high ($10M–100M)` / `severe ($100M+)`. Native amount and ≈USD shown on hover; severity-filter chips. Largest amount wins when multiple are mentioned. | rules — `analysis/severity.py` |
| **v0.3** | **Activity heatmap** — per-regulator strip of stacked weekly bars across the last 12 weeks, segments colored by type tag. Shared Y scale so SEC vs FCA vs ESMA volumes are visually comparable. Per-bar hover shows tag breakdown + weekly total. Inline SVG, no JS deps. | aggregation — `analysis/heatmap.py` |
| **v0.4** | **Topic clusters** — every item tagged with zero or more topics from `crypto / etf / aml / disclosure / marketstructure / privatefunds / cyber / climate`. Multi-topic honest (a crypto-AML enforcement fires both). Dynamic topic-filter chip row with per-topic count badges sourced from `/api/topics`; matched phrases shown on hover; topic pills inline under each headline. | rules — `analysis/topic_keywords.py` |
| **v0.5** | **Polymarket / Kalshi overlay** — every action gets matched against the active Polymarket Gamma + Kalshi public market lists via anchor-token-weighted Jaccard; top-3 matches surface as small `Poly 14¢ yes ↗` / `Kalshi 87¢ yes ↗` deep-link buttons under the headline. Hover shows the full market question + shared anchor tokens + score. Read-only on trades — clicks open the venue in a new tab. New `Has market match` filter chip. | Polymarket Gamma API + Kalshi public `/trade-api/v2/markets` |
| **v0.6** | **Personnel watch** — hand-curated roster of chairs / commissioners / CEOs with `term_end`, `term_type`, and a `source_url` pointing to the official roster page. `days_until` computed live, sorted imminent-first, badge-coded (`imminent` ≤ 7d / `soon` ≤ 90d / `later`). Same v0.5 market matcher attaches Polymarket / Kalshi markets per row (e.g. "Will Powell be Fed Chair on Dec 31, 2026?" against the Powell entry). | hand-curated — `data/personnel.py` |

All views graceful-degrade when their data source is unreachable (the
per-source status row flips to red; other sources keep working).

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/feed?days=90&jurisdiction=&source=&tag=&severity=&topic=&has_market=&q=` | 30 min (feed) + 5 min (markets) | Unified action feed with filters + matched markets |
| `GET /api/heatmap?weeks=12` | 30 min (via feed cache) | Per-regulator × per-week × per-tag counts (`weeks` clamped 4..52) |
| `GET /api/topics?days=90` | 30 min (via feed cache) | Per-topic counts across the window — drives the topic-filter chip badges |
| `GET /api/markets` | 5 min | Raw normalized Polymarket + Kalshi market list (debug-friendly) |
| `GET /api/people` | — (data is a Python literal; markets cached) | Personnel watch with `days_until` + matched markets |
| `GET /healthz` | — | Liveness probe |

Filter semantics:
  - `days` — clamp 1..365, default 90
  - `jurisdiction` — comma-separated codes (`US,UK,EU`), case-insensitive
  - `source` — comma-separated source codes (`SEC,FCA,ESMA`), case-insensitive
  - `tag` — comma-separated category tags (`enforcement,rulemaking,guidance,speech,personnel,other`), matches `primary_tag` or any element of `tags`. The literal `other` matches items where the classifier scored zero.
  - `severity` — comma-separated severity buckets (`low,medium,high,severe,none`). `none` matches enforcement items where no amount was extracted, and every non-enforcement item.
  - `topic` — comma-separated topic keys (`crypto,etf,aml,disclosure,marketstructure,privatefunds,cyber,climate`). Match is "any-of" — an item with `topics=[crypto, aml]` matches `topic=crypto` or `topic=aml`.
  - `has_market` — `true` to keep only items with at least one Polymarket / Kalshi match.
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
python3 -m ingestion.unified_feed     # merged feed + per-source status + classifier tags + severity
python3 -m analysis.classifier        # 11 fixture headlines across all 6 categories
python3 -m analysis.severity          # 13 fixture amounts (incl. multi-amount + false-positive guard)
python3 -m analysis.heatmap           # aggregation smoke against synthetic items
python3 -m analysis.topics            # 8 fixture headlines, multi-topic & negative cases
python3 -m ingestion.polymarket_client  # Live Gamma API fetch, normalized
python3 -m ingestion.kalshi_client    # Live Kalshi /trade-api/v2/markets fetch, normalized
python3 -m analysis.market_match      # 4-item × 4-market join fixtures (incl. Lakers false-positive guard)
python3 -m analysis.people            # Roster dump with days_until + sort order
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
│   ├── polymarket_client.py        Polymarket Gamma API → normalized binary markets (5-min cache)
│   ├── kalshi_client.py            Kalshi /trade-api/v2/markets → normalized markets (5-min cache)
│   └── unified_feed.py             Per-source try/except + 30-min cache + classifier hook
├── analysis/
│   ├── classifier_keywords.py      Six-category phrase dictionary (tunable)
│   ├── classifier.py               Rule-based scorer + 11 fixture self-test
│   ├── severity.py                 Fine-amount extractor (USD/GBP/EUR) + bucketing + 13 fixtures
│   ├── heatmap.py                  ISO-week × regulator × tag aggregation
│   ├── topic_keywords.py           Eight-topic phrase dictionary (tunable)
│   ├── topics.py                   Multi-topic extractor + 8 fixture self-test
│   ├── market_match.py             Anchor-weighted Jaccard joiner — items × markets, 4 fixtures
│   └── people.py                   Personnel roster loader + days-until + synthetic-item for matcher
├── data/
│   └── personnel.py                Hand-curated roster (EDIT HERE to add chairs/commissioners)
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

### v0.2 — severity scoring

`analysis/severity.py` runs only on items tagged `enforcement`. It:

1. Finds every context-word occurrence (`fine` / `penalty` / `settle` /
   `pay` / `disgorge` / `restitution`).
2. Scans an 80-character window on either side for a monetary amount —
   currency symbol (`$£€`) or ISO code (`USD/GBP/EUR`) plus a number
   plus an optional magnitude word (`million`, `billion`, `m`, `bn`,
   `k`, etc.).
3. Converts to USD-equivalent via a fixed FX table (USD=1.00, GBP≈1.25,
   EUR≈1.10). Buckets are 10× apart so 20% FX moves don't shift a
   bucket — refresh the constants annually if it matters.
4. Returns the largest amount across all valid (context, amount) pairs.
   "Pay $5,000 in restitution and $200 million in penalties" → severe.

The context-word anchor is the false-positive guard: "Quarterly profits
hit $10 billion at JPMorgan" has no enforcement context and returns
None. Belt-and-braces: `classify_item` skips severity entirely unless
`primary_tag == "enforcement"`, so a passing "$5M revenue" inside a
rulemaking doc can't leak through.

### v0.3 — activity heatmap

`analysis/heatmap.py` buckets every item into an ISO week (Monday-start),
groups by source code, and counts by `primary_tag`. The endpoint returns
a rectangular grid (regulator × week × tag, zeros included) so the UI
renders without per-cell null checks. Y axis uses the **global** weekly
max across all regulators, so a "FCA went quiet, SEC ramping" pattern is
visible by eyeballing row densities side-by-side.

The chart is inline SVG with `<title>` tooltips per stacked segment —
no Chart.js, no dependencies. Stacking order matches the canonical tag
order from the classifier (`enforcement` at the bottom of each column
since it's the most editorially impactful).

### v0.4 — topic clusters

`analysis/topic_keywords.py` defines eight orthogonal topics; an action
can match any subset. The scorer is identical in mechanics to the v0.1
type classifier (`Σ weight × count` per topic, fire on score ≥ 1) but
multi-tag rather than winner-takes-all. Matched phrases are exposed in
the API and rendered as a hover tooltip on each topic pill.

The dynamic topic-filter chip row above the action feed is populated
from `/api/topics`, which returns per-topic counts across the cached
window — so a reader sees `Crypto (23)` / `AML (17)` / `ETF (8)` at a
glance and can drill in with one click. Each item row carries small
topic pills below the headline so the relationship between filter and
result is visible.

**Topic vs type:** topics are orthogonal to the type classifier. An item
is "an enforcement action" (type) **about** "crypto + AML" (topics).
Both filters compose — `tag=enforcement&topic=crypto` returns crypto-
related enforcement actions.

### v0.5 — Polymarket / Kalshi overlay

`ingestion/polymarket_client.py` pulls active markets from the public
Gamma API and keeps the binary YES/NO ones. `ingestion/kalshi_client.py`
does the same against Kalshi's public `/trade-api/v2/markets`. Both
normalize to a shared shape (`source`, `question`, `yes_price`,
`no_price`, `end_date`, `url`). 5-min cache, independent from the
30-min RSS cache.

`analysis/market_match.py` tokenizes both sides (item title + summary,
market question), drops stopwords + tokens shorter than 3 chars, then
requires at least one **anchor token** (regulator code, marquee topic
keyword, named exchange) in the overlap before scoring. Score is
**anchor-weighted Jaccard**: anchor tokens count 3× in the numerator,
so `{sec, ftx}` between an item and a market with 14 tokens in the
union scores 0.43 (matches) — plain Jaccard at 2/14 = 0.14 would have
missed. Threshold 0.18, minimum 2 shared tokens, top 3 matches per
item.

**Read-only on trades**, same posture as `centralbank-dashboard` v0.5.
The buttons are `<a target="_blank">` to the venue's own market URL —
users execute orders in the venue UI with their own accounts. Phase 2
(in-app trade execution) is gated behind paying users.

Known imperfection: the matcher will surface markets that are
*topically* related but resolve on a slightly different event ("Will
the SEC charge FTX executives?" surfacing for an item about an SEC
charge against an FTX-adjacent firm). The full market question is
shown on hover; users verify on the venue before acting. Lifting
matcher precision needs richer entity extraction — deferred until
v0.5 has live usage telling us which false-positive shapes matter.

### v0.6 — personnel watch

`data/personnel.py` is the editable source — a Python literal list of
`{regulator, role, name, term_end, term_type, source_url, notes}`
dicts. The file's docstring spells out the date semantics
(`term_end` is the next transition anchor — for chairs that means
the incumbent's underlying commissioner term, since the chair role
itself has no fixed end).

`analysis/people.py` loads the roster, computes `days_until` against
UTC today, and sorts imminent (≤ 365d future) → later future → past →
unknown. It also synthesizes a fake item per person (`title` =
"`{name} — {role} {regulator}`", `summary` = the notes) and runs the
v0.5 matcher to attach Polymarket / Kalshi markets — surnames I rely
on (`powell`, `gensler`, `atkin(s)`, `uyeda`) are already in
`market_match.ANCHOR_TOKENS`. To add a market match for a new person,
also append their surname there.

The roster is intentionally seeded with **four** well-known entries —
Powell, Atkins, Rathi, Ross — each with a verifiable `source_url`.
The file is loud about the data being best-effort and refresh-by-hand
until v1.1 (auto-scraping confirmation calendars).

## Roadmap

| Step | Status | Adds |
|---|---|---|
| **v0**   | ✓ done | Action feed (SEC + FCA + ESMA, RSS) |
| **v0.1** | ✓ done | Auto-classifier — type tag (`enforcement` / `rulemaking` / `speech` / `guidance` / `personnel` / `other`) via keyword rules; matched phrases shown on hover; type-filter chips |
| **v0.2** | ✓ done | Severity score — fine-amount regex (USD/GBP/EUR) + USD-equivalent bucketing (low / medium / high / severe) on enforcement-tagged items; native + USD shown on hover; severity-filter chips |
| **v0.3** | ✓ done | Activity heatmap — per-regulator weekly stacked-bar SVG over the last 12 weeks, segments colored by type tag, shared Y scale across regulators |
| **v0.4** | ✓ done | Topic clusters — multi-topic tagging (crypto/etf/aml/disclosure/marketstructure/privatefunds/cyber/climate); per-topic count chips above the feed; topic pills inline under each headline |
| **v0.5** | ✓ done | Polymarket / Kalshi overlay — anchor-weighted Jaccard match between actions and active markets; per-item deep-link buttons (read-only on trade); `has_market` filter |
| **v0.6** | ✓ done | Personnel watch — hand-curated roster of chairs/commissioners with term-end dates, days-until badges, source links, and per-row market overlay reusing the v0.5 matcher |
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
- **Severity FX is fixed, not live.** Bucket thresholds are 10× apart
  so 20% FX moves don't shift a bucket, but a borderline £80M vs $100M
  case is approximate. Refresh `FX_TO_USD` in `analysis/severity.py`
  annually.
- **Severity is title+summary only.** Long press releases that put the
  fine in paragraph six aren't reached. v0 doesn't fetch body HTML;
  adding that is on the wider roadmap.
- **English context words only.** Translated headlines from BaFin /
  JFSA / FINMA won't match the `fine|penalty|settle|pay|disgorge`
  anchor list. Per-language extensions land alongside those sources.
- **Market matcher trades precision for recall.** Anchor-weighted
  Jaccard catches "SEC + FTX" or "FCA + Bitcoin + ETF" reliably but
  will sometimes surface a market that's topically related on a
  different specific event. Full market question + deep-link is shown
  on hover so users verify on the venue. Tightening precision needs
  per-entity extraction — deferred until usage tells us which
  false-positive shapes matter.
- **Polymarket / Kalshi outage modes are independent.** Each client
  has its own 5-min cache; one going down leaves the other's badges
  surfaced. The `market_sources` field in `/api/feed` exposes
  per-venue status for the UI to render.
- **Personnel roster is hand-curated and drifts.** Four seeded
  entries — extend `data/personnel.py` for full coverage. Dates need
  annual verification against each official roster page; the file
  is loud about this, and every row links to its `source_url`. Auto-
  scraping confirmation calendars is roadmap v1.1.
- **Polymarket overlay coverage is thin** outside crypto ETFs and
  big-name settlements, so v0.5 will only annotate a small fraction of
  action cards. That's expected.
- **No CFTC / FinCEN / OFAC in v0.** They're listed in the v1.0 source
  expansion. v0 keeps to three RSS sources to surface the architecture
  without spending the iteration budget on per-regulator feed quirks.
