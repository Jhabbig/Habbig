# Adversarial Audit — `gateway/portfolio/kelly.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M ctx)
Primary target: `/Users/shocakarel/Habbig/gateway/portfolio/kelly.py` (142 LOC)
Supporting layers reviewed:
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py` (callers of `kelly.*`)
- `/Users/shocakarel/Habbig/gateway/queries/markets.py` (the *other* `get_user_bankroll` / `set_user_bankroll` on the canonical `bankroll` column)
- `/Users/shocakarel/Habbig/gateway/market_routes.py` (`/api/market/bankroll`, the canonical user-facing bankroll setter)
- `/Users/shocakarel/Habbig/gateway/db.py` (sqlite `conn()` context manager — WAL, 5s busy_timeout, autocommit-on-exit)
- `/Users/shocakarel/Habbig/gateway/migrations/017_user_bankroll.py` (adds `users.bankroll` + `users.kelly_fraction`)
- `/Users/shocakarel/Habbig/gateway/migrations/062_portfolio_integration.py` (adds `users.bankroll_usd`)
- `/Users/shocakarel/Habbig/gateway/migrations/162_integrity_cleanup.py` (backfills `kelly_fraction` NULLs)
- `/Users/shocakarel/Habbig/gateway/tests/test_kelly.py`
- `/Users/shocakarel/Habbig/gateway/tests/test_portfolio_integration.py`

Scope (per brief):
1. Bankroll race conditions
2. Fractional-Kelly clamping (must be `0 <= f <= 1`)
3. Division-by-zero on zero-edge inputs
4. NaN / Inf propagation

Pre-release pages confirmed untouched.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High     | 3 |
| Medium   | 3 |
| Low      | 3 |
| Info     | 2 |
| **Total**| **12** |

---

## Top 3 findings (ranked by exploitability x impact)

1. **CRIT-1** — Schema split-brain: `portfolio/kelly.py` reads and writes
   `users.bankroll_usd`, while the rest of the codebase (`queries/markets.py`,
   `market_routes.py`, all tests in `test_portfolio_integration.py`, all
   server.py dashboard renders) reads and writes `users.bankroll`. Both
   columns exist on `users` (migrations 017 and 062 both ran). Saving a
   bankroll on `/api/kelly/bankroll` writes `bankroll_usd`, while the user
   loading their own dashboard via `db.get_user_bankroll` reads `bankroll`
   and gets the stale value (or `0.0`). The Kelly endpoint silently uses a
   different bankroll than the user thinks they configured.
   (`kelly.py:120-141` vs `queries/markets.py:424-455`.)

2. **HIGH-1** — `sizing_table()` does not filter NaN or +Inf for `bankroll_usd`.
   `NaN <= 0` is `False` in Python, so a NaN bankroll bypasses the zero
   branch at `kelly.py:72` and poisons every numeric field (`stake_usd`,
   `max_profit_usd`, `max_loss_usd`, `bankroll_usd`). `+Inf <= 0` is also
   `False`, so an `inf` bankroll produces `inf` stakes. The result is
   serialised with `json.dumps(..., allow_nan=True)` (FastAPI default), so
   the response contains literal `NaN` / `Infinity` tokens — which standard
   `JSON.parse` rejects, breaking the dashboard JS. Reachable via
   `/api/kelly/calculate` with `bankroll_usd: "NaN"` (route does
   `float(...)` on user input at `routes.py:164`, which accepts `"nan"` /
   `"inf"`). (`kelly.py:59-108`, `routes.py:144-174`.)

3. **HIGH-2** — The user's *stored* `kelly_fraction` preference (the
   `0 < f <= 1` clamped on input at `market_routes.py:1077`) is **never
   applied** by `portfolio/kelly.sizing_table()`. The function hard-codes
   `full / half / quarter` rows regardless of what the user picked. So the
   "fractional-Kelly clamping" the brief asks about does exist at the
   *write* boundary (`market_routes.py:1077`), but the *read* path used by
   `/api/kelly/calculate` does not honour the stored preference, which
   means the clamp is enforced for a value the calculator ignores.

---

## Findings

### CRIT-1 — Bankroll schema split-brain: two columns, two writers, no coherence

**Location:** `kelly.py:120-141` (reads/writes `users.bankroll_usd`) vs.
`queries/markets.py:424-455` (reads/writes `users.bankroll`).

**What:**
- Migration `017_user_bankroll.py:13-14` adds `users.bankroll REAL`.
- Migration `062_portfolio_integration.py:119-121` adds
  `users.bankroll_usd REAL NOT NULL DEFAULT 0`.
- Both columns live on `users` simultaneously. Confirmed via grep of
  migrations directory — no migration ever drops either, and migration
  062's comment ("Leave `bankroll_usd` in place — additive column on
  users") shows the second column was deliberately left as a parallel.

Writes:
- `kelly.set_user_bankroll(user_id, bankroll_usd)` -> `UPDATE users SET bankroll_usd = ?`
- `queries.markets.set_user_bankroll(user_id, bankroll, kelly_fraction)` -> `UPDATE users SET bankroll = ?, kelly_fraction = ?`

Reads:
- `kelly.get_user_bankroll(user_id)` -> `SELECT bankroll_usd FROM users` -> returns a `float`.
- `queries.markets.get_user_bankroll(user_id)` -> `SELECT bankroll, kelly_fraction FROM users` -> returns a `dict`.

Callers:
- `POST /api/kelly/bankroll` (`portfolio/routes.py:178-196`) -> `kelly.set_user_bankroll` -> writes `bankroll_usd`.
- `POST /api/kelly/calculate` (`portfolio/routes.py:144-174`) -> `kelly.get_user_bankroll` -> reads `bankroll_usd`.
- `POST /api/market/bankroll` (`market_routes.py:1051-1084`) -> `db.set_user_bankroll` -> writes `bankroll`.
- `GET /api/market/bankroll`  (`market_routes.py:1048`) -> `db.get_user_bankroll` -> reads `bankroll`.
- `server.py:7136, 7154` (settings + dashboard renderer) -> `db.get_user_bankroll` -> reads `bankroll`.
- `tests/test_portfolio_integration.py:214-221, 351, 361` -> uses the `dict` API (`bankroll` column) exclusively.

**Concrete impact:**
- User opens the trading-addon settings page (rendered from `bankroll`),
  changes the value, saves via `POST /api/market/bankroll`. This writes
  to `bankroll` only.
- User then opens the Kelly calculator on the portfolio dashboard. The
  calculator calls `/api/kelly/calculate` with no `bankroll_usd` body
  field. The route falls through to `kelly.get_user_bankroll`, which
  reads `bankroll_usd` — which was never updated by the settings save and
  still holds the default `0.0`.
- Recommendation shown to the user: all zeros, with a "Set a bankroll in
  settings" note. The user has just set a bankroll in settings.
- Conversely, a user who hits the Kelly bankroll endpoint directly
  (e.g. via the API docs or a power-user script) updates `bankroll_usd`
  but the dashboard, settings page, and every test continues to display
  the value of `bankroll`.

**Severity:** Critical — silent data loss on the user's stated input,
direct effect on bet-sizing recommendations (the load-bearing output of
the entire trading add-on). User cannot tell which value the system is
using; both endpoints look successful.

**Fix:**
- Pick one column. The rest of the codebase uses `bankroll`. Easiest fix:
  delete `kelly.get_user_bankroll` and `kelly.set_user_bankroll` and have
  `portfolio/routes.py` call `db.get_user_bankroll` and
  `db.set_user_bankroll` directly (with the `dict` shape — `routes.py`
  already uses `bankroll_override` only for the override path, so the
  default branch just needs `db.get_user_bankroll(uid)["bankroll"]`).
- Write a follow-up migration that copies any non-zero `bankroll_usd`
  values into `bankroll` for users where `bankroll IS NULL OR bankroll = 0`
  before the two columns are reconciled. Then drop `bankroll_usd` or
  leave it as a no-op shadow.
- Tests in `tests/test_portfolio_integration.py` already cover the
  canonical column; add a regression test that asserts
  `POST /api/kelly/bankroll` followed by `GET /api/market/bankroll`
  returns the same value.

---

### HIGH-1 — `sizing_table()` does not filter NaN / Inf bankroll, propagates poisoned floats to JSON

**Location:** `kelly.py:59-108`, reachable from `routes.py:144-174`.

**What:**

```python
def sizing_table(our_prob, market_prob, bankroll_usd, *, max_cap=0.25):
    if bankroll_usd <= 0:                   # nan <= 0 -> False, inf <= 0 -> False
        return { ...zeros... }
    ...
    def _row(frac):
        stake = round(bankroll_usd * frac, 2)
        profit = round(stake * b, 2)
        return {"stake_usd": stake, "max_profit_usd": profit, ...}
