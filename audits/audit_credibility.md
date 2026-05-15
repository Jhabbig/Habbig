# Adversarial Audit — `gateway/credibility/`

**Scope:** every file in `gateway/credibility/` (4 modules: `__init__.py`,
`calibration.py`, `timing.py`, `network.py`) plus the consumer paths that
drive scoring, persistence, and recomputation —
`gateway/queries/sources.py` (`recompute_all_credibilities`,
`upsert_source_credibility`, `compute_calibration`),
`gateway/jobs/ai_maintenance.py` (`recompute_calibration_scores`),
`gateway/jobs/compute_source_relationships.py`,
`gateway/jobs/pipeline_jobs.py` (`recompute_credibilities_job` cron
schedule), and `gateway/intelligence_routes.py` (`api_credibility_refresh`,
`api_get_credibility`, `api_get_calibration`).

**Focus vectors:**

1. Scoring fairness — bias, asymmetric clamps, divide-by-zero, MIN_SAMPLE
   thresholds, prior choice, treatment of missing data
2. Score-tampering surfaces — any path by which user input can move a
   source's credibility/calibration without going through the resolution
   pipeline
3. Snapshot consistency — what gets snapshotted, when, atomicity of
   writes, ability to reconstruct historical state, retention
4. Retro-active recomputation safety — idempotence, race conditions on
   concurrent triggers, transaction boundaries, fan-out side effects,
   cache invalidation

**Severity legend:**

- **CRITICAL** — score forgery / persistent corruption / unauthenticated
  rewrite of any credibility row.
- **HIGH** — exploitable from a limited attacker position (any
  authenticated user, any Pro user), or a guaranteed-bug invariant
  violation (concurrent write races, wrong-sign math, unbounded growth).
- **MEDIUM** — fairness bias or defence-in-depth weakness that needs
  preconditions but is realistic.
- **LOW** — code smell, hardening recommendation, doc/comment mismatch.
- **INFO** — observation, no action required.

**Hard rule observed:** this audit is read-only. Findings only — no
code edits in `gateway/credibility/` or any consumer module.
**Pre-release off-limits:** no fixes proposed against pre-release
branches; recommendations describe the change in plain text only.

---

## Cross-cutting findings

### HIGH-1 — Any Pro user can trigger a full-table recompute (event-loop blocking + cache fan-out + table-growth amplifier)

**Location:** `gateway/intelligence_routes.py:87-100` (`api_credibility_refresh`)
→ `gateway/queries/sources.py:226` (`recompute_all_credibilities`).

```python
async def api_credibility_refresh(request: Request):
    srv = _srv()
    user = srv._require_pro_user(request)
    if srv._is_rate_limited(f"cred_refresh:{user['user_id']}", limit=2, window=300):
        return JSONResponse({"error": "..."}, status_code=429, ...)
    count = db.recompute_all_credibilities()                 # ← sync, blocking
    log.info("User %s triggered credibility refresh, ...", user.get("username"))
    return JSONResponse({"recomputed": count, "timestamp": int(time.time())})
```

`recompute_all_credibilities` (`queries/sources.py:226-346`):
1. Reads **every resolved prediction across every source** (`db.py` row
   `WHERE resolved = 1 AND resolved_correct IS NOT NULL`).
2. For each source: writes `source_credibility`, writes one
   `credibility_snapshots` row, writes N rows in
   `source_category_credibility`, calls `compute_calibration` (another
   table scan + a write to `source_calibration`), and emits a realtime
   broadcast per source.
3. The route handler is `async def` but the function body
   `db.recompute_all_credibilities()` is **synchronous SQLite I/O** — the
   coroutine blocks the event loop for the full duration. At ~1 k
   sources × 50 resolved each that is multi-second on warm cache,
   tens of seconds on cold.

Compounding factors:

- **The rate limit is per-user, not global.** N Pro users × 2 refreshes
  per 5 min produces N×2 full recomputes per 5 min. With 50 Pro users
  that is 100 sequential full-table recomputes every 5 min — enough to
  starve the event loop, blow up the snapshot table, and fire 50 × N
  realtime broadcasts.
