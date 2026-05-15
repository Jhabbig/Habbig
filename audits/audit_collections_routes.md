# Adversarial Audit — `gateway/collections_routes.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7)
Target: `/Users/shocakarel/Habbig/gateway/collections_routes.py`
Supporting layer reviewed: `/Users/shocakarel/Habbig/gateway/queries/collections.py`,
`/Users/shocakarel/Habbig/gateway/migrations/120_collections.py`,
`/Users/shocakarel/Habbig/gateway/server.py` (rate-limit + CSRF middleware).

Scope was scoped tightly to the four attacker classes named in the brief:

1. Ownership checks on edit/delete (IDOR)
2. Follow/unfollow rate-limit abuse
3. Share-link forgery
4. Public-collection enumeration leaking private slugs

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 3 |
| Low      | 4 |
| Info     | 2 |
| **Total**| **9** |

## Top 3 findings (ranked by exploitability × impact)

1. **MED-1** — No per-user/per-collection rate limit on `POST /api/collections/{id}/follow`
   and `DELETE .../follow`. Only the 600/min global per-IP cap applies, leaving
   trivial follower-count inflation and follower-DoS-by-fanout. (See MED-1.)
2. **MED-2** — `view_count` is incremented on every `GET /api/collections/{id}`
   and `GET /collections/{id}` for any non-owner viewer, with no per-IP/session
   throttle. A single signed-in attacker can push any public/shared board to the
   top of `most_followed_collections` adjacent (`view_count` is the tiebreaker)
   and skew the "Most followed" explore rail. (See MED-2.)
3. **MED-3** — `/c/{handle}/{slug}` route enumerates slug existence by status
   code: invalid slug → 404, private slug whose owner-handle matches → 404, but
   shared/public → 200. Combined with predictable slug generation
   (`_slugify(title)` → kebab-case of the title, sequential `-2`, `-3` suffixes
   from `_unique_slug`), an attacker who knows a user's handle can enumerate
   what topic boards they have under common names ("watchlist-2", "ai-stocks",
   "trump-2024", etc.). The owner's existence is already public via username,
   but the *titles they care about enough to bookmark* are not. (See MED-3.)

---

## Findings

### MED-1 — Follow/unfollow has no per-user or per-collection rate limit

**Location:** `collections_routes.py:361-375` (`api_follow`, `api_unfollow`),
`queries/collections.py:417-461`.

**What:** Both handlers depend on the global per-IP middleware
(`GlobalRateLimitMiddleware` at `server.py:1751`, 600/min). There is no
per-user or per-(user,collection) limit on follow toggles, no idempotency
guard, no minimum interval between toggles. `follow_collection` uses
`INSERT OR IGNORE` so spam follows are silently deduped, but each call still:

- runs a `SELECT … FROM collections WHERE id = ?`
- runs `INSERT OR IGNORE`
- recomputes `follower_count` via a correlated `SELECT COUNT(*)`
- triggers `_notify_followers_async` for any subsequent `add_item` on that
  board (followers list is recomputed each fan-out, but only for `add_item`,
  not for the follow itself — so notification-storm isn't directly driven
  by follow flapping).

**Attack:**
1. Inflate `follower_count` on rival boards by registering many bot accounts
   (free signup is the only barrier in the codebase) and follow/unfollow in
   rapid succession from each. The `most_followed_collections` query at
   `queries/collections.py:545` orders public boards by `follower_count` —
   featured rail manipulation.
