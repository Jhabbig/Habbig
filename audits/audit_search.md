# Audit — search routes

Scope: every search/query endpoint defined under `gateway/*_routes.py`,
focused on input sanitisation, full-text-search injection, rate-limiting,
result-leakage (private collections / sources / users in public results),
and pagination bounds.

## Endpoints in scope

Discovery command:

```
grep -rln "search\|query" gateway/*_routes.py
```

After triaging the noise (most files use the words "query" / "search"
incidentally), the actual user-facing search endpoints are:

| Method | Path                          | Handler                                         | File                              |
| ------ | ----------------------------- | ----------------------------------------------- | --------------------------------- |
| GET    | `/api/search`                 | `search_routes.unified_search`                  | `gateway/search_routes.py`        |
| POST   | `/api/search/click`           | `search_routes.log_click`                       | `gateway/search_routes.py`        |
| GET    | `/api/search/popular`         | `search_routes.popular_queries`                 | `gateway/search_routes.py`        |
| GET    | `/admin/search-analytics`     | `search_routes.admin_search_analytics`          | `gateway/search_routes.py`        |
| GET    | `/api/feedback/search`        | `feedback_routes.api_feedback_search`           | `gateway/feedback_routes.py`      |
| GET    | `/api/collections/search`     | `collections_routes.api_search_candidates`      | `gateway/collections_routes.py`   |
| GET    | `/admin/users` (`?q=`)        | `admin_routes.users_page`                       | `gateway/admin_routes.py`         |

Out-of-scope query-param handlers (no free-text search; they look up by
slug/category/days only) were verified and excluded.

## Severity counts

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 3
- LOW: 4
- INFO: 3

## Findings

### MEDIUM-1 — `/api/search/popular` can echo low-frequency private queries

`popular_queries` returns the top-6 queries from the last 7 days where
`COUNT(*) >= 3` and `LENGTH(query) >= 3` and `query NOT LIKE '%@%'`. The
k-anonymity floor (`_POPULAR_MIN_COUNT = 3`) is the only privacy
control. Two failure modes:

1.  A single user repeating an identical search 3+ times (e.g. an admin
    pasting a draft prediction title, a user fact-checking a leaked
    rumour) makes that string globally visible on the public palette
    empty-state. `unique_users` is *not* part of the HAVING clause, so
    "3 hits from one user" qualifies.
2.  The `query NOT LIKE '%@%'` filter is the *only* PII guard. It
    catches `@handle` lookups but does not catch emails entered without
    `@` (rare), search-pasted JWT fragments, or anything else sensitive
    a user might paste while typing.

File: `gateway/search_routes.py:390-428`.

Fix: change `HAVING n >= ?` to `HAVING COUNT(DISTINCT user_id) >= 3`
(true k-anonymity), and document that low-cardinality queries are still
visible to anyone.

### MEDIUM-2 — Admin `users` search in `/api/search` exposes account-level metadata to admin role only — but the FTS path uses raw LIKE without further escaping

In `unified_search` the admin-only users branch (lines 306-327)
constructs `like = f"%{q_raw.lower()}%"` and binds it as a parameter
into `LOWER(email) LIKE ? OR LOWER(username) LIKE ?`. SQL injection is
not the risk — bindings are parameterised. The risk is:

1.  `q_raw` is *not* passed through `_escape_fts`, so wildcards `%` and
    `_` typed by the admin act as SQL LIKE wildcards. A short string
    like `%@%` matches every email; `_o_` matches every 3-char username
    where the middle char is `o`. Not a security boundary inside
    admin-only code but produces misleading result counts and
    "result_count" telemetry that overstates real matches.
2.  The admin branch is gated by `_is_admin(user)` (line 194 / 306),
    which is correct, but the same `q_raw` is also persisted via
    `_log_query` (line 335). An admin probing for a user with a
    distinctive substring leaves that substring in `search_queries`
    forever — and a future `popular_queries` change that drops the
    `'%@%'` filter would surface it. Tight coupling between admin
    actions and the public analytics table is a sharp edge worth
    flagging.

File: `gateway/search_routes.py:306-327, 132-153`.

Fix: (a) escape `%` / `_` in the LIKE pattern (`q_raw.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")` with `ESCAPE '\'`), (b) skip
`_log_query` when `"users" in requested` and `admin`, or write to a
separate `admin_audit_log`.

### MEDIUM-3 — TTL cache key truncates `q` to 100 chars; long queries collide

`cache_key = f"search:q_{q_raw[:100]}:t_{types}:adm_{int(admin)}:lim_{limit}"`
(line 184). Two distinct queries whose first 100 chars match return the
*first* user's results to every subsequent caller for 30 seconds. The
admin/non-admin and limit/types are part of the key, so cross-role
leakage is bounded, but two anonymous users who both type long
"prefix… +my-private-suffix" queries would still collide. Practical
exposure is low (queries rarely exceed 100 chars) but the truncation
should at minimum be a content hash:

```python
import hashlib
qh = hashlib.sha256(q_raw.encode("utf-8")).hexdigest()[:16]
cache_key = f"search:q_{qh}:t_{types}:adm_{int(admin)}:lim_{limit}"
```

File: `gateway/search_routes.py:184`.

### LOW-1 — `_escape_fts` strips `+` but leaves Unicode lookalikes through