- **The recompute also fires the realtime fan-out** (line 336-342,
  `emit_credibility_update` per source). Each Pro-user refresh produces
  one broadcast per source per refresh. A motivated user can flood the
  WebSocket layer just by tapping a button.
- **TTL cache invalidation lives only in `recompute_calibration_scores`**
  (the 6-hour calibration job at `jobs/ai_maintenance.py:153`), not in
  `recompute_credibilities_job` or this Pro-user-triggered path. So a
  user-triggered refresh updates the DB but leaves stale TTL caches
  serving the *pre-refresh* numbers — the same user's "refresh now"
  click does not move their displayed score.
- **No global advisory lock.** With two concurrent triggers (cron + Pro
  user, or two Pro users), both run the same recompute against the same
  rows; the inner upserts are individual transactions (`db.conn()` is a
  short-lived context manager) so the table briefly observes
  interleaved writes from two passes. End-state converges, but
  in-flight readers can see one source's `last_computed_at` newer than
  another's mid-pass.

Mitigation outline (do not apply pre-release): (a) move
`db.recompute_all_credibilities()` into `loop.run_in_executor` or
enqueue via the scheduler so the route returns immediately; (b) add a
global rate limit (e.g. one refresh per hour shared across all Pro
users) on top of the per-user one; (c) hold a single named advisory
lock (`PRAGMA application_id` row, or a row in a `job_locks` table) for
the duration of any recompute, with cron + user triggers both honouring
it; (d) fire the TTL invalidation from `recompute_credibilities_job`
and from this route, not only from the calibration job.

### HIGH-2 — `credibility_snapshots` grows unboundedly; no pruning anywhere in repo

**Location:** `gateway/db.py:183-188` (table DDL) — no `DELETE` or prune
job exists.

```
grep -rn "credibility_snapshots" gateway --include="*.py"
  → only INSERT (queries/sources.py:87) + SELECT … LIMIT 5/10 (route +
    api_v1) + the migration / FTS files.
```

The recompute writes one row per source per run. Schedule = every 6 h
cron + every weekly source-relationships run + every Pro-user
on-demand. Steady state: 4 rows × |sources| per day.

At 1 k sources that is 4 k rows/day = ~1.5 M rows/year, far below
SQLite's limits but enough to (a) make the
`idx_cred_snap(source_handle, snapshot_at)` index dominate the page
cache, (b) make `get_credibility_snapshots(... LIMIT 5)` slower than it
needs to be because the index leaves are spread across many leaf
pages, and (c) interact with HIGH-1 amplifier: a single Pro-user
campaign of refreshes can pump arbitrary rows in. There is **no
duplicate suppression** — back-to-back identical recomputes write
duplicate rows with different `snapshot_at`.

Mitigation outline: retention job that prunes `credibility_snapshots`
older than N days (suggest 365), plus a per-source row cap (e.g. keep
the last 200 per source), plus a no-op skip when the new
`global_credibility` matches the most recent snapshot's value to within
1e-6 (de-duplicates back-to-back triggers).

### HIGH-3 — Concurrent recompute paths share the same write set with no lock; snapshot ordering is not guaranteed

**Locations:**
- Path A: `jobs/pipeline_jobs.py:192-204` `recompute_credibilities_job`
  fired by APScheduler (4× daily).
- Path B: `intelligence_routes.py:98` on-demand by any Pro user.
- Path C: `jobs/ai_maintenance.py:79-168`
  `recompute_calibration_scores` (4× daily on the same cron pattern,
  different minute) — only touches calibration columns on `sources`,
  but reads `source_prediction_records`.
- Path D: `jobs/compute_source_relationships.py:58` — weekly, only
  writes `source_relationships` + `source_networks`.

