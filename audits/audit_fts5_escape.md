# FTS5 Query Escape Audit

**Date:** 2026-05-15
**Scope:** All code paths that feed user input into a SQLite FTS5 `MATCH` predicate.
**Method:** Source review + synchronous probe matrix executed against the actual SQLite FTS5 parser.

---

## Functions audited

Two distinct escape helpers exist in the codebase:

| Function | File | Used by |
|---|---|---|
| `_escape_fts(q)` + `_fts_prefix_query(q)` | `gateway/search_routes.py:62–94` | `/api/search` palette endpoint (markets, sources, predictions, users) |
| `_fts_sanitize_query(q)` | `gateway/db.py:609–628` | `db.search_markets()`, `db.search_sources()`, `db.search_predictions()` (called from queries/markets.py, queries/sources.py, queries/predictions.py) |

All four FTS5 tables (`markets_fts`, `sources_fts`, `predictions_fts`, `source_summaries_fts`) are queried only through these two helpers — there are no raw user-input → MATCH paths elsewhere.

---

## Probe matrix

Each probe was run through both escape functions; the resulting string was then executed against an in-memory FTS5 table to confirm whether the SQLite FTS5 parser still treated any operator semantically.

Legend:
- **literal** — character/word reached FTS5 but was treated as a search token, not an operator.
- **stripped** — character was removed by the escape function before reaching FTS5.
- **OPERATOR** — character/word reached FTS5 AND was interpreted as a query-language operator. This is operator injection.
- **syntax-error** — the cooked query is rejected by FTS5 (caught by `except sqlite3.Error` in the route — search silently returns no results).

### `gateway/search_routes.py::_escape_fts` (used by `/api/search`)

| Input | Cooked output | Result |
|---|---|---|
| `o'brien` | `o brien*` | `'` stripped |
| `bit"coin` | `bit coin*` | `"` stripped |
| `foo*bar` | `foo bar*` | `*` stripped |
| `a+b` | `a b*` | `+` stripped |
| `-rate hike` | `rate hike*` | `-` stripped |
| `col:val` | `col val*` | `:` stripped |
| `(foo OR bar)` | `foo OR bar*` | parens stripped, **`OR` reaches FTS5 as OPERATOR** |
| `NEAR(a b)` | `NEAR a b*` | parens stripped, **`NEAR` reaches FTS5** (bareword, not an op without parens — but see below) |
| `foo AND bar` | `foo AND bar*` | **`AND` reaches FTS5 as OPERATOR** |
| `foo OR bar` | `foo OR bar*` | **`OR` reaches FTS5 as OPERATOR** |
| `foo NOT bar` | `foo NOT bar*` | **`NOT` reaches FTS5 as OPERATOR** |
| `AND foo` | `AND foo*` | **syntax-error** — silently returns `[]` |
| `a^b` / `a~b` / `a!b` | `a b*` | stripped |
| `a<b>c` | `a b c*` | stripped |
| `curly"quote` (U+201C) | `curly"quote*` | unicode left-quote NOT stripped (regex only matches ASCII `"`) — but FTS5 tokenizer treats it as a word char, no op |
| `a\b` | `a\b*` | backslash NOT stripped — no FTS5 op semantics, harmless |
| `"*-+:()` | `` | all stripped, empty |
| `foo and bar` (lowercase) | `foo and bar*` | `and` is a literal in FTS5 (only uppercase `AND` is an op), safe |

### `gateway/db.py::_fts_sanitize_query` (used by `db.search_*`)

Wraps every whitespace-separated token in `"…"` and escapes embedded `"` as `""`. Verified against the same probes:

| Input | Cooked output | Result |
|---|---|---|
| `alpha AND beta` | `"alpha" * "AND" * "beta" *` | `AND` is **quoted → literal**, safe |
| `alpha OR beta NOT gamma` | `"alpha" * "OR" * "beta" * "NOT" * "gamma" *` | all bareword ops **quoted → literal**, safe |
| `alpha NEAR beta` | `"alpha" * "NEAR" * "beta" *` | quoted → literal, safe |
| `alpha" OR "beta` | `"alpha""" * "OR" * """beta" *` | quote escape via `""` works — FTS5 sees `alpha"` and `"beta` as tokens, no breakout |
| `*` | `"*" *` | quoted → literal, safe |
| `-` | `"-" *` | quoted → literal, safe |
| `col:val` | `"col:val" *` | colon **inside quotes is literal in FTS5**, no column filter injection |
| `bit"coin` | `"bit""coin" *` | embedded `"` properly doubled, safe |

All probes against `_fts_sanitize_query` resolve to safe literal-search behavior. No operator injection.

---

## Findings

### F1 (HIGH) — `_escape_fts` lets `AND` / `OR` / `NOT` reach FTS5 as operators

**File:** `gateway/search_routes.py:62–74`
**Affected endpoint:** `/api/search` (palette, hit on every keystroke from the global ⌘K)

The strip regex `_FTS_STRIP_RE = re.compile(r"""['"\-:*()<>^~+!]""")` only removes punctuation operators. It does NOT remove the bareword operators FTS5 also supports: `AND`, `OR`, `NOT`, `NEAR`.

