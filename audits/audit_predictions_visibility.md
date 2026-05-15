# User-prediction visibility audit — 2026-05-15

Scope: who can read a user's predictions (the `user_predictions` table from
migration 031) across every surface — page, JSON API, OG card, search,
leaderboard, export.

**Verdict: 3 LOW, 1 MED gap. No HIGH leaks.**

## Surfaces audited

| Surface | File | Result |
|---|---|---|
| `GET /predictions/{id}` (SSR detail) | `user_prediction_routes.py:322` | OK |
| `GET /predictions/public/{user_id}` (SSR profile) | `user_prediction_routes.py:368` | **MED** — ignores `is_anonymous` |
| `GET /predictions` (own history) | `user_prediction_routes.py:293` | OK |
| `GET /api/predictions/me` | `user_prediction_routes.py:232` | OK |
| `PATCH /api/predictions/{id}` | `user_prediction_routes.py:197` | OK |
| `POST /api/predictions` (create) | `user_prediction_routes.py:131` | OK |
| `GET /api/public/v1/predictions/{id}` | `api_public/routes.py:328` | OK |
| `POST /api/public/v1/predictions` | `api_public/routes.py:366` | OK |
| `GET /api/public/v1/feed`, `/best-bets`, `/calendar` | `api_public/routes.py:240+` | OK — source-authored table only |
| `GET /api/leaderboard` | `routes_referrals.py:283` | **LOW** — `user_{id}` fallback |
| `GET /s/p/{token}` (shared prediction) | `routes_sharing.py:203` | OK — owner-minted |
| `GET /og/shared/prediction/{token}` | `routes_sharing.py:323` | OK |
| `GET /api/account/export/.../download` | `export_routes.py:354` | OK — owner-only |
| Search (`/api/search?types=predictions`) | `search_routes.py:281` | OK — source-authored, not user |

## Three core invariants — per request

### 1. Own predictions always visible to owner

Verified across:

- `prediction_detail_page` — `is_owner` short-circuits the privacy gate at
  line 328-332: `if not row["is_public"] and not is_owner: 404`. Owner
  always wins.
- `api_my_predictions` — pulls by `user["user_id"]` only; no parameter
  override. Each row is unconditional.
- `v1_get_prediction` — `is_owner = row["user_id"] == key["user_id"]`
  bypasses the `is_public` gate; owner sees own private rows.
- `list_user_predictions` — `WHERE user_id = ?`, returns all rows
  including private.
- Data export — `_build_zip` writes `db.list_user_predictions(user_id,
  limit=10_000)` for the export owner. Includes private + anonymous
  rows (correct: the owner is exporting their own data).

PASS.

### 2. Private predictions hidden from non-owners — leaderboard, search, OG

**Leaderboard:** `compute_user_leaderboard_scores` in `jobs/referral_jobs.py`
aggregates ALL resolved `user_predictions` rows for each opted-in user
regardless of `is_public`/`is_anonymous`. The job stores total + correct +
accuracy in `user_accuracy`. The leaderboard API surface returns
`total_predictions`, `correct_predictions`, `accuracy` — derived
metrics, **not** individual predictions. So no row contents leak, only
the aggregate score. This is the documented design: opting into the
leaderboard is an explicit second opt-in (`leaderboard_participation`)
separate from per-prediction visibility — the consent model is
"opt-in account, then choose per-prediction whether to also expose
detail". A user who opts into the leaderboard but keeps every
prediction private will still appear with a count + accuracy; that is
intentional (otherwise the leaderboard would be empty unless users
also disclosed reasoning). Documented as such at
`take_routes.py:663-665`.

PASS (with note: leaderboard opt-in implies aggregate-stats consent —
this is the product invariant, not a leak).

**Search:** `search_routes.py:281` only searches the source-authored
`predictions` table via `predictions_fts`. No FTS exists on
`user_predictions` (no `user_predictions_fts` index, no migration that
creates one). User-authored predictions never appear in search results.
The URL `/predictions/{id}` is shared between the source-authored
table (linked from search) and the user-prediction detail page — but
the SSR detail page reads `user_predictions` by primary key, so a
search-hit ID from the source-authored table won't load a user
prediction. Routes are disjoint by ID space (separate tables, separate
autoincrement counters).

PASS.

**OG cards:** Three OG endpoints expose anything prediction-related:

- `/og/shared/prediction/{token}` (`routes_sharing.py:323`) — gated by
  `db_sharing.create_shared_prediction`, which requires
  `resolved_correct = 1` AND `sharer_user_id == row.user_id`. Card text
  is just "@{sharer_handle} called it" + brand chrome — no
  market/probability/reasoning leak. Does NOT check `is_anonymous` or
  `is_public`. By design: the owner explicitly minted a share token,
  which constitutes consent for that single resolved-correct
  prediction. The flag-stripping isn't needed because the card text is
  generic and the prediction must be the owner's own.
- `/og/source/{handle}` (`og_routes.py:75`) — source-authored only, no
  user_predictions touched.
- `/og/market/{slug}` (`og_routes.py:118`) — pulls from source-authored
  `predictions` only.

PASS.

### 3. Anonymous predictions strip `user_id` from non-owners

Verified at three surfaces:

- `v1_get_prediction` (`api_public/routes.py:359-362`): explicit scrub.
  `if not is_owner and row["is_anonymous"]: payload["prediction"]["user_id"] = None`.
  Covered by `test_anonymous_public_hides_user_id_from_non_owner` in
  `tests/test_api_public_polish.py:139`.
- `prediction_detail_page` (`user_prediction_routes.py:334-337`):
  `author = "Anonymous" if row["is_anonymous"] else (username|email)`.
  Author display is replaced before the template renders.
- `_build_prediction_rows_html` (`user_prediction_routes.py:254`):
  rows have no user identifier per row — just market_question,
  outcome, probability, status, edge. No user_id or name on the
  individual row. Safe even if rendered to non-owners (it is, via
  the public profile page).

PASS in two surfaces (1 + 3). FAIL at one surface — see Gap #1.

## Gaps

### GAP 1 — MED — public profile page ignores `is_anonymous`

**File:** `user_prediction_routes.py:368-398` (`public_profile_page`).
**Surface:** `GET /predictions/public/{user_id}` (no auth required).

The page header renders `display_name = u["username"] or f"user{user_id}"`
and template title `{username}'s predictions`. The `is_anonymous` flag
on the *user's predictions* is NOT consulted. A user who marks every
prediction `is_public=1, is_anonymous=1` (expecting to share
performance anonymously) will still have their username and integer
user_id visible at this URL.

Concretely: `/predictions/public/42` → shows `<h1>alice</h1>` (or
`<h1>user42</h1>` if no username) + her public predictions. Anyone who
guesses or scrapes user IDs can deanonymise her record.

Also at line 395: `email=u["email"]` is passed into the template
context. Reviewing the actual template `static/user_prediction_profile.html`
shows it does NOT render `{{ email }}`, so the email itself doesn't
leak in HTML output today — but the parameter is plumbed through and
a future template edit could expose it without anyone re-auditing
this gate. Either drop the param or template-test it.

**Recommendation:** When ALL of a user's *publicly-visible* predictions
are `is_anonymous=1`, treat the whole profile page as anonymous:
suppress username, render the header as `<h1>Anonymous predictor</h1>`,
strip the user_id from URLs that resolve back to identifying pages.
A simpler middle ground: per-row, hide the username next to each
row (rows have no identifier today, so this is automatic), but at
least add a `is_any_non_anonymous = any(not r["is_anonymous"] for r
in rows)` check and fall back to anonymous header when False.

### GAP 2 — LOW — leaderboard handle fallback leaks user_id

**File:** `routes_referrals.py:309`.

```python
handle = (r["handle"] or "").strip() or f"user_{r['user_id']}"
```

When an opted-in user has not set a `leaderboard_handle`, the API
returns `user_42` as their display name. This exposes the raw
integer user_id over the leaderboard JSON to every paid subscriber.
Combined with `/predictions/public/{user_id}`, an attacker can pull
the leaderboard, extract `user_42` strings, and walk each public
profile to map id → predictions.

**Recommendation:** Require a `leaderboard_handle` at opt-in (the
`POST /api/leaderboard/participate` route already accepts
`display_name` — make it mandatory and reject empty). Or fall back
to a hash like `user_{sha256(user_id|salt)[:8]}` so the mapping is
not reversible.

### GAP 3 — LOW — broken `toggle-public` form leaks intent

**File:** `user_prediction_routes.py:361-364`.

The prediction-detail page renders a form posting to
`/api/predictions/{id}/toggle-public`, but no handler is registered
(only PATCH on `/api/predictions/{id}`). Clicking the button 405's.