```

Python comparison semantics:
- `float('nan') <= 0` -> `False` (NaN is unordered)
- `float('inf') <= 0` -> `False`
- `float('-inf') <= 0` -> `True` (safe path, returns zeros — but the
  `note` field still misleads with "Set a bankroll in settings")

So a caller can pass `NaN` or `+Inf` and bypass the zero-bankroll guard.
Downstream:
- `bankroll_usd * frac` propagates NaN/Inf.
- `round(nan, 2) == nan`, `round(inf, 2) == inf`.
- `JSONResponse` -> Python `json.dumps(allow_nan=True)` (default) -> emits
  the literal tokens `NaN` and `Infinity`, which are valid for Python's
  loader but rejected by browsers' `JSON.parse`. The dashboard JS then
  throws on response parsing.

**Reachability via `/api/kelly/calculate`:** the route does
`float(bankroll_override)` on whatever the client sends at
`routes.py:164` — `float("nan")`, `float("inf")`, `float("-inf")` all
succeed. `bankroll_override` is then passed straight into
`kelly.sizing_table`. No `math.isfinite` check anywhere in the chain.

Same applies to `our_prob` / `market_prob` at `routes.py:151-152` —
`float("nan")` succeeds. The `kelly_fraction()` guard at `kelly.py:43-46`
does correctly reject NaN/Inf for the probabilities (the chained
comparison `0.0 < our_prob < 1.0` is `False` for both), so the *fraction*
is safe. But `edge_pct` at `kelly.py:75` and `kelly.py:102` computes
`round(100.0 * (our_prob - market_prob), 2)` unconditionally — so even
the zero-bankroll branch poisons `edge_pct` when `our_prob` or
`market_prob` is NaN/Inf.

**Severity:** High — anonymous (authenticated) caller can break the
calculator response with a known-bad input shape, and a hostile
client-side JSON parser will trip on the response (DoS-of-the-feature
for the affected session). No data corruption directly, but combined
with CRIT-1 the user could pass `bankroll_usd: NaN` and have it written
to the DB (see HIGH-3 below).

**Fix:**
- At the start of `sizing_table`, add `if not math.isfinite(bankroll_usd)
  or bankroll_usd <= 0: return _zero_dict_with_note(...)`.
- In `kelly_fraction`, harden by checking `math.isfinite(our_prob) and
  math.isfinite(market_prob)` before the chained comparisons (defensive;
  current behaviour is already correct but is fragile to refactors).
- In `routes.py` API handlers, reject non-finite inputs at the route
  layer with a 400 — keep the calculator pure and easy to reason about.
- Force `JSONResponse(content=..., media_type="application/json")` to
  serialise with `allow_nan=False` so the layer below the route can
  never accidentally emit `NaN` to a browser.

---

### HIGH-2 — User's stored `kelly_fraction` preference is dropped on the calculator path

**Location:** `kelly.sizing_table()` (`kelly.py:59-108`),
`portfolio/routes.py:144-174`.

**What:** The `users` table stores `kelly_fraction REAL NOT NULL DEFAULT 0.5`
(migration 017, line 16). `POST /api/market/bankroll` clamps it to
`0 < f <= 1` at `market_routes.py:1077`. But the Kelly calculator route
(`/api/kelly/calculate`) never reads this preference:

```python
# routes.py:171
bankroll = kelly.get_user_bankroll(_user_id(user))    # only fetches bankroll_usd
table = kelly.sizing_table(our_prob, market_prob, bankroll)
```

`sizing_table` then emits `full / half / quarter` rows hard-coded at
`kelly.py:82-84`:

```python
full = kelly_fraction(our_prob, market_prob, max_cap=max_cap)
half = full * 0.5
quarter = full * 0.25
```

The user's choice of `1.0 / 0.5 / 0.25` is silently ignored. The brief's
"fractional-Kelly clamping (must be `0 <= f <= 1`)" rule is enforced at
the write boundary in `market_routes.py:1077`, but the value never feeds
into the calculator output. So the clamp exists, but it clamps a
preference no caller of the calculator path reads.

**Note on the clamp itself:** `market_routes.py:1077` is
`if not (0 < kelly_fraction <= 1)`. This rejects `0` and rejects `> 1`.
The brief asks for `0 <= f <= 1`. The route is stricter (`0 < f`), which
is reasonable (`0` means "size every bet at zero", a footgun), but the
test at `test_portfolio_integration.py:322` (`test_patch_rejects_kelly_fraction_zero`)
shows this is intentional. Not a bug, but worth noting the brief wording
differs from the implementation.

Also note: `kelly.kelly_fraction()` uses a `max_cap` parameter (default
`0.25`), which is a *different* concept from the user's `kelly_fraction`
preference. The cap is "cap the recommended stake at 25% of bankroll
regardless of edge"; the preference is "scale full Kelly by 1.0 / 0.5 /
0.25 per the user's risk tolerance". They are independent dials and the
codebase conflates terminology by reusing the name `kelly_fraction` for
both — see Info-1.

**Severity:** High — user-visible behaviour mismatch on a load-bearing
trading-related setting.

**Fix:** Either thread the stored preference through to the calculator
(read from `db.get_user_bankroll(uid)["kelly_fraction"]` and use it to
choose which row is "recommended"), or surface all three rows in the UI
and remove the stored preference as redundant. The current state — store
a preference, never use it — is the worst of both.

---

### HIGH-3 — `set_user_bankroll` clamps negatives via `max()`, but does not reject NaN/Inf cleanly

**Location:** `kelly.py:135-141`.

```python
def set_user_bankroll(user_id: int, bankroll_usd: float) -> None:
    import db
    with db.conn() as c:
        c.execute(
            "UPDATE users SET bankroll_usd = ? WHERE id = ?",
            (max(0.0, float(bankroll_usd)), user_id),
        )