A and B both call the **same** `recompute_all_credibilities` against
**the same** `source_credibility` / `credibility_snapshots` /
`source_category_credibility` tables. APScheduler's `max_instances=1`
(scheduler/scheduler.py:125) only protects A from A. A vs B is
unprotected — Pro user's coroutine and the scheduler thread can be
in-flight at the same time. SQLite serialises individual writes
(WAL or rollback journal), but the per-source loop body issues several
independent transactions (one per upsert helper call) so an
interleaving leaves rows whose `last_computed_at` come from different
passes.

Worse, the upsert for `credibility_snapshots` is **inside the same
helper** that writes `source_credibility` (queries/sources.py:67-89)
under a single `with db.conn() as c:` block — but there is no explicit
`BEGIN`, so it relies on the connection-context manager's implicit
transaction. If the two passes happen to commit interleaved, you can
end up with `source_credibility.global_credibility = X` from pass A
but the latest `credibility_snapshots` row holding `Y` from pass B —
a quiet inconsistency between the live row and its own history.

Mitigation outline: wrap the per-source upsert + snapshot insert in a
single `BEGIN IMMEDIATE` transaction, and gate any
`recompute_all_credibilities` call with a process-wide named lock (or
forbid the Pro-user route from calling it directly — let it enqueue
a job and poll).

### HIGH-4 — Calibration recompute (`recompute_calibration_scores`) silently no-ops when the `sources` table is named differently across branches, and never falls back

**Location:** `gateway/jobs/ai_maintenance.py:114-148`.

```python
source_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
calib_col = "calibration_score" if "calibration_score" in source_cols else None
...
if calib_col is None:
    continue                                   # ← per-source silent skip
```

If migration 053 hasn't run (column missing), the job loops through
**every** scoreable source and skips each one, **but the job itself
returns success** (`{"sources_examined": N, "calibrated": 0, "unlocked": 0}`).
There is no alert, no log warning that the calibration columns are
missing. Operators looking at the admin/jobs dashboard see green ticks.

Also: when `result is None` (sample size below MIN_SAMPLE=10) the code
**clears** `calibration_score = NULL`, `calibration_sample_size = 0`,
`calibration_unlocked = 0` — there is **no path that preserves** a
previously-unlocked source whose record count temporarily drops below
threshold (e.g. a resolved-prediction row is hidden by an admin
moderation action — see HIGH-7). This means moderator-driven hiding
of records can deterministically lock a previously-unlocked source.

Mitigation outline: log + alert if the calibration columns are missing
at job start (single warning per run), and only clear `calibration_*`
fields when the source has zero usable records, not when count drops
below MIN_SAMPLE.

### HIGH-5 — `recompute_calibration_scores` uses `sources` table; `recompute_credibilities_job` uses `source_credibility` table; they disagree about what "calibration" means for the source

**Location:** two separate calibration systems coexist:
- `gateway/credibility/calibration.py` (Brier-based; consumed by
  `jobs/ai_maintenance.py:recompute_calibration_scores`; writes
  `sources.calibration_score` etc.).
- `gateway/queries/sources.py:141-205` (`compute_calibration`; bucket-
  deviation-based; called from `recompute_all_credibilities`; writes
  `source_calibration.calibration_score`).

These two implementations:
1. Use **different formulas** (Brier-normalised-against-0.25 vs
   mean-absolute-deviation between bucket predicted-avg and actual).
2. Run **on different schedules** (00:25/06:25/12:25/18:25 vs
   00:15/06:15/12:15/18:15).
3. Read **different source columns** (`predicted_probability_stated`
   from `source_prediction_records` vs `predicted_probability` from
   `predictions`).
4. Both apparently surface on the user-facing source detail page —
   `api_get_calibration` (`intelligence_routes.py:67`) reads
   `db.get_source_calibration` which is the second one
   (`source_calibration` table), so the Brier-based
   `sources.calibration_score` written by `recompute_calibration_scores`
   appears to be a dead write. (Confirmed by grepping — no production
   reader of `sources.calibration_score` / `calibration_unlocked`.)

