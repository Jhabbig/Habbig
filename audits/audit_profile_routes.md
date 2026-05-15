# Adversarial audit — `gateway/profile_routes.py`

- File: `/Users/shocakarel/Habbig/gateway/profile_routes.py` (598 LOC)
- Aux modules read for context: `gateway/queries/profile.py` (299 LOC),
  `gateway/migrations/172_public_profile_fields.py`,
  `gateway/migrations/173_user_follows.py`, `gateway/static/profile_public.html`,
  `gateway/static/settings_profile.html`, `gateway/server.py`
  (render_page, CSP, CSRF, gate middleware).
- Date: 2026-05-15
- Auditor focus (requested):
  1. Profile-handle uniqueness race
  2. Avatar URL SSRF (does it fetch the URL server-side?)
  3. Bio XSS via raw_*
  4. Handle-change rate-limit
  5. Public-profile visibility opt-in
- Method: static read, cross-reference against template `{{ }}` raw vs.
  escaped insertion, DB schema review, transaction-boundary analysis,
  CSP review. No live exec, no code changes.

## Severity counts

| Severity | Count |
| --- | ---: |
| Critical | 0 |
| High     | 1 |
| Medium   | 3 |
| Low      | 5 |
| Info     | 3 |

Total: 12 findings.

## Top 3 (by exploitability × blast radius)

1. **H1 — Pillow decompression-bomb DoS on `/api/settings/avatar`.** The
   handler caps raw bytes at 2 MB but never sets `Image.MAX_IMAGE_PIXELS`,
   never installs a `PIL.Image.DecompressionBombError`-tripping limit,
   and feeds the byte stream through `Image.open(…).verify()` then a
   second `Image.open(…)` for resize. A ~2 MB PNG can encode roughly
   89 M pixels of solid colour and decode to ~360 MB of RGBA in memory
   before Pillow ever raises its (default-warning-only) bomb check.
   With no per-user/IP rate-limit on this endpoint, a single
   authenticated attacker can OOM the worker(s) with a handful of
   parallel uploads. (lines 447–512)

2. **M1 — Handle-uniqueness check is a TOCTOU across two separate
   `db.conn()` contexts; race produces an uncaught
   `sqlite3.IntegrityError` → HTTP 500 instead of `handle_taken`.**
   `update_profile()` calls `handle_taken_by_other(handle, user_id)`
   (its own connection, line 140), then opens a **new** connection
   for the SELECT/UPDATE block (line 144). Two concurrent requests
   for the same free handle both pass the read check, both reach the
   UPDATE, the unique partial index on `users(profile_handle)` rejects
   the second writer, and the exception propagates out of
   `api_settings_profile` because the handler only catches
   `profile_q.ProfileError`. No duplicate handle is created (good —
   the index does its job), but the second user gets a 500 and the
   30-day cooldown timestamp on the **first** user is now set, while
   the second user is free to retry. Net effect: a determined attacker
   racing many requests can grab a freshly-vacated reserved-clearing
   handle ahead of the legitimate caller — and the loser sees an
   opaque 500.
   (`queries/profile.py` lines 83–97, 114–186; `profile_routes.py`
   lines 430–435)

3. **M2 — No rate-limit on `/api/settings/profile`, `/api/settings/avatar`,
   `/api/follow/{user_id}`.** The 30-day handle cooldown only fires
   **once a handle has been set and is being changed**. Submitting
   the form repeatedly with rejected handles (`handle_invalid`,
   `handle_reserved`, `handle_taken`) is free, as is repeated bio
   editing, repeated avatar uploads, and repeated follow toggles.
   The CSRFMiddleware enforces a double-submit cookie but no
   limiter is wired. Concrete abuse paths:
   - **Handle enumeration** — POST with `handle=alice` and read the
     400 vs. 200; faster than scraping `/u/alice` (which is rate-
     limit-free anyway).
   - **Avatar DoS** (see H1).
   - **Follow-spam / unfollow-thrash** to inflate follower counts on
     attacker-controlled profiles or to spam victim notifications if
     a future notification surface hooks `user_follows` writes.
   - **Bio-rewrite churn** to keep stats invalidations / cache busting
     hot.
   (lines 416–441, 447–512, 536–561)

## Findings — detail

