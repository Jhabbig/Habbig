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
| **v0.7** | **Speech stance ladder** — per-regulator scoring on a body-specific axis: **SEC** `pro-enforcement ↔ light-touch`, **FCA** `pro-innovation ↔ consumer-first`, **ESMA** `prescriptive ↔ principles-based`. Picks the most-recent `speech`-tagged item per regulator from the feed, scores title + summary against the axis dictionary, renders bucketed badge + marker on a tri-color axis + matched phrase chips. Closes out v1.0. | rules — `analysis/stance_keywords.py` |

All views graceful-degrade when their data source is unreachable (the
per-source status row flips to red; other sources keep working).

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/feed?days=90&jurisdiction=&source=&tag=&severity=&topic=&has_market=&q=` | 30 min (feed) + 5 min (markets) | Unified action feed with filters + matched markets |
| `GET /api/heatmap?weeks=12&show_empty=false&group_by=source` | 30 min (via feed cache) | Per-{source|jurisdiction} × per-week × per-tag counts. `group_by=jurisdiction` collapses sources by ISO code. `weeks` clamped 4..52. `show_empty=true` returns rows with zero activity. |
| `GET /api/parliament_hearings` | 1 h | v2.2 — UK Treasury + PAC + EU ECON + JURI committees, filtered to financial-regulator-relevant items |
| `GET /api/bills` | 1 h | v2.3 — US Congress + UK Parliament + EU legislative procedures, filtered by verb×topic match |
| `GET /api/feed.csv?…` | 30 min (via feed cache) | v2.3 — CSV export of the filtered action feed; same filters as `/api/feed` |
| `GET /api/topics?days=90` | 30 min (via feed cache) | Per-topic counts across the window — drives the topic-filter chip badges |
| `GET /api/markets` | 5 min | Raw normalized Polymarket + Kalshi market list (debug-friendly) |
| `GET /api/people` | — (data is a Python literal; markets cached) | Personnel watch with `days_until` + matched markets |
| `GET /api/stance` | 30 min (via feed cache) | Per-regulator speech-stance ladder — most-recent speech-tagged item scored on the body's axis |
| `GET /api/diff` | 30 min (via feed cache) | Per-regulator latest-vs-prior speech diff (token-level ops + stats) |
| `GET /api/sdn` | 12 h | OFAC SDN today's snapshot meta + delta vs prior snapshot (top-20 added/removed previews + program deltas) |
| `GET /api/hearings` | 1 h | Senate Banking + House FS confirmation hearings (filtered to nomination/confirmation items) |
| `GET /feed.xml?…` | 30 min (via feed cache) | RSS 2.0 alert feed — same filters as `/api/feed`, gated by `RSS_SHARED_TOKEN` |
| `POST /api/subscribe` | — | v1.6 — accept `{email, filter}`, send confirmation email, store pending row |
| `GET /api/subscribe/confirm?token=…` | — | Email-click confirmation; flips pending → confirmed |
| `GET /api/subscribe/unsubscribe?token=…` | — | Email-click unsubscribe; idempotent |
| `POST /api/digest/send_now` | — | Admin-token-gated digest dispatcher; external cron drives the schedule |
| `GET /api/sources` | — | v2.0 — master list of every registered RSS source + jurisdictions covered |
| `GET /api/courts` | 1 h | v2.0 — CJEU + UK judiciary + SCOTUS financial-relevant case feed |
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

Run the full fixture suite (17 self-tests) in one command:

```bash
bash run_tests.sh
```

Or via Docker from the repo root:

```bash
docker compose up --build regulators
```

Smoke-test individual modules:

```bash
python3 -m ingestion.sec_rss          # SEC press releases
python3 -m ingestion.sec_litigation_rss  # SEC litigation releases (v1.3)
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
python3 -m analysis.stance            # 7 stance fixtures across SEC/FCA/ESMA × pos/neg/neutral
python3 -m analysis.diff              # latest-vs-prior diff sanity over 4 synthetic items
python3 -m ingestion.ofac_sdn         # SDN parser + persist + delta fixtures (synthetic 2-day XML)
python3 -m ingestion.confirmation_hearings  # Hearing-filter + regulator-hint fixtures (7 cases)
python3 -m analysis.rss_feed          # RSS 2.0 renderer round-trip via defusedxml parse-back
python3 -m ingestion.digest_subscribers  # Sqlite subscriber-store lifecycle (subscribe/confirm/list/sent/unsub)
python3 -m analysis.email_digest      # Confirmation + daily-digest template renderer fixtures
```

## Files

```
regulators-dashboard/
├── server.py                       FastAPI + gateway-SSO middleware + 2 routes
├── ingestion/
│   ├── _rss.py                     Shared RSS/Atom fetcher + parser (defusedxml)
│   ├── sec_rss.py                  SEC press-release feed (US)
│   ├── sec_litigation_rss.py       SEC litigation-release feed (US) — v1.3
│   ├── fca_rss.py                  FCA news feed (UK)
│   ├── esma_rss.py                 ESMA news feed (EU)
│   ├── polymarket_client.py        Polymarket Gamma API → normalized binary markets (5-min cache)
│   ├── kalshi_client.py            Kalshi /trade-api/v2/markets → normalized markets (5-min cache)
│   ├── ofac_sdn.py                 OFAC SDN XML fetch + parse + per-day snapshot + day-over-day delta
│   ├── confirmation_hearings.py    Senate Banking + House FS hearing-RSS filter (1h cache)
│   ├── sources.py                  v2.0 — master list of every registered RSS source (28 bodies)
│   ├── court_cases.py              v2.0 — CJEU + UK + SCOTUS financial-relevant case feed
│   ├── digest_subscribers.py       v1.6 — SQLite subscriber store (pending/confirmed/unsubscribed)
│   ├── email_send.py               v1.6 — stdlib SMTP sender with DRY_RUN fallback
│   └── unified_feed.py             Per-source try/except + 30-min cache + classifier hook
├── analysis/
│   ├── classifier_keywords.py      Six-category phrase dictionary (tunable)
│   ├── classifier.py               Rule-based scorer + 11 fixture self-test
│   ├── severity.py                 Fine-amount extractor (USD/GBP/EUR) + bucketing + 13 fixtures
│   ├── heatmap.py                  ISO-week × regulator × tag aggregation
│   ├── topic_keywords.py           Eight-topic phrase dictionary (tunable)
│   ├── topics.py                   Multi-topic extractor + 8 fixture self-test
│   ├── market_match.py             Anchor-weighted Jaccard joiner — items × markets, 4 fixtures
│   ├── people.py                   Personnel roster loader + days-until + synthetic-item for matcher
│   ├── stance_keywords.py          Per-regulator stance axes (SEC/FCA/ESMA), tunable
│   ├── stance.py                   Per-regulator stance scorer + 7 fixture self-test
│   ├── diff.py                     Token-level speech diff (latest vs prior per regulator)
│   ├── rss_feed.py                 RSS 2.0 renderer for /feed.xml (zero-dep hand-rolled XML)
│   └── email_digest.py             v1.6 — confirmation + daily-digest HTML/text templates
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

