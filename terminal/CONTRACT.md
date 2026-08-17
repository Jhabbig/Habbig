# narve Terminal v0.5 — Build Contract (TRIMMED SCOPE)

Scope per founder decision: **the app shell (mac+win) + the credibility system
only.** Models/reader/LLM/reports-prose/PDF are OUT — leave clean seams.
Look: **Bloomberg-terminal-esque** — dark-first, dense, mono, panel grid —
while staying strictly monochrome per narve rules (no amber, no color-as-info).
Priority: SIMPLE and WORKING end-to-end over completeness.

## Already on disk (salvaged — do not recreate, do not move)
`app/index.html, app/package.json, app/tsconfig.json, app/vite.config.ts,
app/src/fonts/{Inter,InstrumentSerif-Italic,SourceSerif4,GeistMono}.woff2,
app/src/img/logo.png, app/src/styles/tokens.css` — tokens.css is verbatim from
the main repo; keep it, layer the terminal look in app.css on top.

## Toolchain
Python `/opt/homebrew/bin/python3.11` · Node 26 · Rust stable (installed,
`source ~/.cargo/env`). Sidecar = FastAPI on **127.0.0.1:41733**. SQLite WAL,
one DB file (env `NARVE_DB_PATH`, default `~/.narve-terminal/terminal.db`).

## Layout (exact)
```
terminal/
  sidecar/
    pyproject.toml
    narve_sidecar/__init__.py  db.py  credibility.py  ingest.py  sample_data.py  server.py
    narve_sidecar/migrations/001_init.sql
    tests/  (conftest.py, test_credibility.py, test_ingest.py, test_api.py)
  app/
    (salvaged files above)
    src/main.ts  src/router.ts  src/api.ts  src/styles/app.css
    src/screens/markets.ts  src/screens/sources.ts  src/screens/question.ts  src/screens/ingest.ts
    src-tauri/Cargo.toml  tauri.conf.json  build.rs  src/main.rs  capabilities/default.json  icons/
```

## Schema (001_init.sql — exact)
```sql
sources(id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
        alpha REAL NOT NULL DEFAULT 2.0, beta REAL NOT NULL DEFAULT 2.0,
        is_sample INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
questions(id TEXT PRIMARY KEY, title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'live', resolved_outcome INTEGER,
          resolved_at TEXT, is_sample INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL);
predictions(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,
            question_id TEXT NOT NULL, p REAL NOT NULL,
            stated_at TEXT NOT NULL, note TEXT,
            UNIQUE(source_id, question_id, stated_at));
market_snapshots(id INTEGER PRIMARY KEY, venue TEXT NOT NULL,
                 market_id TEXT NOT NULL, question_id TEXT NOT NULL,
                 yes_price REAL NOT NULL, liquidity REAL,
                 captured_at TEXT NOT NULL,
                 UNIQUE(venue, market_id, captured_at));
credibility_events(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,
                   question_id TEXT NOT NULL, old_alpha REAL, old_beta REAL,
                   new_alpha REAL, new_beta REAL, at TEXT NOT NULL);
ingest_log(id INTEGER PRIMARY KEY, file_name TEXT, kind TEXT NOT NULL,
           rows_ok INTEGER NOT NULL, rows_err INTEGER NOT NULL,
           sha256 TEXT NOT NULL UNIQUE, uploaded_at TEXT NOT NULL);
schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
```
Resolutions live ON questions (status/resolved_outcome/resolved_at) — no
separate table in v0.5.

## The credibility system (the product core — exact math, pinned by tests)
- Beta posterior, priors α=2.0 β=2.0. `credibility = α/(α+β)` (new source ⇒ 0.5).
- Resolution of question Q with outcome yes|no: for EVERY source with ≥1
  prediction on Q, take its LATEST prediction p; hit iff `(p>=0.5)==(outcome==yes)`;
  hit ⇒ α+=1 else β+=1. Write a credibility_events row per source. `void` ⇒
  status='void', no score changes. Re-resolving a resolved question = 409 error.
- Consensus on a question: `combined_p = Σ(p_i·cred_i)/Σ(cred_i)` over each
  source's latest prediction. Edge = `combined_p − latest yes_price` (most
  recent snapshot across venues). No liquidity ranking in v0.5 — sort by |edge|.
- Brier per source (display only): mean over resolved predictions of
  `(p − outcome)²` using latest-per-question.

## Ingest (CSV or JSON array; strict; ALL row errors at once)
Kinds & columns:
- predictions: `source_id, question_id, predicted_probability, stated_at, note?`
  (question auto-created with title=id if unknown; source auto-created neutral)
- markets: `venue, market_id, question_id, yes_price, liquidity?, captured_at`
- resolutions: `question_id, outcome (yes|no|void), resolved_at`
Errors: `[{line, reason}]`, 1-based data lines. Idempotent: UNIQUE keys +
INSERT OR IGNORE → `dedup_skipped` count; identical file (sha256) short-circuits
with `already_ingested: true`. XLSX is OUT of v0.5 (CSV+JSON only — README note).