### H1. Pillow decompression-bomb / no `MAX_IMAGE_PIXELS` on avatar upload — High

**Location:** `api_settings_avatar` lines 447–512.

```python
_MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
...
raw = await upload.read()
if len(raw) > _MAX_AVATAR_BYTES:
    return JSONResponse({"error": "too_large", ...}, status_code=413)
...
from PIL import Image
img = Image.open(io.BytesIO(raw))
img.verify()
img = Image.open(io.BytesIO(raw))
```

Pillow's default `Image.MAX_IMAGE_PIXELS` is ≈ 89 M. A solid-colour PNG
of those dimensions compresses to a few hundred kilobytes — well under
the 2 MB cap. On decode (and again on `img.resize(...)` to 200×200,
which has to allocate the source buffer) Pillow materialises the full
RGBA pixel grid: at 4 bytes/pixel that is ~360 MB per request.
Pillow's default behaviour at the warning threshold is a
`DecompressionBombWarning`, not an exception; the hard error fires only
at 2× the limit. There is no explicit `Image.MAX_IMAGE_PIXELS = …`
override anywhere in the repo (verified — `grep -rn MAX_IMAGE_PIXELS`
returns no matches).

```
$ grep -rn "MAX_IMAGE_PIXELS\|DecompressionBomb" /Users/shocakarel/Habbig/gateway/
(no matches)
```

Combined with M2 (no rate-limit on this endpoint) and M5 (no per-user
upload throttle), a single authenticated user can:

- Send N concurrent uploads of a crafted 2 MB PNG (e.g., 9504×9504
  monochrome).
- Each handler thread allocates ~360 MB peak (decode buffer + crop +
  resize).
- Workers OOM-kill or evict legit traffic from the pool.

`img.verify()` does **not** prevent this. Per Pillow docs, `verify()`
only sanity-checks frame headers; the bomb check is on `load()` /
implicit on operations that materialise pixels. The code calls
`Image.open(...)` a second time (line 486), then `.crop(...)`,
`.resize(...)`, `.convert(...)`, `.save(...)` — every one of those
forces decode.

The `Pillow not installed` branch (line 479) returns 500 if the import
fails — fine, that is not the exploit path.

**Recommendations** (no code change required by the task):

- Set `Image.MAX_IMAGE_PIXELS` to something proportionate to the use
  case (e.g., 4096 × 4096 = 16.7 M) at module import.
- Catch `Image.DecompressionBombError` and return 400 / `bad_image`.
- Reject formats up-front by sniffing the magic bytes; only accept
  `image/png`, `image/jpeg`, `image/webp`.
- Add a per-user rate-limit (e.g., 5 avatar uploads / hour) and a
  per-IP cap on this prefix.
- Consider `Image.thumbnail(...)` on the in-memory buffer before any
  `.crop/.resize` so the materialised allocation is bounded by the
  output dimensions rather than the input.

---

### M1. TOCTOU on handle uniqueness across two `db.conn()` contexts — Medium

**Location:** `queries/profile.py` lines 83–97, 114–193; route layer
`profile_routes.py` lines 430–435.

`update_profile()` performs the uniqueness check outside the
transaction that does the write:

```python
# queries/profile.py
if handle_taken_by_other(handle, user_id):     # 1st db.conn()
    raise ProfileError("handle_taken", ...)
...
with db.conn() as c:                            # 2nd db.conn()
    existing = c.execute("SELECT ...").fetchone()
    ...
    c.execute("UPDATE users SET ... profile_handle = ? ...", (new_handle, ...))
```

`db.conn()` (`gateway/db.py` line 257) opens a fresh SQLite connection
each time at default isolation. Two requests for the same unowned
handle:

1. Both call `handle_taken_by_other` → both see "free".
2. Both enter the second `with db.conn()`.
3. SQLite serialises writes. First UPDATE succeeds. Second UPDATE
   violates the partial UNIQUE INDEX
   (`idx_users_profile_handle ON users(profile_handle) WHERE profile_handle IS NOT NULL`,
   migration 172 line 47).
4. `sqlite3.IntegrityError` is raised inside `update_profile`.
5. The route handler only catches `profile_q.ProfileError`
   (line 434). `IntegrityError` propagates out, the global handler
   responds 500.