### v0.7 — speech stance ladder

Unlike v0.1 (multi-class type) and v0.4 (multi-tag topics), stance is a
**per-regulator single axis** — each body is contested on a different
pole, so a generic "strict ↔ lax" scorer would muddle the signal.
v0.7's axes:

  - **SEC** `pro-enforcement ↔ light-touch`
  - **FCA** `pro-innovation ↔ consumer-first`
  - **ESMA** `prescriptive ↔ principles-based`

`analysis/stance_keywords.py` defines a `StanceAxis` per regulator with
two phrase dicts (positive-weighted phrases push toward the positive
label, negative-weighted phrases push toward the negative label).
`analysis/stance.py` picks the most-recent `speech`-tagged item per
regulator from the feed, scores `title + summary` against that axis,
and exposes:

  - `bucket` — `<positive_label>` / `<negative_label>` / `NEUTRAL` /
    `NO_SPEECH` (the last when no speech in the feed window)
  - `norm_score` — raw Σ(weight × count) divided by sentence count
  - `matches` — the phrases that fired, with side and weight, sorted by
    absolute contribution; UI shows these as chips

The UI panel renders each regulator as a row: name + axis label on
the left, bucket badge in the middle, and on the right the latest
speech link + a tri-color axis with a marker showing where the score
landed (orange = negative pole, gray = neutral zone, blue = positive
pole; marker x-position is `norm_score` clamped to [-2, +2] mapped to
[0, 100]%).