Not a leak per se — but it's an owner-only UI element rendered
inline. The form action string is the same for everyone, so it
doesn't leak a token. Still: dead UI on a privacy-sensitive control
is a bad smell; an owner clicks "Make private" and gets a 405,
then assumes the API call succeeded. They later check the public
profile and find their prediction still listed.

**Recommendation:** Either implement `POST .../toggle-public` (one-line
wrapper around `db.update_user_prediction`) or change the button to
a JS `PATCH /api/predictions/{id}` call with `is_public=` form field.

### GAP 4 — LOW — `/api/predictions/{id}` GET fetch from `prediction_detail.html`

**File:** `static/prediction_detail.html:54`.

```js
fetch('/api/predictions/' + pid).then(...)
```

The static page tries to enrich itself via this GET, but no GET
handler is registered (only PATCH). All page enrichment that was
supposed to happen client-side silently fails (`.catch` falls through
to "not found"). The SSR fallback renders correctly so users see
a working page, but the JS-enriched share button, reasoning
blockquote, and Brier line only render when the fetch succeeds —
which is never.

**Recommendation:** Add `app.add_api_route("/api/predictions/{prediction_id}",
api_get_prediction, methods=["GET"], include_in_schema=False)` that
applies the same `is_owner OR is_public` gate as the v1 API. Strip
`user_id` from the response when `is_anonymous=1` AND not owner.

## Confirmations (not gaps, listed for completeness)

- `list_public_user_predictions` (`queries/predictions.py:471`):
  `WHERE user_id = ? AND is_public = 1`. Filter is enforced at the
  query level — even if a caller passes a stranger's user_id, only
  is_public=1 rows are returned.
- `update_user_prediction` (`queries/predictions.py:410`): the DB
  function does NOT enforce ownership — it relies entirely on the
  route layer. `api_update_prediction` (`user_prediction_routes.py:208`)
  does check `if row["user_id"] != user["user_id"]: 403`. Defence
  in depth would be nicer (drop the prediction_id and user_id check
  into the UPDATE WHERE clause), but no current caller bypasses the
  route gate.
- `create_shared_prediction` (`db_sharing.py:182`): enforces
  `user_id == sharer_user_id` AND `resolved_correct = 1`. No
  `is_public` check, but that's intentional — minting a share token
  for your own resolved-correct prediction is an opt-in act.
- Shared-prediction OG card cache key
  (`routes_sharing.py:333`: `f"share:p:{token}"`) is keyed by token,
  not by user_id. No cross-user cache poisoning.
- API public `v1_get_prediction` returns 404 (not 403) for
  unauthorised access to private predictions — deliberate, prevents
  enumeration of valid IDs.
- The detail page hides "Make private" / "Make public" toggle from
  non-owners via `is_owner` gate (`user_prediction_routes.py:360`).

## Files referenced

- `/Users/shocakarel/Habbig/gateway/user_prediction_routes.py`
- `/Users/shocakarel/Habbig/gateway/api_public/routes.py`
- `/Users/shocakarel/Habbig/gateway/routes_sharing.py`
- `/Users/shocakarel/Habbig/gateway/db_sharing.py`
- `/Users/shocakarel/Habbig/gateway/og_cards.py`
- `/Users/shocakarel/Habbig/gateway/og_routes.py`
- `/Users/shocakarel/Habbig/gateway/search_routes.py`
- `/Users/shocakarel/Habbig/gateway/routes_referrals.py`
- `/Users/shocakarel/Habbig/gateway/db_referrals.py`
- `/Users/shocakarel/Habbig/gateway/queries/predictions.py`
- `/Users/shocakarel/Habbig/gateway/jobs/referral_jobs.py`
- `/Users/shocakarel/Habbig/gateway/export_routes.py`
- `/Users/shocakarel/Habbig/gateway/exports/generator.py`
- `/Users/shocakarel/Habbig/gateway/migrations/031_user_predictions.py`
- `/Users/shocakarel/Habbig/gateway/migrations/112_shared_predictions.py`
- `/Users/shocakarel/Habbig/gateway/static/user_prediction_profile.html`
- `/Users/shocakarel/Habbig/gateway/static/predictions_public.html`
- `/Users/shocakarel/Habbig/gateway/static/prediction_detail.html`
- `/Users/shocakarel/Habbig/gateway/tests/test_api_public_polish.py`
- `/Users/shocakarel/Habbig/gateway/tests/test_user_predictions.py`