The brittleness here is twofold: (a) operators reading the cron
catalogue would assume the 25-past-the-hour job is what powers the
calibration badge, but it isn't; (b) any future code that wires the
Brier number through to the UI will quietly diverge from the existing
deviation number. This is a maintenance trap, not a live bug, but the
deviation calibration uses a far weaker `len(preds) < 5` threshold vs
the Brier code's `MIN_SAMPLE = 10`, so the visible badge is unlocked
at a lower bar than the dead one would allow.

Mitigation outline: pick one calibration definition and delete the
other path, or rename one to make the distinction explicit (e.g.
`calibration_brier` vs `calibration_deviation`) and wire one of them
through to a real reader.

### HIGH-6 — `network_adjusted_consensus.cluster_cap_weight` is caller-controlled with no validation; a malicious caller can flip consensus direction by passing 0

**Location:** `gateway/credibility/network.py:148-227`.

```python
def network_adjusted_consensus(
    predictions: list[dict],
    clusters: list[list[str]],
    *,
    cluster_cap_weight: float = 1.0,
) -> dict:
    ...
    scale = min(1.0, cluster_cap_weight / max(total, 1e-9))
    yes_weight += bucket["yes"] * scale
    no_weight  += bucket["no"]  * scale
```

The function clamps `scale` with `min(1.0, …)` but **does not clamp
negatives, NaN, or values < 0**. Pass `cluster_cap_weight=0` → every
clustered bucket contributes nothing → consensus is driven entirely by
ungrouped sources (which may be a small minority deliberately spinning
the result). Pass `cluster_cap_weight=-1` → negative weights subtract
from `yes_weight` / `no_weight` and yield a `consensus_yes` outside
[0, 1] or a divide-by-zero-via-negative-total when the totals cancel.

Today no caller passes anything but the default — but the function is
exposed at the module top-level (`credibility/__init__.py:24`) and the
docstring suggests it's a reusable analysis primitive. A future
admin/debug route or backtest knob that surfaces this parameter to a
request body is one line away from a tampering surface.

Mitigation outline: clamp `cluster_cap_weight` to `[0.0, 100.0]` at
function entry; treat NaN / inf as the default 1.0.

### HIGH-7 — Bayesian smoothing strength is fixed at 10 globally; new sources with adversarial reverse-streak content can hold artificial scores indefinitely

**Location:** `gateway/queries/sources.py:242-302`.

```python
STRENGTH = 10
...
global_cred = (total * dwa + STRENGTH * PRIOR) / (total + STRENGTH)
unlocked = total >= MIN_FOR_UNLOCK     # 10
```

The strength constant equals the unlock threshold, so a source crosses
the "unlocked" badge at the same moment Bayesian smoothing weight is
50/50 between observed and prior. The first 10 predictions therefore
have outsized leverage on a source's permanent rank — a source that
goes 10/10 on **trivially-easy markets** (huge favorites resolving at
the obvious side, e.g. "will the sun rise tomorrow"-type markets that
slip through) gets unlocked with `dwa ≈ 1.0` and `global_cred ≈ 0.75`,
and decay only pulls them down slowly as misses accumulate. There is
no minimum-difficulty filter on `predictions` (no edge-at-entry check,
no contrarian filter) before they count toward unlock.

This is a fairness flaw in the unlock criterion, not a bug. The
calibration system would catch the bias separately, but
(per HIGH-5) the calibration the UI actually shows uses MIN_SAMPLE=5
and a softer deviation formula, so the same trivially-easy markets
also produce great-looking calibration.

Mitigation outline: weight unlock by either (a) market-implied edge at
prediction time (favour predictions where the market was uncertain) or
(b) category diversity — require unlock from at least two distinct
categories.

### MEDIUM-1 — `compute_timing_score` is exported and unit-tested, but no production caller writes its output

**Location:** `gateway/credibility/timing.py:30-91` exported via
`__init__.py:20`.

```
grep -rn "compute_timing_score" gateway --include="*.py"
  → only the unit test (test_intelligence_layer.py).
  → migration 053 added timing_score columns but nothing populates them.
```