**To add a new regulator's axis**: append a `StanceAxis(...)` entry to
`AXES` in `stance_keywords.py` keyed by the same source code the RSS
ingestion module uses (`SEC`, `FCA`, `ESMA`, eventually `CFTC`,
`BaFin`, etc.). The scorer iterates `AXES` so nothing else changes.

**Scope note:** v0.7 scores `title + summary` only. RSS summaries are
usually enough — speeches are punchy by design — but if signal turns
out thin in practice, we'd add full-body HTML fetching matching the
`centralbank-dashboard/ingestion/cb_statements._fetch_statement_body`
pattern. Held until v0.7 has live usage telling us whether summaries
suffice.

### v1.2 — speech diff viewer

`analysis/diff.py` picks the two most recent speech-tagged items per
regulator from the feed and computes a token-level diff over their
concatenated `title + summary`. Output is a list of `{op, a, b}` ops
(`equal`, `delete`, `insert`, `replace`) where `a`/`b` are token
sub-lists. The renderer concatenates equal tokens plain, wraps deleted
ones in `<del>` (red, strike-through), inserts in `<ins>` (green).

Each per-regulator block shows:

  - Header: regulator code · added/removed/equal word counts · similarity %
  - Meta: prior date → latest date · external links to both source articles
  - Body: the inline rendered diff

Falls back gracefully when there are fewer than two speeches per
regulator in the feed window — that regulator's block renders "only
one recent speech" or "no recent speeches".

**Same scope caveat as v0.7**: v1.2 diffs RSS-level text only. Body
fetching is the natural lift that would let us diff multi-paragraph
speech text. The signal in summaries-only is "what changed in the
headline + lede" — useful for spotting tone shifts ("from 'enforcement'
to 'robust enforcement'"), less useful for tracking deep prose changes.

### v1.4 — OFAC SDN delta

`ingestion/ofac_sdn.py` fetches the Treasury SDN list daily (`sdn.xml`,
~50MB+ with ~14k entries), streams the XML through `defusedxml.iterparse`
to keep peak memory bounded, and digests each entry to
`{uid, name, type, programs, country}`. Today's digest persists to
`SNAPSHOT_DIR/<YYYY-MM-DD>.json` keyed by OFAC's own `Publish_Date`.

The delta computes `today_uids - yesterday_uids` (additions) and
`yesterday_uids - today_uids` (removals), aggregates added/removed
counts per sanctions program, and exposes everything via `/api/sdn`.
The endpoint returns the top-20 added + top-20 removed entries with
remainder counts — the full 14k-entry list isn't shipped to the client.

**First-snapshot semantics:** if no prior snapshot exists on disk, the
delta returns `first_snapshot=true` with empty arrays. The UI renders
"First snapshot collected — delta available from next publication"
rather than implying zero changes today. Deltas work from day 2 onward.

**Persistence path:** `SDN_SNAPSHOT_DIR` env var (defaults to
`tempfile.gettempdir()/regulators-sdn-snapshots`). For production
Docker, mount a persistent volume there so day-over-day deltas
survive container restart. Last 14 days kept; older snapshots pruned.

**Cache:** 12 h on the fetch path. OFAC publishes weekly on average,
sometimes ad-hoc same-day for breaking sanctions packages.

### v1.1 — confirmation-hearing tracker

`ingestion/confirmation_hearings.py` pulls the Senate Banking Committee
and House Financial Services Committee hearing feeds, reuses the v0
`_rss.py` parser (defusedxml + graceful degradation), filters items
whose title or summary matches `nomination|confirmation|nominee|to be
(chair|commissioner|governor|director|secretary)`, and attaches a
`regulator_hint` when SEC / Fed / CFTC / FDIC / OCC / FinCEN / OFAC /
CFPB / HUD / Treasury are referenced.

