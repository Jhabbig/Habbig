# AI Race Dashboard

Tracks the AI race across major frontier labs:

- **Frontier model leaderboard** — best public scores on MMLU-Pro, GPQA Diamond,
  SWE-bench Verified, AIME, HLE, LMArena Elo, LiveCodeBench.
- **Capability frontier chart** — running max-score per benchmark over time.
- **Release timeline** — major model releases since ChatGPT.
- **Lab snapshots** — last-known valuation, headline model, compute posture,
  open-weights stance.
- **Live AI prediction markets** — Polymarket questions matching AI keywords,
  pulled live (≤60s cache) via the Gamma API.

Port: **7070**. Subdomain key: **`airace`** (gateway). DEV bypass: `DEV_MODE=1`.

---

## Data maintenance

All curated values live in [`data.py`](./data.py). Each row carries:

- `as_of` (`YYYY-MM`) — when the value was last reviewed.
- `source` — short label pointing at the primary report (lab blog, system
  card, Reuters/Bloomberg, etc.).

`DATASET_AS_OF` at the top of the file is the wholesale "last reviewed" date.
Update it whenever you sweep through.

When a new frontier model lands:

1. Add a row to `MODELS` with scores. Use base/non-tool numbers when the lab
   reports both, unless flagged.
2. Add a `TIMELINE` entry.
3. Bump `DATASET_AS_OF`.

When a benchmark gets saturated or replaced, add a new entry to `BENCHMARKS`
and start populating it on new model rows. The frontier chart picks it up
automatically.

## Run locally

```bash
cd ai-race-dashboard
pip install -r requirements.txt
DEV_MODE=1 python3 server.py        # → http://127.0.0.1:7070
```

## Endpoints

- `GET /` — single-page UI
- `GET /api/health`
- `GET /api/labs`
- `GET /api/benchmarks`
- `GET /api/models` — curated rows merged with live scrapes; each cell carries
  `score_meta` with provenance (`curated` / `live:<source>`), `as_of`, and a
  `stale` flag.
- `GET /api/timeline`
- `GET /api/compute` — per-lab compute scoreboard
- `GET /api/export-controls` — chip export-control timeline
- `GET /api/capex` — Big Tech quarterly AI capex
- `GET /api/talent` — researcher moves + lab headcount estimates
- `GET /api/news` — RSS fan-in across AI feeds (90s cache)
- `GET /api/frontier` — running max-score series per benchmark, computed off
  merged scores (live values bump the curve).
- `GET /api/markets` — keyword-filtered Polymarket AI markets (legacy view)
- `GET /api/markets/featured` — curated Polymarket events + Kalshi series
  with full multi-outcome trees; includes cross-venue spread pairs
- `GET /api/markets/moves?min_change=0.05&limit=12` — top 24h price movers
  among AI-tagged questions (Polymarket only — Kalshi's public events
  endpoint doesn't surface a 1d delta)
- `GET /api/sources` — per-source ingestion status (last fetch, errors,
  entry count)
- `POST /api/refresh` — force-refresh every ingestion source

## Live ingestion (v2.1)

The dashboard runs a background thread that refreshes ingestion sources
once per hour. Each source is in `ingestion/`:

| Source | File | Benchmark | TTL | Status |
| --- | --- | --- | --- | --- |
| LMArena (Chatbot Arena) | `ingestion/lmarena.py` | `lmarena_elo` | 1h | best-effort URL list — update if HF Space reorganizes |
| HuggingFace Open LLM Leaderboard v2 | `ingestion/openllm.py` | `mmlu_pro`, `gpqa_diamond` | 1h | uses datasets-server rows API |
| SWE-bench Verified | `ingestion/swebench.py` | `swe_bench_verified` | 6h | best-effort GitHub raw URLs |

Each source is wrapped in a `TTLCache` (in-memory, thread-safe). On fetch
failure the cache *retains the last successful payload* so the UI keeps
showing live data with a "last_ok_at" timestamp until the next successful
poll. The fetched scores are merged into `data.MODELS` by the
`live_data.match_model()` matcher, which uses normalized names + an alias
table to bridge identifiers like `claude-opus-4-5-20250930` ↔ `Claude Opus
4.5`.

When a public leaderboard restructures and our scrapers break:

1. The dashboard keeps working — curated values become the fallback.
2. `/api/sources` shows `ok: false` with the error message; the UI sources
   panel surfaces this.
3. To fix: update the URL constants at the top of the relevant
   `ingestion/<source>.py`, then trigger `/api/refresh`.

To disable live ingestion entirely (e.g. for sandboxed local dev), set
`DISABLE_INGESTION=1`.

## Talent & news (v2.4 + v2.7)

- **Talent flow** — curated list of senior researcher/leadership moves
  in `data.TALENT_MOVES`, plus `HEADCOUNT` estimates. Move kinds:
  `founder` (left to start a new lab), `hire`, `return` (back to a
  previous employer or promoted in place), `exit` (left without
  immediate destination disclosed). `TALENT_ORG_LABELS` adds display
  metadata for orgs that aren't in `LABS` (Microsoft, Safe
  Superintelligence, Thinking Machines, etc.).
