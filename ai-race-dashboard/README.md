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
- `GET /api/models`
- `GET /api/timeline`
- `GET /api/frontier` — running max-score series per benchmark
- `GET /api/markets` — live Polymarket AI markets (60s cache)