This complements `data/personnel.py` rather than replacing it:
personnel covers *confirmed* officials with their term-end anchors;
this module covers the *pending* pipeline.

**Feed URL caveat:** the seeded URLs are best-guess against common
Senate / House RSS conventions. Both chambers reorganize their feed
paths roughly annually. If a source goes red in `/api/hearings`, drop
the current URL (from the committee homepage) into `SOURCES` and
deploy — no other code change required.

### v1.5 — RSS alert feed

`analysis/rss_feed.py` hand-renders a valid RSS 2.0 XML document from
filtered items, with the same filter semantics as `/api/feed`. Each
`<item>` carries the title, link, GUID, pubDate, categories (source +
type tag + topics), and a description that embeds the classifier
metadata (type, severity bucket, topics) so the feed reader shows the
same context the dashboard surface does.

**Auth shape:** RSS readers can't send custom headers, so `/feed.xml`
**bypasses** the gateway-SSO middleware (`x-gateway-secret`) and is
instead gated on `?token=<RSS_SHARED_TOKEN>`. When `RSS_SHARED_TOKEN`
is unset in non-DEV environments the route 503s with a clear "feed
disabled" message so no operator accidentally exposes data publicly.

**Why RSS and not email** in v1.5: email digest needs subscriber
management, unsubscribe tokens, bounce handling, and SMTP plumbing —
a multi-day build. RSS delivers ~80% of the alert value for ~5% of
the build cost, mirroring the "Trade Poly / Trade Kalshi deep-link"
call in v0.5. Managed-email digest stays open as a future milestone.

UI surface: a small `Subscribe via RSS ↗` link in the action-feed
filter row that mirrors the current filter chips into the URL — pick
your filter on the dashboard, copy the link, paste into your reader.

### v1.3 — SEC litigation releases

`ingestion/sec_litigation_rss.py` adds a new `RssSource` (code
`SEC-LIT`, name "SEC Litigation Releases") and registers it in
`unified_feed._SOURCES`. From there it flows through the v0.1 → v0.5
pipeline automatically: items get type-classified (LR titles like
"SEC Charges X with Fraud" reliably score `enforcement`), severity
extracted, topics tagged, and market-matched against Polymarket /
Kalshi. The new code shows up as its own row in the heatmap and its
own entry in the per-source status row at the bottom of the action
feed.

No new endpoint, no new UI panel — the existing surfaces just gain
a new source. That's the architectural payoff of the v0 → v0.5
modular pipeline: adding a regulator stream is a single file +
two-line `_SOURCES` edit.