**Outcome:**
- No duplicate handle is created. The DB constraint is the actual
  guarantee. Good.
- The loser sees an opaque 500 rather than `handle_taken` and
  has no actionable error UI.
- The winner's `profile_handle_changed_at` is set, locking them out
  of changes for 30 days.
- Race-aware abuse: a quick attacker watching a popular handle being
  vacated (e.g., name `alice` cleared because the owner's account was
  deleted — see also L4) can grab it ahead of the legitimate caller
  reliably by spraying requests.

**Fixes** (informational — out of scope for this audit):
- Wrap the read+write in a single connection with `BEGIN IMMEDIATE`
  so the second writer fails fast on lock acquisition.
- Catch `sqlite3.IntegrityError` and translate to
  `ProfileError("handle_taken", ...)` so the loser gets a 400.
- Alternatively, the cleaner pattern is to attempt the UPDATE and
  rely on the unique index, then surface the integrity error as
  the user-facing taken message — no pre-read needed.

---

### M2. No rate-limit on profile / avatar / follow POSTs — Medium

**Location:** `register()` lines 567–598; the three POST routes
`api_settings_profile` (416–441), `api_settings_avatar` (447–512),
`api_toggle_follow` (536–561).

CSRF middleware fires for all three (verified: none of the paths
appear in `_CSRF_EXEMPT_POSTS` / `_CSRF_EXEMPT_POST_PREFIXES` in
`server.py:1104–1158`). But there is no `rate_limit` decorator
applied (none imported in this file), and no per-prefix limiter in
`server.py`. The repo has a `security/rate_limiter.py` module that
the file does **not** use:

```
$ grep -n "rate_limit\|RateLimit" gateway/profile_routes.py
(no matches)
```

Concrete abuses:

| Endpoint | Limit-less behaviour | Impact |
| --- | --- | --- |
| `POST /api/settings/profile` | Fire as fast as the worker accepts; cooldown only blocks **changing** an existing handle. Initial-set or same-handle re-submission is free. | Handle enumeration (200 vs. 400 reveal); bio-thrash to spike DB churn. |
| `POST /api/settings/avatar` | 2 MB per upload, unbounded N. | DoS vector (see H1). |
| `DELETE /api/settings/avatar` | Idempotent but unbounded. | Free disk churn; less interesting. |
| `POST /api/follow/{user_id}` | One DB write per call (`INSERT OR IGNORE` / `DELETE`). | Mass-follow / unfollow-thrash; future notification spam. |

Note: site-wide gate + auth do reject anonymous traffic, so the
adversary must be a paying user. The Stripe-level barrier raises the
floor but does not eliminate the risk — a single compromised account
or a malicious low-tier user can still saturate.

---

### M3. Follow targets need not have opted in to public profiles — Medium

**Location:** `api_toggle_follow` lines 536–561.

```python
target = db.get_user_by_id(user_id)
if not target:
    return JSONResponse({"error": "not_found"}, status_code=404)
state = profile_q.toggle_follow(viewer["user_id"], user_id)
```

The check is "does the user row exist", not "has this user opted in
to a public profile / accepted being followed". Any authenticated
user can therefore POST `/api/follow/{user_id}` for **any** user id
in the system — including users who have never set
`public_profile_enabled = 1`, never published a profile, and have no
intention of being part of a social graph. The row lands in
`user_follows`. Implications:

- Forces a user into a social-graph relationship they never
  consented to. `follower_count(target)` increments visibly on any
  future profile rollout for that user.
