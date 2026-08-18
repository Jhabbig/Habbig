# narve Terminal — testing & the cofounder demo script

Everything below is **reproducible on any machine with the repo** — nothing is
canned. Every number regenerates from deterministic code (fixed seeds, anchored
timestamps), so the demo *is* the test suite.

Requirements: `brew install python@3.11 node` · then
`python3.11 -m pip install --user fastapi uvicorn pydantic python-multipart httpx pytest`

Start the app (two terminals, from `terminal/`):

```bash
cd sidecar && python3.11 -m narve_sidecar.server          # engine :41733
```
```bash
cd app && npm install && npm run dev                       # UI on :5173
```

---

## The 5-minute live demo (in the UI)

1. **INGEST → LOAD SAMPLE DATA.** 6 sources (5 models + 1 human texter),
   14 questions, 15 predictions. Point at: the counts line updating, the raw
   peek table filling.
2. **MARKETS.** The tape: |edge|-sorted, every number monospace, SAMPLE tags.
   Point at: House majority 0.635 vs 0.550 market — the human texter's 0.66
   stance is *in* that blend, weighted by their 0.5 credibility.
3. **SOURCES.** The leaderboard. Point at: KIND chips (MODEL vs USER),
   `capitol_staffer` at 0.500 — people start at the neutral Beta(2,2) prior and
   *earn* their way up. Race Model at 0.8125 with 1 resolved call — click the
   row for its credibility-event history.
4. **Any question → RESOLVE.** The whole product in one click: pick Nevada,
   RESOLVE NO → the CREDIBILITY MOVES table appears inline
   (state_polls 0.706 → 0.722, HIT) and the leaderboard reorders. Say it out
   loud: *resolution feeds credibility — that loop is narve*.
5. **INGEST a text message.** Drop a CSV row like
   `x:someone,"hearing it passes",2026-08-18T10:00:00Z,some-question,0.78`
   into MESSAGES → the person is auto-created as a USER source and their
   stance becomes a scored prediction. Resolve → their credibility moves.

## The automated suites (run live in front of him)

```bash
cd terminal/sidecar && python3.11 -m pytest -q     # 48 passed
```
```bash
cd terminal/why && python3.11 -m pytest -q         # 127 passed
```
```bash
cd terminal/app && npx tsc --noEmit && npm run build
```

What they pin: Beta(2,2) hand math, the resolve→credibility loop, blend
Σ(p·cred)/Σcred, idempotent re-uploads (sha256 + natural keys),
all-row-errors-at-once validation, byte-identical determinism, banned-phrase
guards, the 30¢/60% → +$0.30 EV worked example.

## The 25k-tweet stress test (one command each)

```bash
cd terminal
python3.11 tools/gen_tweets.py .        # writes tweets_25k.csv (seed 42 — identical every run)
```

Then with the sidecar on a scratch DB (`NARVE_DB_PATH=/tmp/stress.db`):

```bash
curl -X POST -F "file=@tweets_25k.csv" http://127.0.0.1:41733/ingest/messages
curl -X POST -F "file=@resolutions_2025.csv" http://127.0.0.1:41733/ingest/resolutions
```

Measured on a MacBook (2026-08-18): **25,000 tweets ingested in 0.44s**
(~57k rows/s incl. validation), re-upload dedup short-circuit 0.08s,
mass-resolve of 631 tweeting accounts 0.067s, `/sources` with 1,206 sources
86ms, the UI leaderboard renders all 1,206 rows, DB 8.1MB.

The planted-skill experiment: 30 accounts get real skill (0.78–0.92), 1,170
are noise. After resolution the cohorts separate — **skilled mean credibility
0.648 / Brier 0.163 vs noise 0.499 / 0.326**. Leaderboard ties (integer
records tie exactly) break by Brier so sharp accounts outrank lucky ones
within a record. Known and intentional: hit-record still dominates across
records at low n — the Brier-weighted-credibility upgrade is specced for v1
and this test is its evidence.

*(generator: `terminal/tools/gen_tweets.py` — deterministic, seed 42; it also
writes resolutions_2025.csv for the resolve step.)*

## The why engine (Julian's integration seam)

```bash
cd terminal/why
python3.11 -m narve_why explain --in fixtures/house_majority.json --format text
```

Ten-second card: probability + market gap on line 1, arrowed drivers, named
dissent (race_model 0.64 vs macro_model 0.34, 30 pts), watch-list, honest
confidence. Add `--db ~/.narve-terminal/terminal.db` and the credibility table
is pulled live from the terminal's engine — fixture state and DB state match
number-for-number (that cross-check is a test).

Break it on purpose (the error-message contract):

```bash
echo '{"question_id":"x","question_text":"x","probability":1.4,"as_of":"2026-08-18T00:00:00Z","model_outputs":[{"model_id":"m","source_id":"m","p":0.5,"weight":1,"inputs_ref":["r:1"]}],"market_snapshots":[]}' > /tmp/bad.json
python3.11 -m narve_why explain --in /tmp/bad.json    # exit 2: "probability: must be within [0, 1] (got 1.4)"
```

The full interface between the two halves is one JSON contract:
repo-root `CONTRACTS.md` v1.0.0 — sign-off line waiting.