**PACER scope note:** the original v1.3 spec called for "PACER
scraper for SEC litigation releases — paid feed, deferred." v1.3
delivers the FREE half (SEC's own LR feed); deep PACER per-case
access (complaints, motions, exhibits, court dockets) stays
deferred until there's budget plus a clear cost-justified use case.

### v2.0 — global regulator expansion

The v0 → v1.6 architecture (single-file config → automatic pipeline) pays
off here. `ingestion/sources.py` is now the only place RSS sources are
declared; everything downstream — classifier, severity, topics, market
match, heatmap, stance, diff, RSS feed, email digest — picks them up
automatically.

**Sources (28 total, 13 jurisdictions):**

| Region | Bodies |
|---|---|
| US | SEC, SEC-LIT, CFTC, FinCEN, OCC, FDIC, CFPB, OFAC |
| UK | FCA, PRA, BoE |
| EU | ESMA, EBA, EIOPA, ECB |
| Continental EU | BaFin (DE), FINMA (CH), AMF (FR), CONSOB (IT) |
| APAC | MAS (SG), HKMA (HK), SFC-HK, ASIC (AU), RBA, SEBI (IN), RBI |
| Americas (non-US) | OSC (CA), CVM (BR) |

`ingestion/court_cases.py` adds **CJEU, UK judiciary, and SCOTUS** as a
parallel free-RSS source pipeline. Items are keyword-filtered against
financial / regulatory / sanctions terms so the dashboard isn't drowned
by generic civil litigation. Surfaces in a dedicated **Court cases** UI
panel between the hearings panel and the stance ladder. **8/8 relevance
fixtures pass** (securities/MiFID/crypto-AML/OFAC matched; family law,
immigration, patent cases correctly filtered out).

`data/personnel.py` expanded from 4 to **18 entries** across 15 regulators
(US Fed, SEC ×4, CFTC, CFPB; UK FCA, BoE; EU ESMA, ECB; DE BaFin; CH
FINMA; SG MAS; HK HKMA; AU ASIC; IN SEBI; JP BoJ). Each carries a
verifiable `source_url`. Same loud-comment caveat applies: best-effort,
verify before relying, auto-scrape on the deferred roadmap.

`analysis/market_match.ANCHOR_TOKENS` expanded from 36 to **81 tokens** —
every new regulator code plus notable people (Lagarde, Bailey, Ueda,
Behnam, Pham, Peirce, Crenshaw, etc.) so the prediction-market matcher
catches markets about the wider roster.

The jurisdiction filter-chip row in the action-feed panel is now
**populated dynamically** from `/api/sources` — adding a new code to
`ingestion/sources.py` auto-surfaces a chip with no HTML edit.

**Known UI scaling limit:** the heatmap was designed for 3-5 regulators
per render. With 28 rows it'll be tall and most rows sparse. A
"collapse low-volume regulators" toggle would help; deferred until
real usage shows whether the noise actually hurts. The other panels
(action feed, personnel, courts) handle the larger source pool fine
because they're already row-oriented.

**v2.1 addressed this:** `/api/heatmap` now defaults `show_empty=false`
— regulators with zero items in the window are auto-hidden so the chart
stays readable at 54 sources. Pass `?show_empty=true` to render every
registered body, including zeros, when debugging coverage.

**Same URL-verification caveat as every other RSS module:** the seeded
URLs are best-guess against common regulator RSS conventions. The
graceful-degradation lane (per-source `ok=false` with a real error
string) means a bad URL is visually obvious in the per-source status
row at the bottom of the action feed. Drop the live URL into
`ingestion/sources.py` and redeploy; no other code change needed.

### v1.6 — managed email digest

End-to-end subscriber flow with double-opt-in confirmation and
manual cron-driven dispatch:

  1. **`POST /api/subscribe`** with `{email, filter}` — creates a
     `pending` row in `data/digest.sqlite` (SQLite, configurable
     via `DIGEST_DB_PATH`) and sends a confirmation email.
  2. **`GET /api/subscribe/confirm?token=…`** — email-click flips
     `pending → confirmed`. Renders a small HTML "you're subscribed"
     page.
  3. **`POST /api/digest/send_now`** (admin-token-gated) — finds
     every confirmed subscriber whose `last_sent_at < today UTC`,
     filters the cached feed against each subscriber's `filter`,
     renders the digest (HTML + text), sends, marks
     `last_sent_at=now`. Designed to be hit by external cron /
     k8s CronJob — no in-process scheduler to babysit.
  4. **`GET /api/subscribe/unsubscribe?token=…`** — email-click
     flips to `unsubscribed`. Idempotent; the row stays around for
     audit.

**SMTP shape:** `ingestion/email_send.py` uses stdlib `smtplib`
with optional STARTTLS. If `SMTP_HOST` is unset the sender
**DRY_RUNs** — logs the message and returns `ok=True, dry_run=True`.
That lets the sandbox + staging exercise the full subscribe →
confirm → send-now flow without real delivery. For production
deliverability at scale (DKIM, SPF, dedicated IP, bounce processing)
swap in a managed provider (Postmark, SendGrid, AWS SES) by
replacing the one `send()` function — caller surfaces unchanged.

**Auth shape:** all four endpoints are added to the gateway-SSO
bypass list since email-click links can't supply custom headers.
`/api/digest/send_now` is gated on `DIGEST_ADMIN_TOKEN` (query
param or `X-Admin-Token` header); the three subscribe routes are
gated by the secrecy of their per-subscriber `confirm_token` /
`unsubscribe_token` (32-byte URL-safe random per row).

**Auto bounce-handling is deferred** — a hard-bounce mailbox
should mark itself `unsubscribed` after N strikes, but that
requires receiving bounce notifications, which is provider-
specific. Out of scope for v1.6; tracked as the next polish lift.

**UI surface:** a small `email@example.com [Email me daily ↗]`
form inline in the action-feed filter row. Filter state is
auto-captured into the subscribe payload so users get whatever
they're currently looking at, not a global digest.

**Operational note:** add a cron entry like
`0 16 * * * curl -X POST https://your-host/api/digest/send_now -H "X-Admin-Token: $TOKEN"`
to dispatch daily at 16:00 UTC. The endpoint is idempotent on
re-hit (it skips subscribers already sent today), so doubled cron
firings are safe.

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
| **v0.7** | ✓ done | Per-regulator speech stance ladder — SEC `pro-enforcement ↔ light-touch`, FCA `pro-innovation ↔ consumer-first`, ESMA `prescriptive ↔ principles-based`; matched phrases shown as chips |
| **v1.0** | ✓ done | Closes v1.0 — all seven sub-milestones (v0 → v0.7) shipped on the SEC + FCA + ESMA seed source set |
| **v1.1** | ✓ done | Confirmation-hearing tracker — Senate Banking + House FS feeds, filtered to nomination/confirmation items, with regulator-hint tag |
| **v1.2** | ✓ done | Statement diff viewer — latest-vs-prior speech per regulator with token-level inline diff and similarity score |
| **v1.3** | ✓ done (LR only) | SEC Litigation Releases pulled as a new source code `SEC-LIT`; flows through the v0.1 → v0.5 pipeline (classifier, severity, topics, market match, heatmap) automatically. Deep PACER per-case scraping (complaints, motions, exhibits) remains deferred — paid feed, ROI unproven. |
| **v1.4** | ✓ done | OFAC SDN delta-per-day — fetch + parse Treasury `sdn.xml`, persist daily digest, compute additions/removals + per-program counts |
| **v1.5** | ✓ done | RSS alert feed at `/feed.xml` mirroring all `/api/feed` filters; subscriber gate via `RSS_SHARED_TOKEN` |
| **v1.6** | ✓ done | Managed email digest — SQLite subscriber store, double-opt-in confirmation flow, manual `POST /api/digest/send_now` (external cron drives schedule), DRY_RUN fallback when SMTP_HOST unset. Auto bounce-handling deferred. |
| **v2.0** | ✓ done | **Global regulator expansion** — 28 sources across 13 jurisdictions (US/UK/EU/DE/CH/FR/IT/SG/HK/AU/IN/CA/BR). New `ingestion/sources.py` master config; new court-cases module (CJEU + UK + SCOTUS); personnel roster grew from 4 to 18; jurisdiction filter chips populated dynamically from `/api/sources`; anchor tokens expanded to 81. |
| **v2.1** | ✓ done | **Wider international coverage** — 54 sources across 34 jurisdictions. Added 8 European national bodies (CNMV/AFM/DNB/FI-SE/Finanstilsynet/KNF/CSSF/FSMA-BE/OENB), 4 more APAC (FSC-KR/FSC-TW/SEC-TH/BNM, plus JFSA placeholder), 5 MENA/Africa (CMA-SA/SCA-AE/DFSA/ISA-IL/FSCA), 3 more LatAm (CNBV/CMF-CL/SFC-CO), 4 international/supranational (FATF/BIS/IOSCO/FSB). Personnel grew 18→27. Anchor tokens 81→108. New heatmap `hide_empty=true` default so the chart stays readable as the source list grows. |
| **v2.2** | ✓ done | **Heatmap jurisdiction-grouping + parliament hearings + JFSA HTML scraper.** Heatmap gains `?group_by=jurisdiction` (collapses 54 sources → 34 country rows, useful at this scale). UK Treasury/PAC + EU ECON/JURI committees wired as `parliament_hearings.py` with a verb×topic regex filter (10/10 fixtures pass). JFSA placeholder replaced with a real HTML scraper (`jfsa_scraper.py`) that integrates as a non-RSS source alongside the RSS pipeline — proves the pattern for any future HTML-only jurisdiction. |
| **v2.3** | ✓ done | **Legislative-bills tracker + CSV export + test runner.** `legislative_bills.py` adds US Congress + UK Parliament + EU legislative-procedure feeds with the same verb×topic filter (10/10 fixtures pass). `/api/feed.csv` exports any filtered slice for analyst pipelines. `run_tests.sh` runs all 17 fixture suites in one shell command — green/red exit code for CI. |
| **v2.4** | ✓ done | **5 new stance axes + personnel deepening + CI workflow.** Stance now covers SEC/FCA/ESMA/**BoE/CFTC/Fed/ECB/MAS** with each body's real contested pole (pro-stability↔pro-growth for BoE; hawkish↔dovish for Fed/ECB; pro-innovation↔pro-stability for MAS, etc.). 16/16 stance fixtures pass. Personnel roster grew 27→42 across 30 regulators (Fed Board fleshed out, OCC/FDIC/FinCEN, BIS/IOSCO/FATF, ECB Executive Board). Anchor tokens 108→122. `.github/workflows/regulators-dashboard-tests.yml` runs `run_tests.sh` on every push/PR touching the dashboard. |
| **v2.5** | ✓ done | **Quality pass: parallel fetch + dedup + stale flag.** `unified_feed._fetch_all` now submits all 55 sources to a `ThreadPoolExecutor` (workers=16) — cold-cache `/api/feed` drops from sequential-worst-case ~4 min to **~5 s**. Cross-source dedupe by `link` (SEC + SEC-LIT duplicates collapse to first occurrence); `deduped_count` exposed top-level. Per-source `stale=true` flag when `ok=true` but newest item is > 60 days old — catches silently-broken feeds. UI per-source status row gains a yellow tier between green/red. |
| later | open  | Extend source list (CFTC, FinCEN, OFAC, BaFin, FINMA, MAS, HKMA, JFSA, ASIC) |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7080` | Override listen port. |
| `SDN_SNAPSHOT_DIR` | tempfile path | Where v1.4 persists per-day OFAC SDN digests. Set to a mounted volume in production. |
| `RSS_SHARED_TOKEN` | unset | Token gating `/feed.xml` (v1.5). Required outside `DEV_MODE`; subscribers append `?token=<value>` to the URL. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` / `SMTP_STARTTLS` | unset | v1.6 SMTP config. `SMTP_HOST` unset = DRY_RUN. |
| `DIGEST_DB_PATH` | tempfile path | v1.6 SQLite subscriber DB path. Production = mounted volume. |
| `DIGEST_ADMIN_TOKEN` | unset | v1.6 — gates `POST /api/digest/send_now`. Required outside DEV_MODE. |
| `PUBLIC_BASE_URL` | request origin | v1.6 — base URL baked into email confirm/unsubscribe links. |

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
- **Stance is scored on title + summary, not body text.** Speeches
  are usually punchy enough that the RSS-level signal carries the
  axis lean. If a regulator's recent speech is dead-zero, the badge
  reports NEUTRAL — which is honest given the matched-phrase chips
  underneath show zero hits. Full-body HTML fetching (mirroring
  `centralbank-dashboard/ingestion/cb_statements._fetch_statement_body`)
  is the next polish lift if summaries prove insufficient.
- **Stance axes are per-regulator and intentionally asymmetric.**
  An SEC speech is scored on enforcement intensity, an FCA speech on
  consumer-vs-growth orientation, an ESMA speech on rulebook
  rigidity. A generic "strict ↔ lax" axis would muddle the signal.
  Adding a new regulator requires picking its real contested axis
  and seeding `stance_keywords.AXES` accordingly.
- **Polymarket overlay coverage is thin** outside crypto ETFs and
  big-name settlements, so v0.5 will only annotate a small fraction of
  action cards. That's expected.
- **No CFTC / FinCEN / OFAC in v0.** They're listed in the v1.0 source
  expansion. v0 keeps to three RSS sources to surface the architecture
  without spending the iteration budget on per-regulator feed quirks.