2. From a single attacker session, hammer follow/unfollow on one board ~600
   times per minute (per IP). The `UPDATE collections SET follower_count =
   (SELECT COUNT(*) …)` is a write-back into the `collections` row and a
   correlated count over `collection_follows` — cheap individually, but
   sustained churn invalidates SQLite page cache for the collection row and
   spam-bumps `updated_at` only on follow change (`follower_count` is
   recomputed but the update statement at queries/collections.py:438-443
   doesn't bump `updated_at` — good — so `recently_updated_collections`
   isn't directly weaponisable here).

**Why not High:** Global IP-level 600/min cap is a backstop, and Cloudflare
sits in front. Followers can't be forged (one per real account) so the
inflation requires N accounts.

**Fix:**
- Add `_is_rate_limited(f"coll-follow:{user_id}", limit=30, window=60)` to
  `api_follow` / `api_unfollow` and / `api_update_follow`. The bulk of
  legitimate flows toggle once per page-load.
- Optional: bump `collections.updated_at` only on first-time follow, not on
  no-op `INSERT OR IGNORE` flapping — though current code already avoids
  this.

---

### MED-2 — `view_count` is incrementable without throttle by any viewer

**Location:** `collections_routes.py:245-263` (`api_get`),
`collections_routes.py:990-1017` (`page_collection_detail`),
`collections_routes.py:1020-1052` (`page_public`).
Underlying: `queries/collections.py:178-182`, `:205-209`.

**What:** Both API and HTML detail routes call `get_collection(...,
bump_views=True)`. `bump_views` only skips the increment when the viewer IS
the owner — anonymous viewers and any signed-in non-owner each bump
`view_count` by 1 per request. There is no cooldown per IP/session.

**Attack:** A single attacker (or unsigned bot) can pump `view_count` on any
public/shared board with a tight `curl` loop, capped only by the 600/min
global IP limit and Cloudflare. Even with the IP cap, multiple proxy IPs
trivially defeat it.

`view_count` directly impacts:

- `most_followed_collections` (`queries/collections.py:545`) — `ORDER BY
  follower_count DESC, view_count DESC` — view-count is a tiebreaker so a
  board with 1 follower can outrank dozens of 0-follower boards.
- The admin curation list (`list_all_public_for_admin` shows the views
  column at `collections_routes.py:1110`, used as a curation signal).

**Fix:**
- Throttle the increment per (viewer_id|ip, collection_id) with a 5-15
  minute cooldown in the rate-limit table, e.g.
  `if not _is_rate_limited(f"coll-view:{vid_or_ip}:{cid}", 1, 600): bump`.
- Consider exempting bumps from non-public boards entirely (shared boards
  rarely benefit from view-count surfaces).

---

### MED-3 — Public + shared collection enumeration leaks slug existence

**Location:** `collections_routes.py:1020-1035` (`page_public`),
`collections_routes.py:517-530` (`rss_feed`), plus `queries/collections.py:186-210`.

**What:** `/c/{handle}/{slug}` returns 404 in *all* not-found-or-not-visible
cases — good. But:

1. The `_can_view` rule in `queries/collections.py:96-106` returns True for
   *any* signed-in user on `shared` boards. So a signed-in attacker can:
   - Know which usernames exist (public from existing `/profile` flows).
   - Brute-force common slugs against each known username:
     `/c/{victim}/watchlist`, `/c/{victim}/ai-stocks`, `/c/{victim}/saved-1`,
     etc.
   - 200 vs 404 cleanly reveals shared (and public) slug existence.
2. Slugs are predictable: `_slugify(title)` lowercases + kebab-cases the
   title. Collisions append `-2, -3, … -200` (`_unique_slug`,
   `queries/collections.py:69-85`). An attacker with a topic dictionary
   (~10k common titles) can enumerate a victim's library in seconds, capped
   only by the 600/min IP throttle.
3. The 404 path *does* hide private slug existence from anonymous viewers
   and from non-owner signed-in viewers — that part is correct. The leak is
   specifically for `shared` visibility, which the docstring describes as
   "any signed-in narve user". So technically by-design — but exposure of
   the *slug list* (and through it, the titles, since slugs are derived
   from titles) wasn't an explicit product decision. A user who shares one
   board to a friend likely doesn't expect every other narve account to
   enumerate their *other* shared boards.

**Why not High:** Requires a signed-in attacker, and `shared` boards are by
spec readable by all signed-in users. The leak is "titles of all your
shared boards", not private boards.

**Fix:**
- Option A: Make `shared` visibility require an explicit invite token
  (`/c/{handle}/{slug}?t={token}`), and treat shared boards as 404 without
  the token.
- Option B: At minimum, add per-IP/per-user enumeration throttling on
  `/c/{handle}/*` 404 responses — e.g. >50 404s in 5 minutes ⇒ block. The
  rate-limit helper at `server.py:1633` can do this directly.
- Option C: Document `shared` as "discoverable by every signed-in user" so
  the threat model is explicit.

---

### LOW-1 — `_owner_handle` returns empty string on missing user; share URL becomes `/c//slug`

**Location:** `collections_routes.py:74-80`.

```python
def _owner_handle(user_id: int) -> str:
    with db.conn() as c:
        row = c.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else ""
```

A deleted-but-orphaned owner record produces an empty handle, yielding a
share URL of `/c//{slug}`. FastAPI will likely 404 it but the leaked
`canonical_url` (`collections_routes.py:946`) emits `/c//{slug}` into the
HTML `<link rel="canonical">`. Low practical risk because the
`ON DELETE CASCADE` foreign key on `collections.owner_user_id` should drop
the collection if the user row vanishes — but the cascade only fires for
SQLite when foreign keys are enabled (PRAGMA), and that's worth verifying.

**Fix:** Return None / raise on missing user, or skip rendering the share
button when handle is empty.

---

### LOW-2 — `notifications_on` PATCH lacks ownership-collision protection

**Location:** `collections_routes.py:378-400`.

`api_update_follow` calls `set_follow_notifications(user_id, collection_id,
…)`. The query updates only the caller's own follow row, so there's no
cross-user write. However:

- The handler does NOT verify the collection still exists or is still
  visible to the caller. A user who followed a `shared` board, then the
  owner flipped it to `private`, can still PATCH notifications_on without
  realising the board is no longer visible to them. Not a security leak —
  just an inconsistency. Minor UX bug.

**Fix:** Optional `get_collection(id, viewer_user_id=…)` check before
mutating the follow row, to surface a 404 consistent with the GET API.

---

### LOW-3 — `_notify_followers_async` fan-out uses raw collection title from owner input

**Location:** `collections_routes.py:97-114`.

```python
body = f"New {item_type} added to "{title}""
```

`title` is owner-controlled (set via `api_create` / `api_update`). The
notification body is then passed to `create_notification` and persisted to
the `notifications` table. If a recipient renders notification body without
output escaping, the title could carry HTML/script. This is the *creator*
attacking their own *followers*, but with shared/public boards anyone can
become a follower, and titles up to 64 chars are unconstrained text.

Verified: `coll.create_collection` accepts `title` verbatim (no allow-list)
at `queries/collections.py:122-125`. Sanitisation responsibility falls to
whoever renders the notification.

**Fix:** Either escape on render (preferred — `notifications` is a generic
table) or strip control chars + cap length in the route layer.

---

### LOW-4 — `api_search_candidates` SQL LIKE allows wildcard injection in `q`

**Location:** `collections_routes.py:417-511`, particularly `441` (`like = f"%{q}%"`).

`q` is concatenated into a `LIKE` parameter without escaping `%` / `_`. A
crafted query of `_` or `%%` becomes a full-table scan over
`source_credibility` and `predictions`. `predictions` is plausibly
300k+ rows (per the inline comment at line 489). Combined with a `LIMIT`
cap, this is one slow query per request, but at 600/min × tens of seconds
each, it's a DOS vector.

**Fix:** Escape `%` and `_` in the user-supplied substring before
interpolation:

```python
def _esc_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
```

…and append `ESCAPE '\\'` to each LIKE clause.

---

### INFO-1 — Ownership checks on edit/delete: correctly enforced

**Location reviewed:** `update_collection` (`queries/collections.py:213-261`),
`delete_collection` (`:264-277`), `_assert_collection_mutable` (`:283-294`),
which gates `add_item`, `remove_item`, `reorder_items`.

All four go through the same shape:

```python
row = c.execute("SELECT owner_user_id, is_system FROM collections WHERE id = ?", (cid,)).fetchone()
if not row: raise LookupError(...)
if row["owner_user_id"] != owner_id: raise PermissionError("not owner")
```

The route layer converts `PermissionError` → 403 and `LookupError` → 404.
There is no path that performs a mutation without checking
`owner_user_id == request.user.user_id`. Specifically:

- Update / delete / reorder / add-item / remove-item all use
  `user["user_id"]` from `_require_user(request)` — server-side, not from
  the body.
- `api_add_item` and friends accept `id` from the URL path; the
  `owner_user_id` check rejects any mismatch.
- System collections (`is_system=1`) cannot be renamed (`update_collection`)
  or deleted (`delete_collection`) or have items added/removed
  (`_assert_collection_mutable`) — also good.

No IDOR found in the edit/delete surface. ✅

---

### INFO-2 — CSRF + auth wrapping is consistent

All state-changing JSON endpoints (`POST/PATCH/DELETE`) gate on
`_require_user(request)` first. The global `CSRFMiddleware` (registered at
`server.py:1470`) covers all non-safe methods. The admin route
`/admin/api/collections/{id}/feature` additionally requires
`_require_admin_user`. Auth/CSRF surface is correct. ✅

---

## Out of scope (noted, not investigated)

- The `_resolve_items` cache reads (`unified_markets._get_cached`) — separate
  cache-poisoning surface, not relevant to the four classes in the brief.
- The HTML rendering path (`_render_detail_item`, inline `__hbColl.remove(…)`
  onclick) — XSS concerns are item-meta-only since user-input titles
  *appear* to be `_html.escape`d uniformly in this file. A full XSS sweep
  was not requested.

---

## Recommended priority order

1. Throttle `view_count` bumps (MED-2) — single-line `_is_rate_limited`
   gate.
2. Throttle follow toggles per user (MED-1) — likewise one-liner.
3. Either gate `shared` boards behind invite tokens or add 404-rate-limit
   detection (MED-3) — larger change.
4. Escape `%`/`_` in `api_search_candidates` (LOW-4).
5. Cleanups on LOW-1 through LOW-3.