```

Behaviour:
- `max(0.0, float('nan'))` -> `0.0` (Python's `max` returns the first
  argument when comparing against NaN; this happens to be safe here).
- `max(0.0, float('-inf'))` -> `0.0` (safe).
- `max(0.0, float('inf'))` -> `inf` (UNSAFE — writes `Infinity` into a
  `REAL NOT NULL DEFAULT 0` SQLite column, which sqlite stores as the
  IEEE 754 binary).
- A subsequent `SELECT bankroll_usd FROM users` returns
  `float('inf')`, which kelly.get_user_bankroll passes through
  unmodified to `sizing_table`, which then hits HIGH-1.

Defence-in-depth: the route handler at `routes.py:190-193` already
caps `bankroll` between `0` and `10_000_000`, so the externally-reachable
write path is safe. But:
- The brief explicitly calls for "NaN/Inf propagation" review at the
  *module* level, and `set_user_bankroll` is a public API of this module.
- Any future internal caller (cron job, admin tool, migration backfill)
  that imports `kelly.set_user_bankroll` bypasses the route guard. There
  is no defence in depth at the data layer.

**Severity:** High — module-level invariant is wrong, route happens to
mask it.

**Fix:**

```python
def set_user_bankroll(user_id: int, bankroll_usd: float) -> None:
    import math, db
    v = float(bankroll_usd)
    if not math.isfinite(v):
        raise ValueError(f"bankroll_usd must be finite, got {v!r}")
    v = max(0.0, v)
    with db.conn() as c:
        c.execute(
            "UPDATE users SET bankroll_usd = ? WHERE id = ?",
            (v, user_id),
        )