Confirmed against the live FTS5 parser:

```
input:  foo AND bar      cooked: foo AND bar*   semantics: foo ∩ (bar prefix)
input:  foo OR bar       cooked: foo OR bar*    semantics: foo ∪ (bar prefix)
input:  foo NOT bar      cooked: foo NOT bar*   semantics: foo AND NOT (bar prefix)
input:  AND foo          cooked: AND foo*       semantics: SYNTAX ERROR
```

**Impact:**
1. **Operator injection (functional).** A user who types `bitcoin AND crash` gets an intersection query, not a literal-substring search for those three words. Same for `OR` / `NOT`. This is not a security boundary (FTS5 has no column-cross-talk because every FTS5 table the search hits is per-domain), but it IS a behaviour bug — the docstring claims operators are stripped, and they are not.
2. **Silent breakage when a query starts with `AND` / `NOT` / `OR`.** The cooked query is a syntax error; the route's `except sqlite3.Error` swallows it and returns zero results. A search for `"AND1 currency"` (legitimate phrase) silently fails.
3. **Inconsistency with `_fts_sanitize_query`** which DOES neutralise these via per-token quoting. Two helpers, two behaviours — the safer one is not the one used on the public palette.

**Fix sketch:** quote each term before joining, mirroring `_fts_sanitize_query`. With prefix-wildcard outside the quote: `'"foo" "bar"*'`. This makes `AND`, `OR`, `NOT`, `NEAR`, `:`, and bareword-op edge cases all-literal in one pass and removes the need for the strip regex entirely.

### F2 (MEDIUM) — `NEAR` keyword reaches FTS5 unfiltered

**File:** `gateway/search_routes.py:62`
Same root cause as F1. Confirmed:

```
input: NEAR(a b)   cooked: NEAR a b*    semantics: searches for tokens NEAR, a, b
input: a NEAR b    cooked: a NEAR b*    semantics: same — without parens, NEAR is a token not an op
```

`NEAR` without trailing `(` parses as a regular term, so the practical impact is lower than `AND`/`OR`/`NOT`. But the fix is the same as F1, so they should be addressed together.

### F3 (LOW) — Strip regex is ASCII-only

**File:** `gateway/search_routes.py:62`
Unicode quote characters (`U+201C` `“`, `U+201D` `”`, `U+2018` `‘`, `U+2019` `’`) and backslash are not stripped. FTS5's default tokenizer treats them as word-characters, so no operator semantics are reachable — but if the tokenizer is ever swapped to `unicode61 remove_diacritics 2` or a custom tokenizer that treats fancy quotes as delimiters, this assumption silently breaks. Not exploitable today; flagging for the next tokenizer change.

### F4 (LOW) — No regression test for any FTS5 operator probe

**File:** `gateway/tests/test_search.py`, `gateway/tests/test_user_features.py`
Existing tests cover empty input, quote-doubling in `_fts_sanitize_query`, prefix-match correctness. They do NOT cover:
- `AND` / `OR` / `NOT` / `NEAR` as inputs to either escape helper
- A syntax-error round-trip (does the route 500 or return `[]`?)
- Cross-helper drift — both helpers should agree on operator handling

A small parametrised test in `test_search.py` running the F1 probe matrix would have caught this on day one.

### F5 (LOW / drift) — Two divergent helpers for the same problem

`_escape_fts` (search_routes) and `_fts_sanitize_query` (db.py) both claim to make user input safe for FTS5 MATCH, but they take different approaches (strip vs quote) and produce different safety guarantees. The strip-based one is wrong on bareword ops; the quote-based one is correct. They should converge — preferably on the quote-based approach — and one of them should be deleted.

---

## What is safe today

- `db.search_markets()`, `db.search_sources()`, `db.search_predictions()` (queries/markets.py, queries/sources.py, queries/predictions.py): use `_fts_sanitize_query`, confirmed robust against all probes including `AND`/`OR`/`NOT`/`NEAR`/`:`/`"` / `*` / `-`.
- `/api/search` is **not** SQL-injectable — the cooked string is bound as a parameter, not concatenated. FTS5 operator injection here is a behaviour/quality bug, not a SQLi vector. No data exfiltration vector identified.

---

## Gaps

1. **F1** — `_escape_fts` admits `AND` / `OR` / `NOT` as operators on the public palette endpoint. Behaviour bug, not a SQLi, but contradicts the function's own docstring and creates a denial-of-service-by-typo when input starts with one of those keywords.
2. **F2** — Same root cause as F1, lower practical impact for `NEAR`.
3. **F4** — No automated regression for FTS5 operator probes on either helper.
4. **F5** — Two helpers with divergent safety models; the weaker one is on the user-facing path.
5. **F3** — ASCII-only strip regex is fine today but coupled to current tokenizer choice; brittle.

No SQLi or data-leak vector identified. The fix for F1/F2/F5 is a single coordinated change: replace `_escape_fts` with a quote-each-term implementation aligned to `_fts_sanitize_query`, then collapse to one helper.
