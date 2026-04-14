# midterm-dashboard/backend/ — FastAPI server

The Python half of the Midterm Dashboard. FastAPI app that pulls election
predictions from Polymarket / Kalshi / PredictIt / polling aggregators,
stores them in SQLite, and exposes JSON endpoints + the built React frontend
on port 8051.

Run standalone:

```bash
pip install -r requirements.txt
python3 main.py
```

Or rely on the multi-stage `Dockerfile` in the parent directory, which builds
the frontend then runs this backend.

## Files in this directory

| File | Purpose |
|---|---|
| `main.py` | The FastAPI app. Routes (`/data/*`, `/auth/*`, `/admin/*`), gateway SSO middleware, CORS for the Vite dev server, background data-refresh task (every 5 min), serves the built `frontend/dist/` as static files in production. Also exposes the **admin human-review** endpoints — `POST/DELETE /admin/race/{key}/flag` to mark a (source, source_id) pair as the wrong market for a race, and `POST/DELETE /admin/race/{key}/verify` to mark a race pairing as human-confirmed. `divergence_calculator`, `data_races`, and `data_race_detail` all consult `Database.get_all_wrong_flags()` once per request and skip flagged pairs so flagged matches disappear from listings and the divergence chart. |
| `database.py` | SQLite layer (WAL mode, threading lock, contextmanager). User auth was migrated out — the gateway handles users/sessions; this module only manages dashboard data and the shared `profiles` table. UUID string user IDs. Also owns the human-review tables `midterm_market_match_flags` (admin-flagged wrong markets, keyed by `(source, source_id, race_key)`) and `midterm_market_race_verifications` (admin-confirmed correct pairings, keyed by `race_key`), with `flag_market_as_wrong` / `unflag_market` / `get_flags_for_race` / `get_all_wrong_flags` / `verify_race` / `unverify_race` / `get_race_verification` / `get_all_verifications` upsert + lookup methods. |
| `district_profiles.py` | Hand-curated background context per state/district — demographics, economy, infrastructure, political history, geography. A background task in `main.py` keeps profiles fresh for every state with an active race. |
| `race_context.py` | Per-race context dictionary keyed by `{race_type}_{state}`: incumbents, likely candidates, state ballot measures, key issues, Cook/Sabato lean rating, narrative. Drives the "why is this market moving" view. |
| `historical_results.py` | Hand-curated dataset of recent federal/statewide election winners with vote totals and margins. Powers the `/data/historical` endpoint so users can compare current markets against historical baselines. |
| `test_race_key.py` | Standalone (no pytest) regression test for `market_race_key` — guards against the prior bug where unrelated "other/no-state" markets like "Bulgarian elections" and "LeBron James for president" collapsed into the same `other_US` bucket. Also covers the human-review DB round-trips: flag/unflag idempotency, verify/unverify upsert, and the divergence-calculator skip path. Run with `./venv/bin/python test_race_key.py`. |
| `requirements.txt` | Python deps (FastAPI, aiohttp, sqlmodel, etc.). Tiny — most logic is in stdlib + FastAPI. |
| `midterm_dashboard.db` | Main SQLite DB (~108MB, gitignored). Auto-created on first run. |

## Human-review of market matches

Even with the strict race-key matcher, the source aggregators occasionally
group the wrong Polymarket / Kalshi market into a race. The dashboard ships
an admin-only review loop:

1. An admin opens a race detail page and spots a source card that doesn't
   belong (e.g. a Polymarket "LeBron for president" market grouped under
   the Bulgarian parliamentary race).
2. They click **Wrong** on that source card. The frontend POSTs to
   `/admin/race/{race_key}/flag` with `{source, source_id, note?}`.
3. `Database.flag_market_as_wrong` upserts the row in
   `midterm_market_match_flags`.
4. On the next listing fetch, `data_races` and `divergence_calculator`
   skip the flagged pair, so it disappears from the dashboard everywhere.
5. The race detail endpoint `data_race_detail` keeps the flagged entry
   visible (with `flagged: true` and the admin's note) so the same admin
   can undo their own flag via the **Unflag** button.
6. Conversely, an admin can mark a known-good pairing as human-verified
   via `POST /admin/race/{key}/verify`. The frontend shows a "Verified"
   badge in the race header. This is informational only — verification
   doesn't change matching behaviour, it just records reviewer trust.

All admin endpoints sit behind `require_tier(request, "admin")`. Tests
live in `test_race_key.py`.

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `aggregators/` | Source connectors for prediction markets and polling | `aggregators/README.md` |
| `templates/` | Reserved for future server-rendered templates (currently empty). | — |