- Couples cleanly with M2: mass-follow scripted from one bad actor.
- Future features that read `user_follows` (notifications, "people
  who follow you" lists) will surface these forced relationships.
- Combined with L3 below (internal `user_id` leaks via avatar URL),
  the attacker can scrape `user_id`s from public `/u/{handle}` pages
  and then fan out follows against everyone the site exposes.

The opt-in semantics for public profiles (existence-hide via 404 on
`/u/{handle}` for non-opted-in users) are inconsistent with the
follow surface accepting writes against those same users.

**Fix direction:** require `target["public_profile_enabled"] = 1` in
`api_toggle_follow`. Per-user "allow follows" toggle if you want
finer control.

---

### L1. JSON-LD bio injection partially mitigated — Low

**Location:** lines 261–264, helper `_person_schema` 76–88.

```python
schema_payload = json.dumps(
    _person_schema(row, stats, profile_url=profile_url),
    separators=(",", ":"),
).replace("</", "<\\/")
```

`json.dumps` with default `ensure_ascii=True` already escapes non-ASCII
as `\uXXXX` and escapes embedded quotes/backslashes. The follow-up
`.replace("</", "<\\/")` defangs the only sequence the HTML5 parser
treats as a script-end (`</script>` and any `</…` actually, since the
replace is unconditional). This is a correct script-context escape for
JSON-LD payloads. **Not exploitable today**, but two caveats worth
noting:

1. The substitution is global (`</` everywhere), not just before
   `script`. A bio of `"</"` becomes `"<\\/"` in the rendered JSON,
   which a strict JSON-LD consumer would still parse correctly (JSON
   allows the escape).
2. Equivalent inputs are not normalised — Unicode control characters
   in `bio` are passed through `json.dumps` as `\uXXXX`. A bio of
   ` ` (line separator) would historically have broken inline
   `<script>` blocks under some legacy JS engines, but `json.dumps`
   already encodes those literally as the 6-char escape so the
   browser sees safe ASCII. Confirmed by spec.

Bio is also embedded into OG meta `content="…"` via
`_html.escape(og_description)` (line 269), so quote-breaking is
prevented. Good.

The schema description fallback to
`f"Forecaster on narve.ai with {accuracy_pct} accuracy"` uses
`accuracy_pct` which is `"{N:.0f}%"` or `"—"` — both pure ASCII. Fine.

**Why "Low" not "Info":** the CSP is permissive
(`script-src 'self' 'unsafe-inline'`, line 884 of `server.py`),
meaning if escaping were ever wrong here, CSP would not save us. The
escape is load-bearing.

---

### L2. Bio rendered into OG meta `content="…"` — escaping intact but
fragile — Low

**Location:** lines 265–281.

```python
og_description = (
    bio if bio
    else f"Forecaster on narve.ai with {_fmt_pct(stats['accuracy'])} accuracy"
)
og_description_safe = _html.escape(og_description)
...
og_meta = (
    ...
    f'<meta property="og:description" content="{og_description_safe}">\n'
    ...
)
```

`_html.escape` defaults to `quote=True` (in Python 3.2+), so `"`, `<`,
`>`, `&`, `'` are encoded. **Not exploitable.** Flagged Low because:

- The whole `og_meta` string is interpolated into the template as
  `raw_og` — raw injection — which means any future change in this
  module that forgets to escape a new field will be silently raw-injected.
- The handle in the same block uses `handle_safe = _html.escape(handle)`
  but the handle has already passed `^[a-z0-9_]{3,20}$` so the escape
  is redundant. Defensive code is good; the comment in the file should
  reflect that the regex is the primary defense.

Recommend: build the OG meta with a helper that always html-escapes
its inputs, rather than re-implementing the escape in-line each time.

---

### L3. Internal `user_id` leaked via `profile_avatar_url` on every
public profile — Low

**Location:** `_avatar_url` lines 68–73, write-back at lines 510–511,
read at the public profile page (template line 33–39).

Once a user uploads an avatar, `profile_avatar_url` is set to
`/_gateway_static/avatars/{user_id}.webp?v={ts}`. This URL is then
returned in:

- `<img src="…">` on `/u/{handle}` (raw template) — visible in HTML
  source to any anonymous visitor or crawler.
- The OG card endpoint output (separate path, no user_id in it — fine).

So **internal numeric user_id** is observable from outside for every
opted-in user with an uploaded avatar. Direct consequences:

- Enumeration of total user count (max observed id).
- Couples with M3 (forced follow): scrape `/u/*` pages, harvest
  `user_id`s, mass-POST `/api/follow/{user_id}`.
- Pivot for any other endpoint that takes `user_id` and assumes
  anonymity (e.g., `/api/predictions/public/{user_id}` if such a
  route exists).

This is the kind of low-severity finding that compounds: every other
"user_id is opaque" assumption in the codebase is weaker because of
this leak. Storing the avatar as a hash of `(user_id, upload_ts)`
or under a per-user opaque token would unbreak it.

---

### L4. Avatar file is not deleted on profile opt-out — Low

**Location:** `update_profile` lines 114–193, `api_settings_avatar_delete`
lines 515–530.

Flipping `public_profile_enabled` to 0 does **not** call
`update_avatar_url(user_id, None)` and does **not** unlink the file
on disk. The file remains at `/_gateway_static/avatars/{user_id}.webp`
and remains publicly fetchable from `_gateway_static` (no auth in
front of the static prefix; this is the documented design per
`_PUBLIC_PREFIXES`). So a user can:

1. Upload an embarrassing avatar.
2. Toggle their public profile off.
3. Be told "your data stays put" (per the settings copy at
   `static/settings_profile.html` line 67).
4. Still have the avatar URL reachable by anyone who recorded it,
   or who guesses the `user_id`.

The opt-out story is "your `/u/{handle}` returns 404" but **not**
"your avatar disappears from the world". Worth either:

- Deleting the on-disk file when `public_profile_enabled` flips to 0,
  or
- Documenting that opt-out does not remove the file (and link to
  `DELETE /api/settings/avatar` from the UI for the destructive
  action).

This is a privacy expectation gap, not a code defect.

---

### L5. `toggle_follow` is logically two transactions, racy — Low

**Location:** `queries/profile.py` lines 265–279.

```python
def toggle_follow(...):
    ...
    if is_following(...):       # connection #1
        unfollow(...)            # connection #2
        new_following = False
    else:
        follow(...)              # connection #2
        new_following = True
    return {"is_following": new_following,
            "follower_count": follower_count(...)}   # connection #3
```

Two concurrent toggle requests from the same viewer for the same
target can land in inconsistent terminal state. The composite
primary key on `user_follows(follower_user_id, followed_user_id)`
(migration 173) prevents duplicate rows from being **stored**, so
the failure mode is a wrong returned `is_following` flag, not data
corruption.

Real-world this is mostly a double-click hazard, not an attack.
The HTMX swap reflects whichever response wins the race; a refresh
makes it consistent. **Low** because the data is correct, only the
optimistic UI may briefly lie.

---

### Info-1. Public-profile opt-in: visibility correctly gated by
`public_profile_enabled = 1` — INFO

**Location:** `queries/profile.py` `get_profile_by_handle` lines 55–68.

```sql
SELECT * FROM users
WHERE profile_handle = ? AND public_profile_enabled = 1
```

Both `/u/{handle}` and `/og/profile/{handle}` route through this
function and 404 when no row comes back (lines 196–200, 318–319 of
`profile_routes.py`). The 404 explicitly hides existence (comment on
line 199 — "Hide existence — never 403 here"). This is correct
behaviour and matches the documented opt-in semantics.

The handle regex `^[a-z0-9_]{3,20}$` is enforced **before** the DB
read (lines 195, 315), so DB-side wildcard / pattern abuse is not
reachable from these routes. Parameter binding (`?` placeholders)
prevents SQLi end-to-end.

---

### Info-2. No server-side URL fetching on the avatar surface (no SSRF) — INFO

**Location:** `api_settings_avatar` lines 447–512, `_avatar_url`
68–73, `_gravatar` 61–65.

The avatar surface accepts only multipart file uploads. There is no
endpoint that takes a URL and fetches it. `_gravatar()` constructs a
Gravatar URL but stores nothing fetched from it; the URL is rendered
client-side by the browser. `_avatar_url(user_row)` only returns
whatever string is stored in `profile_avatar_url`, never resolves it.

`profile_avatar_url` is only written from `update_avatar_url(...)`,
which is called only from `api_settings_avatar` (line 511, fixed
template `/_gateway_static/avatars/{user_id}.webp?v={ts}`) and
`api_settings_avatar_delete` (line 526, `None`). No code path lets the
user supply an arbitrary URL. **No SSRF** on this file or its
callers.

Caveat: the column is plain `TEXT` with no `CHECK` constraint. If
some future feature or direct DB write injects an arbitrary URL
(e.g., `https://evil.com/track.png?session=...`), the public profile
page would happily emit it as `<img src="…">`. The CSP allows
`img-src 'self' data: https:` (server.py line 888), so any https
URL would load. Treat this as a defensive note for future avatar
sources (e.g., Twitter/X avatar mirroring).

---

### Info-3. Bio XSS via `{{ bio }}` is blocked by render_page escaping — INFO

**Location:** `static/profile_public.html` line 42 (`{{ bio }}`),
`static/settings_profile.html` line 93 (`<textarea>{{ profile_bio }}</textarea>`),
`render_page` in `server.py` lines 2682–2688:

```python
raw_keys = {"dashboard_cards", "billing_rows"}
for key, value in context.items():
    placeholder = "{{ " + key + " }}"
    if key in raw_keys or key.startswith("raw_"):
        page = page.replace(placeholder, str(value))
    else:
        page = page.replace(placeholder, html.escape(str(value)))
```

Bio is passed as `bio=bio` (line 289) and `profile_bio=bio` (line 409),
neither prefixed with `raw_`, so both get `html.escape` applied. A bio
of `"<script>alert(1)</script>"` lands as the escaped string in both
contexts:

- `<p class="profile-hero__bio">&lt;script&gt;alert(1)&lt;/script&gt;</p>`
- `<textarea …>&lt;script&gt;alert(1)&lt;/script&gt;</textarea>`

The only raw paths that *touch* bio are:

- `raw_jsonld` — addressed in L1; defended by `json.dumps` + `</` escape.
- `raw_og` — addressed in L2; defended by `_html.escape`.

No XSS via bio is currently reachable. The defence-in-depth caveat:
the CSP (`script-src 'self' 'unsafe-inline'`) allows any inline
script that escapes the literal-string boundary, so the escaping
**must** be correct everywhere. It is, but this is a hot path: a
future contributor who passes `raw_bio=bio` by mistake would
introduce a stored-XSS bug immediately.

---

## Cross-cutting observations

- **The CSP is permissive on `script-src` (`'self' 'unsafe-inline'`).**
  This is documented elsewhere (`audit_security_headers.md`) and is
  not introduced by `profile_routes.py`, but every escaping decision
  in this file is load-bearing because of it. Tightening CSP to drop
  `'unsafe-inline'` would dramatically reduce blast radius of any
  bio/handle XSS regression.
- **Two separate connections inside `update_profile` create three
  TOCTOU/race windows in total** (handle taken check, cooldown check,
  unique-index check). The DB partial unique index is the only
  authoritative defence; the application-layer reads are advisory.
- **`/og/profile/{handle}` is not gated.** Correct by design (crawlers
  must reach OG cards), and the input is regex-validated before the DB
  read. No issue, noted for completeness.
- **Reserved handle list is enforced at the application layer only**
  (`queries/profile.py` lines 28–45). A future operator inserting a row
  directly into the DB could create a `support` / `admin` / `narve`
  handle. Out of scope for an HTTP-surface audit, mentioned for
  completeness.

## Out-of-scope but adjacent

- The cached `user_prediction_stats` row read by `_stats_for_user`
  may include statistics computed from `is_public = 0` predictions
  (depends on the recompute job in `Dashboard-x-truth-research-prediction`).
  If so, the public profile page leaks aggregate signal from private
  forecasts. Worth a separate look at how `upsert_user_prediction_stats`
  is fed.
- The `username` / `email` used for Gravatar (line 528) is sent to a
  third party (Gravatar / Automattic) via the user's browser the
  moment they view the settings page. Not a server-side leak, but a
  privacy disclosure that should be documented.

## What was NOT found

- No SSRF on the avatar surface (no URL-fetch path exists; Info-2).
- No SQLi (all parametrised; spot-checked all DB calls in this file
  and `queries/profile.py`).
- No path traversal on avatar write (file path is
  `_AVATARS / f"{user_id}.webp"`, where `user_id` is an int from the
  session).
- No bio XSS via the regular `{{ bio }}` template substitution
  (Info-3).
- No reserved-handle bypass via the documented HTTP surface (regex +
  list check happen before the write; case is normalised; the
  `RESERVED_HANDLES` set is checked after lowering).
- No handle-cooldown bypass: clearing the handle is silently a no-op
  (line 175 of `queries/profile.py`), so an attacker cannot "reset"
  the cooldown by clearing and re-setting.

---

*No code changes made (per task hard rule). Bash run synchronously.*