`_FTS_STRIP_RE = re.compile(r"""['"\-:*()<>^~+!]""")` removes the FTS5
operator characters listed in the SQLite docs but does not normalise
non-ASCII lookalikes (e.g. fullwidth `＊` U+FF0A, en-dash U+2013).
SQLite's FTS5 tokenizer (`unicode61 remove_diacritics 2` per migration
115) does not honour those as operators, so no injection occurs — but
copy-pasted text from PDFs containing fullwidth punctuation may yield
empty result sets where the user expected a hit. Cosmetic only.

File: `gateway/search_routes.py:62, 65-74`.

### LOW-2 — No pagination on `/api/search`; only `limit` cap

`unified_search` accepts `limit` in `[1, 50]` *per type* and returns up
to 4 types — so a single call can return ~200 result objects. There is
no `offset` / cursor and no Link/next header. The 30-second cache means
sequential `?limit=50` calls cost the same as one, so abuse impact is
modest, but consumers expecting cursor pagination need to scroll via
narrower queries. Documented limitation; not exploitable.

File: `gateway/search_routes.py:174`.

### LOW-3 — `/api/collections/search` (`api_search_candidates`) has no rate-limit decorator

`collections_routes.api_search_candidates` (lines 417-511) requires
auth (`_require_user`) but has no `@rate_limit` decorator, unlike
`unified_search`. An authenticated user can issue arbitrary substring
LIKE queries against `predictions` and `source_credibility` (no index
on `content LIKE '%…%'`) at full request rate. The handler caps `limit`
to 20 per kind, so each query returns at most 60 rows, but the SQLite
work to satisfy a 300k-row LIKE scan is non-trivial. Add a per-user
limit similar to the search palette (120/min) to bound the DB cost.

Note: searched-over tables (`predictions`, `source_credibility`) are
public content with no per-user visibility, so this is a
DoS-amplification finding, not a result-leakage one.

File: `gateway/collections_routes.py:417-511`.

### LOW-4 — `/api/feedback/search` only filters by `is_public=1`; no rate limit; relies on modal debouncing

`api_feedback_search` calls `_list_items(q=q_clean, include_private=False, …)`
which inserts `WHERE is_public = 1` (good — private posts are excluded)
but has no `@rate_limit` decorator. Comment claims rate-limit is
provided "implicitly by the modal (debounced, single call on blur)" —
which is client-side and easily bypassed. Auth is required so the
blast radius is bounded to authenticated users. Result leak path is
clean. DoS path is open.

File: `gateway/feedback_routes.py:492-521`.

### INFO-1 — Click logging accepts any `query_id` the client returns

`log_click` (lines 344-378) blindly updates `search_queries` by the
client-supplied `query_id`. No check that the row's `user_id` matches
the caller, so a logged-in user *could* tamper with another user's
click attribution. The downstream effect is limited to skewing the
admin analytics page (no auth bypass, no privacy leak — the columns
mutated are `clicked_result_type`, `clicked_result_id`, `clicked_at`).
Worth gating to "rows where user_id IS NULL OR user_id = ?" in a
future hardening pass.

File: `gateway/search_routes.py:367-374`.

### INFO-2 — Admin analytics `/admin/search-analytics` renders queries via `html.escape`; safe

Confirmed: `_top_row` / `_zero_row` both pass `r.get("query")` through
`html.escape` (lines 578, 588). The surrounding `<code>` and `<td>`
tags are static. No XSS via stored queries.

File: `gateway/search_routes.py:577-595`.

### INFO-3 — `_log_query` truncates to `query[:500]`; covers reasonable inputs

Search history insert clips at 500 chars (line 148). FTS5 will gladly
process larger inputs; trimming at insert keeps the analytics table
tidy and bounded.

File: `gateway/search_routes.py:148`.

## Top 3 (action-prioritised)

1. **MEDIUM-1** — switch `/api/search/popular` HAVING to
   `COUNT(DISTINCT user_id) >= 3` so a single user can't promote their
   own typed queries to the public palette empty-state.
2. **MEDIUM-2** — escape SQL LIKE wildcards in the admin `users`
   branch of `/api/search`, and stop persisting admin user-lookup
   queries to `search_queries` (or move them to an admin-only audit
   table).
3. **MEDIUM-3** — content-hash the cache key in `unified_search`
   instead of `q_raw[:100]` to eliminate the long-query cache
   collision class.

## Verified clean

- FTS injection: `_escape_fts` strips every documented FTS5 operator;
  prefix `*` is appended server-side, not user-supplied.
- SQL injection: every `WHERE` clause uses parameter binding. The
  `_list_items` `order_sql` interpolation is gated by a static dict.
- Private-content leakage in unified search: the underlying FTS tables
  (`markets_fts`, `sources_fts`, `predictions_fts`,
  `source_summaries_fts`) cover only public predictions, public
  market_snapshots, and public source records; there is no
  `is_public` column on those tables because the data is uniformly
  public.
- User-search admin gate: `if admin and "users" in requested` (line
  306) plus `_is_admin` check before adding `users` to `requested`
  (line 194) — both bounds enforced.
- HTML escaping in the admin analytics page is consistent and
  parameterised through `html.escape`.

## Methodology

- Identified candidates with `grep -rln "search\|query" gateway/*_routes.py`,
  then narrowed to handlers that accept free-text `q`/`query` input.
- Read each handler end-to-end for: input clipping, FTS5 operator
  escaping, parameter binding, visibility filters, rate-limit
  decorators, and cache-key construction.
- Cross-checked FTS tables vs migration `115_unified_search_fts.py` to
  confirm no privacy column exists on the indexed rows.
- Cross-checked admin gating via `_is_admin` against the user-search
  branch.
- Verified `rate_limit` decorator semantics in
  `gateway/security/rate_limiter.py`.

No code was modified.