```

(Or, per CRIT-1, delete this function altogether and route through
`db.set_user_bankroll` which already lives behind the canonical column.)

---

### MED-1 — Read-modify-write race window on `bankroll` updates

**Location:** `kelly.py:135-141`, `queries/markets.py:438-455`.

**What:** Both setters are blind `UPDATE users SET bankroll[_usd] = ?
WHERE id = ?`. The frontend UX is "user-typed value into a number input,
hits save", so the race is small — but two concurrent saves from
different tabs (or from the settings page and the calculator endpoint at
the same time) will land in arbitrary order. Since both writes are
absolute (not relative), the last-writer-wins outcome is at least
deterministic; there is no "lost increment" bug because there are no
increments.

What is missing:
- No `WHERE id = ? AND version = ?` optimistic-concurrency check.
- No `RETURNING` clause to confirm the value the server actually
  persisted (caller takes the post-write `GET` as the truth, with a
  separate trip and a separate connection).
- No transaction wrapping `set` + read-back at the route layer — the
  POST handler at `routes.py:195-196` writes, then a *separate*
  request comes back for the GET. Plenty of room for a second writer
  to interleave.

`db.conn()` (db.py:258-272) opens a fresh sqlite3 connection per
context, with `journal_mode=WAL` and `busy_timeout=5000`. WAL gives
read-uncommitted-style isolation for readers vs writers, but does *not*
prevent two writers from sequentially overwriting each other.

**Severity:** Medium — real but small impact; the worst case is one of
two simultaneous saves "disappears", which the user notices and retries.
No money movement, no privilege escalation.

**Fix (optional, defer):**
- Add `last_modified INTEGER` column on `users` and require the
  client-supplied `If-Match` header to match before the UPDATE proceeds.
  Probably overkill for a single-user settings field.
- At minimum, log the (user_id, old, new) tuple on every bankroll write
  so a confused user has an audit trail. (Could not find any logging in
  `kelly.set_user_bankroll`.)

---

### MED-2 — `kelly_fraction` returns silently on degenerate inputs, hiding bugs

**Location:** `kelly.py:29-56`.

**What:** Every error path returns `0.0`:
- `our_prob` outside `(0, 1)` -> `0.0`
- `market_prob` outside `(0, 1)` -> `0.0`
- `our_prob <= market_prob` -> `0.0` (correct: no edge)
- `b <= 0` -> `0.0` (defensive but unreachable given prior check)
- `raw <= 0` -> `0.0` (correct: no edge)

The route at `routes.py:144-174` cannot distinguish between
"calculator says no edge" and "calculator was fed garbage". For a
calculator UI this is OK (the user sees a zero recommendation either
way). For monitoring / Sentry signal, every bug looks like "no edge".

**Severity:** Medium — debuggability, not correctness.

**Fix:** Raise `ValueError` for nonsense inputs (NaN, negative,
out-of-range) and return `0.0` only for legitimate no-edge results.
Have the route translate `ValueError` -> 400. This also lets the test
suite assert specific exception types per failure mode.

---

### MED-3 — `_zero_size()` branch returns a different schema than the happy path

**Location:** `kelly.py:72-81` vs `kelly.py:100-108`.

**What:** When `bankroll_usd <= 0`, the dict returned omits `max_cap`
and includes a `note` field. The happy path includes `max_cap` and omits
`note`. Frontend code reading the response has to handle both shapes
(or, worse, doesn't, and breaks when one shape arrives unexpectedly).

```python
# zero-bankroll branch:
{ "bankroll_usd": ..., "edge_pct": ..., "full_kelly_pct": 0.0,
  "full": _zero_size(), "half": _zero_size(), "quarter": _zero_size(),
  "note": "Set a bankroll in settings to get a recommendation" }