The migration created the `timing_score` and `edge_at_prediction`
columns on `source_prediction_records` and `avg_timing_score` /
`early_predictor_rank` on `sources`. Recompute paths never compute or
store them. Reliability: zero rows reach the source detail page's
"Timing profile" section that the module docstring promises. This is a
**fairness drift risk** — the credibility ranking docs say timing
counts, but in production it does not, so users who specifically
optimise for early/contrarian behaviour receive nothing for it.

Mitigation outline: either delete the module + columns + tests, or
wire it into the resolution pipeline that flips
`predictions.resolved_correct`.

### MEDIUM-2 — `_valid_record` accepts `True`/`False` for `resolved_correct` but `recompute_all_credibilities` never converts; the per-record Brier component handles bool, the bucket fast-path does not

**Location:** `gateway/credibility/calibration.py:42-49` and
`queries/sources.py:281-284`.

`calibration._valid_record` allows
`outcome in (0, 1, True, False)` — explicit. Inside `compute_brier_score`
the conversion happens via `1.0 if _get(rec, "resolved_correct") else 0.0`,
which handles bools correctly.

But `recompute_all_credibilities` does `is_correct = 1 if p["resolved_correct"] else 0`
(line 284) — same truthiness check. **However**, SQLite returns INTEGER
0/1, never Python `True/False`, so the input contract differs from the
pure-function module. If anyone calls `compute_brier_score` directly
from a route handler with parsed-JSON input (where booleans survive
deserialisation as `True`/`False`), the module computes correctly but
the integrity-check column `resolved_correct` in the DB is mixed-typed
across the two writers. Downstream `SELECT … WHERE resolved_correct = 1`
ignores the boolean variant. Today only the recompute job writes, but
the implicit contract is fragile.

Mitigation outline: in `compute_brier_score`, coerce `outcome` to int
at validation time, and add an integration test that sets
`resolved_correct = True` from a Python-API path.

### MEDIUM-3 — Reliability diagram empty buckets are coerced to `actual_accuracy=0` instead of `None`; chart will look like sources predict 0 of the time at every gap

**Location:** `gateway/credibility/calibration.py:144-161`.

```python
else:
    predicted_avg = (b["bin_lo"] + b["bin_hi"]) / 2.0
    actual = 0.0                                # ← misleading for empty bins
```

Empty buckets are emitted "for chart continuity" per the docstring,
but `actual_accuracy = 0.0` will draw a line at the x-axis through the
gap. `is_overconfident = bool(b["count"]) and delta > 0.10` is False
when the bucket is empty, so flags don't fire — good — but the visible
chart shape is wrong. A source with 0 predictions in the 0.4-0.5 band
will appear to have called those probabilities 100% wrong.

Mitigation outline: emit `actual_accuracy = None` for empty buckets so
the frontend can render gaps; or interpolate; or drop empty buckets
entirely and let the chart connect adjacent points.

### MEDIUM-4 — `pairwise_stats.both_correct_rate` defaults to `0.0` (not `None`) when no records have both resolutions; classifies as "neutral" or "opposing" instead of "indeterminate"

**Location:** `gateway/credibility/network.py:75-78`.

```python
both_correct_rate = (
    both_correct_count / resolved_both_count
    if resolved_both_count else 0.0
)
```

`resolved_both_count == 0` happens when the two sources agreed on N≥5
markets but none of those markets has been resolved yet for both
sides. Setting `both_correct_rate = 0.0` and then asking
`classify_relationship` to map it can produce `echo_chamber` (when
agreement is high) even though *we have no idea whether their
agreement is predictive* — by definition no resolutions exist.

Mitigation outline: return `both_correct_rate = None` and have
`classify_relationship` return `"neutral"` (the not-stored bucket) when
the field is `None`.

### MEDIUM-5 — `_days_remaining` clamps at 0 but does not warn on backwards timestamps; clock-skew or AI-generated extraction with future-dated `predicted_at` yields silent score deflation

**Location:** `gateway/credibility/timing.py:129-134`.