## Sample data
Derive from `~/Habbig/gateway/lab_pages/markets.py::_events()` (12 rows:
title, question_id, source_id, credibility, narve_p, market_price, fresh).
Create sample: sources with integer α,β hitting the listed cred ±0.005
(α+β between 8 and 30), the questions, one latest prediction per row at
narve_p, one polymarket snapshot at market_price. Plus 2 PRE-RESOLVED sample
questions (one hit one miss for two of the sources) so the SOURCES screen
shows movement out of the box. Everything is_sample=1, ids prefixed `sample:`
(questions keep their slug ids). POST /sample/load idempotent.

## API (FastAPI; keys/shapes exact; CORS allow tauri://localhost + http://localhost:1420)
```
GET  /health              -> {"ok":true,"version":"0.5.0","db":"<path>"}
POST /ingest/{kind}       multipart file OR json {rows:[...]}
                          -> {ok_rows,err_rows,dedup_skipped,already_ingested,errors:[{line,reason}]}
GET  /templates/{kind}.csv
POST /sample/load         -> {loaded:true,counts:{sources,questions,predictions,snapshots}}
GET  /sources             -> [{id,name,alpha,beta,credibility,n_resolved,n_live,brier,is_sample,last_active}] cred desc
GET  /sources/{id}        -> above + events:[credibility_events] + predictions list
GET  /questions           -> [{id,title,status,n_sources,combined_p,market_price,edge,is_sample,updated_at}] |edge| desc
GET  /questions/{id}      -> {question, per_source:[{source_id,credibility,p,stated_at}],
                              combined_p, market:[latest per venue], history:[snapshots asc]}
POST /resolve             {question_id,outcome,resolved_at?} -> {resolved:true,moves:[{source_id,old_cred,new_cred,hit}]}
GET  /raw/{kind}          ?question_id&source_id&limit=200&offset=0 -> {rows,total}
```

## App (Tauri v2 + plain strict TS — no framework)
- src-tauri: productName "narve Terminal", identifier ai.narve.terminal,
  window 1280×800 min 980×640 titled "narve Terminal", bundle targets dmg+msi.
  Dev: sidecar spawned by `tauri-plugin-shell` running
  `/opt/homebrew/bin/python3.11 -m narve_sidecar.server` with cwd terminal/sidecar
  (document the packaging seam: v1 ships pyinstaller/briefcase later — README).
  Kill sidecar on window close. Icons: PNGs from logo.png via sips.
- Router (hash): #/markets (default) #/sources #/question/:id #/ingest.
  Each screen module: `export function mount(root: HTMLElement, params: Record<string,string>): void`.
- BLOOMBERG-ESQUE look (app.css, dark-first; honour [data-theme="light"] too):
  near-black base from tokens (--bg-void/--bg-base), 1px hairline panel grid,
  EVERYTHING data in Geist Mono 12-13px, ALL-CAPS 10px letter-spaced labels,
  `[ MARKETS / 01 ]` section markers, top command bar (logo chip · screen tabs
  MARKETS/SOURCES/INGEST · clock UTC ticking · LOCAL badge), bottom status bar
  (db path · row counts · last ingest · version), row hover = bg step not color,
  signed numbers rendered with explicit +/− (never color), tables dense
  (row-height ~26px), no shadows, radius ≤4px for the terminal look.
- Screens:
  MARKETS: the flagship dense grid — QUESTION · SRCS · NARVE P · MKT · EDGE ·
    STATUS · UPD, sorted |edge| desc, SAMPLE tag chip on sample rows, click →
    #/question/:id. Header strip: counts + "resolve feeds credibility — the loop".
  SOURCES: leaderboard — SOURCE · CRED (bar drawn as ▮▮▮ mono blocks) · α/β ·
    RESOLVED · LIVE · BRIER · LAST; expandable row → credibility_events history.
  QUESTION: per-question drill — big combined_p, market prices, per-source table
    (SOURCE · CRED · P · STATED), RESOLVE YES/NO/VOID buttons (confirm modal)
    that then show the credibility moves inline (old→new per source).
  INGEST: 3 upload cards (predictions/markets/resolutions) drag-drop+picker,
    template download links, LOAD SAMPLE DATA button, result panel with full
    error table (LINE · REASON), raw-data peek table underneath (GET /raw).
- Empty states: real copy. Errors: real copy. Never "something went wrong".

## Process rules (every agent)
- Sync bash only; NEVER run_in_background; no sleep/poll loops; no git.
- Writes ≤100 lines per Write call, extend with Edit.
- Only your assigned files. python3.11 full path. Verify your own surface:
  py_compile / pytest for python; `cd app && npx tsc --noEmit` for TS (deps are
  installed; if node_modules missing run npm install synchronously).
- Tests pin: Beta math by hand (2,2 → hit → 3/5=0.6), the resolution loop,
  blend `Σ(p·c)/Σc`, idempotent re-upload, all-errors-at-once validation,
  sha256 short-circuit, /resolve 409 on double-resolve, sample load counts.
```