- **News feed** — RSS fan-in (`news.py`) across lab blogs, AI
  newsletters, and arXiv cs.CL, mirroring the proven
  world-state-dashboard pattern (defusedxml-parsed, ≤90s cache,
  per-feed best-effort with graceful degradation). Feed list lives in
  `data.NEWS_FEEDS`. UI has a kind filter (all / lab / research /
  newsletter / community).

Both add zero new server-side dependencies beyond `defusedxml` (already
used by world-state); both degrade gracefully when upstream is
unreachable.

## Compute & infra (v2.2)

The "industrial layer" view — the inputs that drive the race rather than
the model outputs.

- **Compute scoreboard** — per-lab H100-equivalents (common-denominator
  estimates from public reporting), flagship cluster, 2025 announced AI
  capex. TPU-only labs (Google DeepMind) annotated separately. Bar widths
  normalize against the highest-disclosed fleet.
- **Chip export-control timeline** — US BIS rules + non-US responses,
  separate from the model timeline because cadence and audience differ.
- **Big Tech AI capex** — quarterly capex sparklines for the four
  hyperscalers (MSFT, GOOGL, META, AMZN), curated from earnings releases.
  Latest combined quarter is displayed below the cards.

All three views are curated in `data.py` (`COMPUTE`, `EXPORT_CONTROLS`,
`CAPEX_QUARTERLY` + `CAPEX_TICKERS`). Compute is the most volatile — most
rows carry their own `as_of`. Capex needs a refresh each earnings season
(append a row to `CAPEX_QUARTERLY` with the new quarter's numbers).

## Capability views (v2.6)

Three derived views built off the same `/api/models` payload — no extra
endpoints, so they update automatically when live scrapers bump scores.

- **Benchmark saturation** — horizontal bars per benchmark showing
  current best vs. effective ceiling, with a gray tick for the
  informed-human reference where one applies. Ceilings live in
  `BENCHMARKS[*].ceiling` in `data.py`; null = unbounded (e.g. LMArena
  Elo) so the bar uses a visual cap and no ceiling tick.
- **Lab capability radars** — one mini-radar per lab, taking the lab's
  highest-average-normalized-score model. Each axis is normalized to its
  benchmark's `floor`/`ceiling`. Spiky polygons = specialist; round =
  balanced.
- **First-to-X waterfall** — matrix showing which model first crossed
  each public threshold per benchmark, with release date. Dashed cells
  haven't been crossed yet by anything in the tracked set.

When you add a new benchmark to `BENCHMARKS`, populate `floor`,
`ceiling`, and `human_baseline` (the latter may be `None`) — all three
views adapt without UI changes.

## Featured prediction markets (v2.5)

Two markets layers run side by side:

1. **Curated events** — operator-maintained whitelists in `data.py`:
   `AI_POLY_EVENT_SLUGS` (Polymarket event slugs) and `AI_KALSHI_SERIES`
   (Kalshi series tickers). The dashboard fetches the full multi-outcome
   tree per entry, so one whitelist row can expand to many Yes/No bars
   (e.g. "which lab releases the best model of 2026" → one bar per lab).
   Bad slugs are silently dropped at fetch time.
2. **Keyword-matched markets** — algorithmic discovery via
   `AI_MARKET_KEYWORDS`, used for the secondary "More AI markets" view
   and the 24h movers panel.

Code lives in `markets/`:
- `markets/polymarket.py` — `/events?slug=…` for featured, full markets
  scan for movers.
- `markets/kalshi.py` — `/trade-api/v2/events?series_ticker=…&with_nested_markets=true`,
  prices normalized cents → 0–1.
- `markets/__init__.py` — aggregator + cross-venue pairer.

**Cross-venue spreads.** When the same topic trades on both venues we pair
them and surface the Yes-vs-Yes spread (positive ⇒ Polymarket overpriced
relative to Kalshi). Matching is conservative: requires ≥2 substantive
token overlap *including* a named entity (lab name, "agi", "nvidia", etc.)
to avoid pairing on coincidental date matches.

**Adding a new event.** Visit polymarket.com or kalshi.com → find the
event → copy the slug (Polymarket) or series ticker (Kalshi) → append to
the relevant list in `data.py`. The featured panel will pick it up within
90 seconds.

## Per-cell freshness

In the leaderboard, the small dot in each score cell is a freshness signal:

- **green** — value is from a live scrape (hover for source + timestamp).
- **none** — curated, recent (within 60 days of `as_of`).
- **amber** — curated but the row's `as_of` is older than 60 days.
- **gray** — no value reported.

Adjust `live_data.STALE_DAYS_THRESHOLD` to taste.