```python
def _days_remaining(predicted_at, market_close_time):
    a = _to_unix(predicted_at); b = _to_unix(market_close_time)
    if a is None or b is None: return None
    return max(0.0, (b - a) / 86400.0)        # ← silent floor
```

If `predicted_at > market_close_time` (extractor mis-dated, or the
source predicted *after* close, which is the prevention target),
`days_remaining = 0`, `time_component = 0`, and the timing score
collapses to `(0 + contrarian) * outcome / 2`. No data-quality signal
ever reaches operators that the extractor has produced a
post-close prediction record — but the credibility math silently
penalises that source on every such row.

Conversely, a manufactured `predicted_at` set arbitrarily far in the
past produces `min(days_remaining / 30, 1.0) = 1.0` for free — i.e. an
attacker who can influence the extractor's date field can inflate
every prediction's `time_component`.

Mitigation outline: distinguish "no data" (return None →
`time_component = 0.5` neutral) from "backwards data" (log + treat as
`time_component = 0.5` + flag the row for moderation review).

### MEDIUM-6 — `categories_active` is computed from `predictions.category` with no normalisation; "Politics" / "politics" / "POLITICS" all count as distinct categories

**Location:** `gateway/queries/sources.py:290-292`.

```python
cat = p["category"] or "other"
if cat not in cat_data:
    cat_data[cat] = {...}
```

`p["category"]` is the raw string from `predictions`. If the extractor
or admin re-categorisation produces mixed case (or extra whitespace),
`categories_active` over-counts, and per-category credibility splits
across near-duplicate rows. This is mostly a display issue today but
will bite anyone who tries to query `source_category_credibility`
with a canonical lower-case slug.

Mitigation outline: normalise `(p["category"] or "other").strip().lower()`
at recompute time; add a CHECK constraint on the column, or a trigger,
to enforce going forward.

### MEDIUM-7 — Realtime `emit_credibility_update` fan-out is unbounded and best-effort silent

**Location:** `gateway/queries/sources.py:333-342`.

```python
try:
    from realtime.broadcast import emit_credibility_update
    emit_credibility_update(source_handle=handle, global_credibility=...)
except Exception:
    pass
```

Inside the per-source loop, one broadcast per source per recompute.
Combined with HIGH-1 (Pro user can trigger), a single button-tap can
generate one broadcast per source. The `except: pass` swallows even
broken-broadcast errors entirely (no log line) so the operator cannot
see when the hub is rejecting writes.

Mitigation outline: batch into a single broadcast (one event holding
the per-source delta map), or rate-limit broadcasts to e.g. one per
source per 60 s; log at `warning` level when the import or emit fails.

### LOW-1 — `__init__.py` re-exports `brier_component_for_record` which has zero production call sites

`brier_component_for_record(p, o)` is documented as a way for the
pipeline to "store a pre-computed `calibration_contribution` per
record" but the recompute path re-scans history every time and never
calls it. Dead surface area.

### LOW-2 — `reliability_diagram_data(bins=10)` rounds bucket edges to 4 decimals but doesn't dedupe; binsize 7 produces non-distinct edges via floating-point round

For odd bin counts (`bins=7`) the rounded edges happen to be distinct
today, but the contract that `bin_lo` / `bin_hi` are unique is not
enforced. Frontend that joins on those values can lose a bucket.

### LOW-3 — `echo_chamber_clusters` exposes singletons through the union-find map even though it filters them at the return

The `parent` dict is built for every node touched (line 119-138), so
`echo_chamber_clusters([])` allocates nothing but
`echo_chamber_clusters([{...echo_chamber...}, ...])` retains every
node that ever appeared on any echo edge in `parent`. Memory is freed
on function return — but anyone who logs the result with `pprint`
would benefit from the function actually deleting singleton parents.
Cosmetic.

### LOW-4 — `network_adjusted_consensus` divides by `1e-9` instead of returning early when a cluster's total weight is exactly 0

`scale = min(1.0, cluster_cap_weight / max(total, 1e-9))` (line 214).
The guard avoids divide-by-zero but happens after the `if total <= 0:
continue` (line 211), which already exits. So the `1e-9` is dead
defensive code. Harmless.