# happy-path branch:
{ "bankroll_usd": ..., "edge_pct": ..., "full_kelly_pct": ...,
  "full": _row(full), "half": _row(half), "quarter": _row(quarter),
  "max_cap": max_cap }
```

`_zero_size()` includes the same keys as `_row()`, so the inner
structure matches. The outer schema does not. Also: the zero-bankroll
response includes `edge_pct` computed from `our_prob - market_prob` even
when those inputs are degenerate (see HIGH-1's note on `edge_pct` NaN
poisoning).

**Severity:** Medium — schema drift, latent breakage for any strict
response parser (TypeScript types, OpenAPI consumers, snapshot tests).

**Fix:** Make the two branches return identical keys, with `note: null`
and `max_cap: max_cap` in both. Or split into two distinct response
shapes with a `state: "no_bankroll" | "calculated"` discriminator.

---

### LOW-1 — `kelly_fraction()` `max_cap` is not validated

**Location:** `kelly.py:29-56`.

**What:** `max_cap` is a keyword-only argument with default `0.25`. No
range check. A caller passing `max_cap=-1.0` makes `min(raw, -1.0)`
return `-1.0`, then `max(0.0, -1.0)` returns `0.0`, so the function
masks the bug. A caller passing `max_cap=float('nan')` makes
`min(raw, nan)` -> `raw` in Python (because NaN comparisons are
False), then `max(0.0, raw)` -> `raw` — so a NaN cap silently disables
the cap. A caller passing `max_cap=float('inf')` disables the cap as
intended (`min(raw, inf) -> raw`).

**Severity:** Low — internal API, no current malicious caller, but the
NaN-disables-the-cap behaviour is silently dangerous.

**Fix:** `assert math.isfinite(max_cap) and 0.0 <= max_cap <= 1.0` at
the top of the function.

---

### LOW-2 — Division-by-zero guard is correct but indirect

**Location:** `kelly.py:49-51`.

```python
b = (1.0 / market_prob) - 1.0
if b <= 0:
    return 0.0
