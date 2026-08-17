# narve Terminal

Desktop app (macOS + Windows) that answers one question: **who is actually
credible about what happens next?** v0.5 ships the credibility system — the
resolve→credibility loop — inside a Bloomberg-terminal-style monochrome shell.
Everything runs on one machine; data enters by manual upload; no network calls.

## What's in v0.5

- **Sidecar** (`sidecar/`) — Python 3.11 + FastAPI on `127.0.0.1:41733`,
  SQLite (WAL) at `~/.narve-terminal/terminal.db` (override: `NARVE_DB_PATH`).
  The credibility engine: Beta(2,2) posterior per source, consensus blend,
  edge vs market, strict CSV/JSON ingest, sample dataset. 38 tests.
- **App** (`app/`) — Tauri v2 + plain strict TypeScript (no framework).
  Screens: MARKETS (|edge|-sorted tape) · SOURCES (credibility leaderboard +
  history) · QUESTION (drill-down + RESOLVE flow) · INGEST (drag-drop uploads,
  templates, sample loader, raw-data browser).

Cut from v0.5 (clean seams, come back in v1): per-question models, the
reader/LLM reasoning prose, PDF reports, XLSX ingest, stock models, live APIs.

## The math (pinned by tests — do not improvise)

- Credibility: Beta posterior, priors `α=2.0, β=2.0` → new sources start at
  0.500 and can never reach 0 or 1. On resolution, each source's **latest**
  prediction on the question is a hit iff `(p ≥ 0.5) == (outcome == yes)`;
  hit → α+1, miss → β+1; `void` changes nothing. Every move is journaled in
  `credibility_events`.
- Consensus: `combined_p = Σ(p_i · cred_i) / Σ(cred_i)` (latest per source).
- Edge: `combined_p − latest market yes_price`.
- Brier (display): mean `(p − outcome)²` over a source's resolved calls.

## Run it (dev)

```bash
# 1. sidecar
cd terminal/sidecar
/opt/homebrew/bin/python3.11 -m pip install --user fastapi uvicorn pydantic python-multipart
/opt/homebrew/bin/python3.11 -m narve_sidecar.server            # 127.0.0.1:41733

# 2. app (second terminal)
cd terminal/app
npm install
npm run tauri dev        # native window; spawns nothing extra in release builds
# or, browser-only UI: npm run dev  (vite on :5173)
```

First run: INGEST → **LOAD SAMPLE DATA** → MARKETS. Resolve any question from
its drill-down and watch the SOURCES leaderboard move. That loop is the product.

## Tests

```bash
cd terminal/sidecar && /opt/homebrew/bin/python3.11 -m pytest -q   # 38 passed
cd terminal/app && npx tsc --noEmit && npm run build
```

## Installers

CI (`.github/workflows/terminal-build.yml`): macOS universal2 `.dmg` +
Windows `.msi`, unsigned (signing hooks documented inline). Trigger on push to
`terminal/**` or manually via workflow_dispatch; artifacts on the run page.

## Ingest schemas (CSV header row or JSON array of objects)

| kind | columns |
|---|---|
| predictions | `source_id, question_id, predicted_probability, stated_at, note?` |
| markets | `venue, market_id, question_id, yes_price, liquidity?, captured_at` |
| resolutions | `question_id, outcome (yes\|no\|void), resolved_at` |

Templates download in-app. Validation reports **every** row error at once
(`LINE · REASON`). Re-uploads are idempotent (natural-key dedupe + whole-file
sha256 short-circuit). Unknown `source_id`s are auto-created at the neutral
prior. Nothing is ever silently dropped.

## Decisions (boring on purpose)

- Sidecar transport: localhost HTTP (curl-able, debuggable) over stdio-RPC.
- Resolutions live on `questions` (status/outcome/at), no separate table.
- Dev builds spawn the sidecar via `tauri-plugin-shell` + system python;
  release builds expect a bundled sidecar binary (pyinstaller — v1 seam,
  documented in `app/src-tauri/src/main.rs`).
- Sample data derives from `gateway/lab_pages/markets.py` (12 live questions +
  2 pre-resolved so credibility shows movement out of the box); everything is
  tagged `SAMPLE` in the UI.

## v1 grows back

API pullers are just another writer of the same ingest rows; per-question
models enter the blend as `model:<name>` sources; the reader/LLM writes prose
over the same report struct. The seams are marked in code comments.