### LOW-5 — `compute_brier_score` rounds the output Brier to 6 decimals and the calibration to 6 decimals but does not document that the displayed precision is intentional

Anyone diffing scores across recompute runs will see "differences" at
the 7th decimal vanish without explanation.

### LOW-6 — `_valid_record` accepts `outcome in (0, 1, True, False)` but rejects `outcome == "1"` (string from a JSON column with a TEXT type)

If a downstream caller ever stores `resolved_correct` as TEXT (legacy
import, CSV round-trip), every record silently drops out of
calibration. Worth coercing.

### LOW-7 — `recompute_all_credibilities` uses `math.exp(-LAMBDA * age_days)` without clamping `age_days` at a maximum

For very old predictions, `age_days` could be 10 000+, producing
`decay ≈ 0`. Sum-of-weights still works, but a single very-old
prediction with `decay ≈ 1e-43` and `is_correct = 0` is silently
treated as if the prediction did not exist for DWA purposes while
still counting toward `total_predictions`. The Bayesian smoothing then
penalises the source for the "n" but not the observation.

### INFO-1 — `MIN_SAMPLE = 10` (Brier) vs `len(preds) < 5` (deviation) vs `MIN_FOR_UNLOCK = 10` (credibility) vs `MIN_SHARED_MARKETS = 5` (network) — four different thresholds, no central constants module

Operator-tunable thresholds are scattered across four files
(`calibration.py`, `queries/sources.py`, `network.py`,
plus `db_takes.DEFAULT_AUTHOR_CRED = 0.5`). No `credibility/constants.py`
or env-tunable knobs.

### INFO-2 — `__init__.py` docstring claims "no DB connections are opened in the compute path" — true for the modules themselves, but the public consumer (`queries/sources.recompute_all_credibilities`) opens **N transactions per source** through the helper upserts

Compute is pure; persistence is not. Worth a short sentence in the
`__init__.py` docstring noting that.

---

## Score-tampering surfaces — exhaustive map

**Question:** by what paths can a non-admin user move a credibility
number?

1. **Direct write via REST** — none. All `INSERT`/`UPDATE` against
   `source_credibility`, `source_category_credibility`,
   `credibility_snapshots`, `source_calibration` happen inside
   `gateway/queries/sources.py` only. No route handler executes these
   SQLs. (Verified: `grep -rn "UPDATE source_credibility\|INSERT INTO
   source_credibility\|... source_category_credibility ..."` returns
   only `queries/sources.py` and test fixtures.)
2. **Triggering recompute** — yes, any Pro user via
   `POST /api/credibility/refresh` (HIGH-1). The user does **not pick the
   inputs** — the recompute reads from `predictions` — but the user
   does pick **the timing** of the write, which interacts with HIGH-3
   to produce inconsistent observable state, and with HIGH-2 to grow
   the snapshot table.
3. **Influencing recompute inputs** — yes, indirectly. Anyone who can
   write to `predictions.resolved_correct` shifts the score. The DB
   has no direct route surface to set `resolved_correct` from
   user-controlled input, but moderation actions and resolution-batch
   jobs do; out of scope here (covered in audit_admin_jobs_routes.md
   and audit_state_reconciliation_drift.md).
4. **Tampering with intermediate columns** — `predicted_probability`,
   `category`, `direction` on the `predictions` table feed into both
   credibility recompute and calibration. Same out-of-scope mod-write
   surfaces as above.
5. **Tampering with author/take credibility** — `db_takes.get_blended_credibility`
   produces a per-user nudge (0.85 * global + 0.15 * take_accuracy).
   Take accuracy is computed from `market_takes.resolved_correct` —
   another moderation surface, out of scope here. Worth noting that
   blended user credibility flows into the take/quality-score, not
   into `source_credibility`, so this audit's surface is unaffected.

**Net finding:** the only direct tampering path within scope is
HIGH-1 (Pro user triggers recompute timing), and the indirect path is
the moderation surface to `predictions` — separately audited.