```

The brief flags "division-by-zero on zero-edge inputs". Verified:
`1.0 / market_prob` would divide by zero if `market_prob == 0`. The
*previous* guard at `kelly.py:45-46` (`not (0.0 < market_prob < 1.0)`)
rules out `market_prob <= 0.0`, so the divide is safe. Defence in depth
is good but the guard is implicit — a refactor could remove the upper
guard and reintroduce the bug.

`b <= 0` only triggers when `market_prob >= 1.0`, which is already
ruled out. So the second guard is dead code. The same goes for the
`raw <= 0` guard at line 54: given `our_prob > market_prob` (line 47)
and `b > 0`, `raw = (p*b - q)/b = p - q/b > p - q*(1) = p - (1-p) = 2p-1`.
This is non-negative iff `p >= 0.5`. It is *not* generally true that
`our_prob > market_prob` implies `raw > 0`; e.g. `p=0.3, m=0.2` gives
`b=4, raw = (0.3*4 - 0.7)/4 = 0.5/4 = 0.125` — positive. `p=0.1, m=0.05`
gives `b=19, raw = (0.1*19 - 0.9)/19 = 1.0/19 = 0.053` — positive. So
`raw > 0` whenever `our_prob > market_prob` (this is the standard Kelly
result that edge implies a positive bet). The guard is also dead code.

**Severity:** Low — no bug, but the redundancy makes the function 8
lines longer than it needs to be and easier to break in a refactor.

**Fix:** Drop the redundant guards or add a comment explaining they are
defence-in-depth.

---

### LOW-3 — `get_user_bankroll` swallows `KeyError` from `sqlite3.Row`

**Location:** `kelly.py:129-132`.

```python
try:
    return float(row["bankroll_usd"] or 0)
