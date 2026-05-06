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
- `GET /api/frontier` — running max-score series per benchmark, computed off
  merged scores (live values bump the curve).
- `GET /api/markets` — live Polymarket AI markets (60s cache)
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

## Per-cell freshness

In the leaderboard, the small dot in each score cell is a freshness signal:

- **green** — value is from a live scrape (hover for source + timestamp).
- **none** — curated, recent (within 60 days of `as_of`).
- **amber** — curated but the row's `as_of` is older than 60 days.
- **gray** — no value reported.

Adjust `live_data.STALE_DAYS_THRESHOLD` to taste.