---

## Snapshot consistency — exhaustive map

1. **What is snapshotted?** Only `global_credibility` (single REAL) per
   source per recompute. The snapshot does **not** capture
   `decay_weighted_accuracy`, `total_predictions`, `correct_predictions`,
   or per-category breakdowns.
2. **Is the snapshot atomic with the live row?** Yes, inside the same
   `with db.conn() as c:` context — but across concurrent recompute
   passes (HIGH-3) the order of commits is not enforced.
3. **Can the snapshot be reconstructed?** Only crudely. With just
   `global_credibility` + timestamp, you cannot reproduce the formula
   inputs or audit-trace how a score moved. Combined with HIGH-2
   (unbounded growth, no pruning), this is paradoxical: we keep too
   much data (every duplicate identical recompute) but too thin (none
   of the contributing breakdowns).
4. **Are snapshots written under transaction guarantees?** The CREATE
   statement does not declare `STRICT` or any uniqueness; duplicate
   rows for the same `(source_handle, snapshot_at)` are allowed if two
   recompute paths happen to hit the same second.
5. **Are snapshots cleaned up when a source is deleted?** No FK / no
   cascade. Deleting from `source_credibility` (which has UNIQUE on
   `source_handle`) leaves orphaned snapshot rows for the same
   handle. Future API readers will surface a `null` live row + N
   historical snapshots.

---

## Retro-active recomputation safety — exhaustive map

1. **Idempotence:** verified by `test_credibility_recompute.py::test_idempotent`
   — running twice in a row produces the same numeric result up to 6
   decimal places. Good.
2. **Race safety across concurrent triggers:** broken (HIGH-3). End
   state converges but mid-state is observable.
3. **Effect of column changes:** the calibration job (HIGH-4) silently
   skips when columns are missing; the credibility recompute does
   not check for column existence and would crash on a schema that
   lacks `predictions.resolved_correct` (relied on at line 260).
   Acceptable — that column is core. But the calibration column check
   is asymmetric and silent.
4. **Effect of historical row deletion:** if a moderator soft-deletes
   resolved predictions (sets `resolved = 0` or hides them), the next
   recompute drops them from `total_predictions`, `correct_predictions`,
   and decay weights — the source's credibility moves. There is **no
   snapshot of the predictions that contributed** to any given
   credibility value. Combined with HIGH-2's lack of pruning, you get
   a long timeline of credibility values but no way to explain any
   given delta.
5. **Effect of clock skew or backfilled `resolved_at`:** `age_days`
   is `max(0, (now - resolved_at) / 86400)`. A backfilled `resolved_at`
   in the future yields `age_days = 0` and `decay = 1.0` — a "stale"
   prediction backfilled today behaves like it just resolved. No
   detection.
6. **TTL cache fan-out:** only `recompute_calibration_scores` calls
   `ttl_invalidate.on_credibility_recompute` (HIGH-1 compounding
   factor). The credibility recompute itself does not, so cached
   `/sources/{handle}` reads served by the gateway after a
   credibility-only recompute remain stale until the next calibration
   tick (could be up to 6 h later if the credibility cron fires
   between the calibration ticks).

---

## Bottom-line — severity counts

- CRITICAL: **0**
- HIGH: **7**
- MEDIUM: **7**
- LOW: **7**
- INFO: **2**

## Top 3

1. **HIGH-1 — Pro-user `POST /api/credibility/refresh` blocks the
   event loop with a full-table sync recompute, with per-user
   (not global) rate limiting and unbounded realtime fan-out.**
2. **HIGH-3 — Concurrent recompute paths (cron + Pro-user + weekly
   relationships) share the same write set with no advisory lock;
   `source_credibility` row can be observed at a different pass than
   its own latest `credibility_snapshots` row.**
3. **HIGH-2 — `credibility_snapshots` has no retention, no
   duplicate-suppression, and no per-source cap; in steady state it
   grows linearly with `|sources| × recomputes/day`, and HIGH-1 lets
   any Pro user pump it.**