except (TypeError, ValueError, KeyError):
    return 0.0
```

`sqlite3.Row` indexing by name returns `None` if the column exists but
the value is NULL — that is filtered by `or 0`. It raises `IndexError`
(not `KeyError`) if the column does not exist. So a deployment that
somehow has `users` without the `bankroll_usd` column (migration 062
not run) raises `IndexError`, not `KeyError`, and bubbles up.

**Severity:** Low — only matters on a misconfigured deploy.

**Fix:** Catch `IndexError` too, or check the columns once at module
load.

---

### INFO-1 — Terminology overload on the name `kelly_fraction`

The codebase uses `kelly_fraction` for three distinct things:
1. The full-Kelly mathematical output, `f* = (pb-q)/b` (the return of
   `kelly.kelly_fraction()`, also the column name in some contexts).
2. The user-preference dial — one of `1.0 / 0.5 / 0.25` — stored in
   `users.kelly_fraction`.
3. The route param `kelly_fraction` on `POST /api/market/bankroll`.

The function `kelly.kelly_fraction()` shadows the dictionary key
`kelly_fraction` returned from `db.get_user_bankroll`, which is a real
trap for human readers. Recommend renaming the preference column to
`kelly_preference` or `kelly_multiplier`.

---

### INFO-2 — Test coverage gaps

`tests/test_kelly.py` (74 LOC) covers:
- No edge / negative edge / classic positive edge
- Cap application
- Degenerate inputs (`0`, `1` for either probability)
- Zero bankroll
- Positive edge sizes monotone (full > half > quarter)
- `max_loss_usd == stake_usd` at `max_cap=1.0`

Not covered:
- NaN / Inf inputs to any of `kelly_fraction`, `sizing_table`,
  `set_user_bankroll`.
- The `bankroll_usd` vs `bankroll` column split (CRIT-1).
- Concurrent writes to `bankroll_usd` (MED-1).
- Response schema parity between the zero-bankroll and happy paths
  (MED-3).

Recommend a `test_kelly_hostile.py` covering each NaN/Inf path
(probabilities, bankroll, cap) as a regression net once the fixes land.

---

## Summary

`gateway/portfolio/kelly.py` is mathematically correct on its happy path
and correctly handles probability inputs (chained comparisons exclude
NaN/Inf for `our_prob` / `market_prob`). The pure-function `kelly_fraction()`
is the strongest part of the file.

The weaknesses cluster on two seams:

1. The *data* seam — `users.bankroll_usd` versus `users.bankroll`. This
   is a system-level bug, not a math bug: the module's column choice is
   incompatible with the rest of the codebase (CRIT-1).
2. The *types* seam — bankroll arguments are `float` with no `isfinite`
   check, leading to NaN/Inf propagation in `sizing_table` and `set_user_bankroll`
   (HIGH-1, HIGH-3). The route layer happens to defend against most of
   this, but the module is the wrong place to rely on routes for
   sanitisation.

Priority for fix order, lowest blast-radius first:
1. CRIT-1 (data unification) — fixes the silent user-visible bug.
2. HIGH-1 + HIGH-3 (NaN/Inf hardening) — prevents the dashboard JS from
   choking and prevents bad values entering the DB at all.
3. HIGH-2 (use the stored `kelly_fraction` preference, or remove it).
4. MED + LOW items in any order; they are quality-of-life.

End of audit.
