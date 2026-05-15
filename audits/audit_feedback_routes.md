# Adversarial Audit — `gateway/feedback_routes.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7)
Target: `/Users/shocakarel/Habbig/gateway/feedback_routes.py`
Supporting layers reviewed:
`/Users/shocakarel/Habbig/gateway/security/input_hygiene.py`,
`/Users/shocakarel/Habbig/gateway/security/csrf.py`,
`/Users/shocakarel/Habbig/gateway/server.py` (`render_page`, `_is_rate_limited`,
`current_user`),
`/Users/shocakarel/Habbig/gateway/migrations/130_feedback.py`,
`/Users/shocakarel/Habbig/gateway/static/feedback.html`,
`/Users/shocakarel/Habbig/gateway/static/feedback-detail.html`,
`/Users/shocakarel/Habbig/gateway/static/admin/feedback.html`.

Scope was tightly bound to the five attacker classes named in the brief:

1. Anonymous-submit spam / submission rate-limit
2. Comment XSS via `raw_` template injection
3. Vote-stuffing — multiple votes per user, count inflation, race
4. Admin moderation guards on every `/admin/feedback/*` endpoint
5. Attachment validation (none exists — see INFO-1)

Out of scope: notification rendering (separate `notification_routes.py`),
billing / subscription gate correctness (deferred to `_user_plan_info`),
the floating "Feedback" button JS, CSRF middleware itself (covered by
`CSRF_AUDIT.md`).

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 4 |
| Low      | 5 |
| Info     | 3 |
| **Total**| **12** |

## Top 3 findings (ranked by exploitability × impact)

1. **MED-1** — Submission rate-limit silently no-ops when the helper raises a
   non-HTTP exception. The handler wraps `server._is_rate_limited(...)` in a
   bare `except AttributeError: pass` (lines 553-554). If the Redis path
   raises `AttributeError` *inside* the helper (e.g. on a pipeline against a
   stub during a Redis hot-swap), the limiter is bypassed for that request
   while the comment claims the fallthrough is "Older server.py without
   the helper". Attacker doesn't need to trigger this directly — any
   intermittent Redis client breakage opens a several-minute spam window.
   In addition, `FEEDBACK_RATELIMIT_DISABLED=1` is documented as a test-only
   flag, but the gate is `!= "1"` — anything that defeats env hygiene
   (sub-process inheritance of `1`) turns it off in prod with no audit log.

2. **MED-2** — Comment endpoints (`POST /api/feedback/{id}/comment` and
   `POST /admin/feedback/{id}/comment`) bypass `clean_text`. Body goes
   through `(body or "").strip()[:2000]` only — no control-character reject,
   no NFC normalisation, no zero-width / bidi strip, no null-byte guard.
   This **does not yield XSS** today because `_load_comments`-rendered body
   is escaped via `html.escape(com["body"])` at line 430, but the
   inconsistency is dangerous: any future template that interpolates
   `body` into a `raw_` slot, an HTML email digest (`jobs/feedback_digest.py`
   already reads these rows), or a JSON-LD block will be raw-passed.
   Submit went through `clean_text` (line 568-575); comments did not.
   The bidi-flip attack (RLO/LRO) lands here unblocked and would render
   reversed in any non-`html.escape` consumer — including the admin
   triage page if a future change inlines comment previews. Also note
   the 2000-char cap is measured **after** `.strip()` but **before**
   anything else; an attacker can pad with zero-width chars to push the
   visible-glyph count above 2000 with no rejection.

3. **MED-3** — Admin moderation endpoints have **zero rate limit and no
   idempotency guard**. `_require_admin` is the only gate. A compromised
   admin session (or an inside-threat admin) can:
   - flip status on every existing item via `/admin/feedback/bulk-status`
     in a single multipart POST containing `ids=` repeated thousands of
     times — each id triggers a `_notify_submitter()` write to
     `notifications`, fanning out an arbitrarily-large notification
     storm with no cap; the loop has no `LIMIT` (lines 897-912).
   - mark every public item shipped with a forged `sha=` value of up to
     64 chars of attacker-controlled text via `/admin/feedback/{id}/ship`
     (line 974). No hex/length validation. The 7-char prefix is then
     displayed on `/feedback` (line 278-279). The display is
     HTML-escaped so it can't XSS, but a malicious admin can poison
     every shipped item's "commit" with garbage that defames a coworker
     ("FIRED01", "RACIST", etc.) — visible to every logged-in user.

---

## Findings

### MED-1 — Submission rate-limit fails open on AttributeError / env flag

**Location:** `feedback_routes.py:543-554`.

```python
import os as _os
if _os.environ.get("FEEDBACK_RATELIMIT_DISABLED") != "1":
    try:
        if server._is_rate_limited(
            f"feedback-submit:{user['user_id']}",
            limit=10, window=3600,
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many submissions. Try again in an hour.",
            )
    except AttributeError:
        pass  # Older server.py without the helper — fall through.
```

**What:** Two independent failure modes:

1. `except AttributeError: pass` catches more than the documented case.
   `server._is_rate_limited` exists in this tree (`server.py:1633`) but
   calls `_is_rate_limited_redis` which calls `_redis_client.pipeline()`.
   If `_redis_client` becomes `None` between the
   `if _redis_client is not None` check (line 1639) and the pipeline call
   (very narrow race in a worker reload, or in a forked subprocess) — or
   if any future refactor inlines a `getattr(... , ...)` against a
   `None`-able global — the `AttributeError` propagates from the helper
   and the rate limit silently no-ops. Spam window: the duration of
   whatever transient condition triggered it.
2. The env-flag check uses `!= "1"`. A bash subshell that inherited
   `FEEDBACK_RATELIMIT_DISABLED=1` (e.g. from a CI runner spawned via
   `os.execve`) carries the bypass into production if anyone forgets
   to scrub the environment. No audit log entry is emitted when the
   flag is in effect — the route looks identical from the response side.

**Attack:**

A single authenticated user fills the admin triage inbox at >>10/hour
the moment either condition is in effect. The submission flow
log-lines (`feedback submitted uid=… id=…`) accumulate but no signal
distinguishes "limit honoured" from "limit bypassed".

**Fix:**

1. Catch a narrower exception (or none) around the call. The
   "older server.py" claim is no longer true on this tree — the helper
   is committed. Make it a hard call.
2. Log an `WARN`/`audit` entry whenever `FEEDBACK_RATELIMIT_DISABLED=1`
   is honoured in a non-test environment (e.g. `if ENV != "test":
   log.warning(...)`).
3. Consider adding a *second* per-IP cap (`f"feedback-submit-ip:{ip}"`)
   on top of the per-user cap so a credential-stuffed multi-account
   spammer is still throttled per-source.

**Severity rationale:** MED, not HIGH — the limit is set at 10/hour
which is high enough that the spam ceiling without the bypass is
already lenient, and admin moderation absorbs whatever lands. Bumps
to HIGH if any downstream of `feedback_items` (notifications fan-out,
digest job) becomes a DoS amplifier.

---

### MED-2 — Comment body never sees `clean_text`; padding + bidi unblocked

**Location:** `feedback_routes.py:686-708` (user comment),
`feedback_routes.py:942-968` (admin comment).

```python
body_clean = (body or "").strip()[:2000]
if not body_clean:
    raise HTTPException(status_code=400, detail="Comment cannot be empty")
```

**What:**

- No `clean_text` invocation. The submit path uses
  `clean_text(title, max_len=200, ...)` and `clean_text(body, max_len=4000)`
  (lines 568-575) which strips zero-width glyphs, bidi controls,
  rejects C0/null, and NFC-normalises. Comments skip all of this.
- The 2000-char cap is applied **before** invisible-glyph stripping.
  Zero-width / BOM / bidi codepoints count toward the cap, so an
  attacker can pack `2000 - N` zero-width chars in to push the visible
  glyph count over 2000 in any consumer that strips them later.
- A null byte (`\x00`) reaches the DB intact. SQLite stores it. Any
  downstream consumer that round-trips through a C string (e.g. an
  embedded export pipeline, libxml, the json digest job) truncates at
  the null.
- Bidi attack: an admin response containing `RLO` (U+202E) reads
  legitimately in the admin compose textarea but renders the rest of
  the line right-to-left in `feedback-detail.html`. `html.escape` does
  not escape RLO/LRO. The detail page's `white-space:pre-wrap` faithfully
  reproduces the visual flip.

**Is it XSS today?** No. `_load_comments` rendering at line 430 wraps
the body in `html.escape(com["body"])`. The bidi reordering is a
**visual** spoof, not script execution.

**Why MED, not LOW:**

- `jobs/feedback_digest.py` reads `feedback_comments.body` directly
  (confirmed at `grep -l feedback_comments`); any HTML email digest
  that uses an `{body}` substitution with markdown rendering would
  pick up the un-escaped, un-normalised content. The submit path
  already protected its title/body; comments are the gap.
- The admin comment endpoint is the channel teams use to publicly
  respond. A malicious admin (or hijacked admin session) can deface
  every public item's "Team response" with `RLO`-flipped or
  zero-width-padded text. Item title is escaped; the **shown content**
  is the admin-controlled comment.
- The author of the original submission gets the bidi-flipped excerpt
  in their `notifications` row (line 132: `extra["excerpt"]`) — and
  the notifications panel rendering is a separate module that this
  audit didn't scope. If `notification_routes.py` ever interpolates
  excerpt into a raw HTML field, that becomes the live XSS sink.

**Fix:**

Route both comment bodies through
`clean_text(body, max_len=2000, required=True, field="body")`. Same
call signature as the submit path. The admin path benefits less from
the bidi strip (we trust admins to not RLO themselves) but the
consistency keeps the next reviewer from having to remember which
endpoints are hardened and which aren't.

---

### MED-3 — Admin moderation endpoints have no rate limit; bulk-status loop is unbounded; `sha` is not validated

**Location:**

- `feedback_routes.py:830-865` (`admin_feedback_status`)
- `feedback_routes.py:868-917` (`admin_feedback_bulk_status`)
- `feedback_routes.py:920-939` (`admin_feedback_duplicate`)
- `feedback_routes.py:942-968` (`admin_feedback_comment`)
- `feedback_routes.py:971-990` (`admin_feedback_ship`)

**What:**

1. **No `_is_rate_limited` on any admin route.** The codebase's pattern
   is `if _is_rate_limited(f"admin_bulk:{admin['email']}", 10): raise 429`
   (e.g. `server.py:6203` for admin bulk operations). Feedback admin
   endpoints don't apply this. A compromised admin session (or an
   inside threat) can hammer `admin_feedback_status` thousands of times
   per second, each call writing a row into `notifications` for every
   non-self submitter.
2. **`bulk-status` has no `len(item_ids)` cap.** `form.getlist("ids")`
   returns every `ids=` value in the multipart body — there is no
   ceiling. A POST with 100k ids (each a small integer, easy to fit
   under any reasonable body-size limit) runs 100k SELECTs and 100k
   UPDATEs in a single transaction (single `with db.conn() as c`),
   blocking the SQLite write lock for whoever else is writing. The
   loop also calls `_notify_submitter()` for every id that resolves,
   fanning out 100k notification inserts.
3. **`ship` accepts arbitrary `sha`.** The handler does
   `sha_clean = (sha or "").strip()[:64] or None` (line 974). No hex
   regex, no length floor. The displayed 7-char prefix is HTML-escaped
   so it can't XSS, but a malicious admin can write "FRAUD01" or any
   text up to 64 chars into every shipped item's commit field —
   visible on the public `/feedback` list (line 278-279).
4. **`duplicate_of` is not checked against `item_id`.** Line 933 will
   happily set `duplicate_of = item_id` (item marked dup of itself).
   The list rendering shows " · dup of #N" (line 745); a self-dup
   silently displays " · dup of #N" against item N. Annoyance not
   exploit, but a hint of insufficient validation across the
   admin surface.

**Attack scenarios:**

- *Inside threat:* admin marks every existing item shipped with
  `sha="FIRED01"`, defaces the public roadmap for the next 5 minutes
  until another admin reverts (no undo built in).
- *Stolen admin session:* attacker calls `/admin/feedback/bulk-status`
  with `status=declined` and `ids=` enumerating 1..100000 (silent
  skip-on-miss means no error). All open feedback closes in a single
  request. The notification fan-out generates one row per author per
  item — easy to scale into the millions.
- *Notification DoS:* combine bulk-status with the fan-out: each
  status flip notifies a real user. Spam every active customer with
  10+ "Feedback status updated" notifications, then the notifications
  service starts dropping legitimate events.

**Fix:**

1. `_is_rate_limited(f"admin_feedback:{admin['user_id']}", limit=30, window=60)`
   on every admin POST. Same key shape as the other admin endpoints.
2. Cap `len(item_ids)` at e.g. 100 in `admin_feedback_bulk_status`;
   return 400 above. The triage UI cannot reasonably select more
   than a page of items at once anyway.
3. Validate `sha` with `re.fullmatch(r"[0-9a-fA-F]{7,40}", sha_clean)`
   and reject otherwise. Match git's actual SHA format.
4. Reject `duplicate_of == item_id` with a 400 in `admin_feedback_duplicate`.

---

### MED-4 — Submit path does not deduplicate; `q` similar-search returns even if attacker spams identical titles

**Location:** `feedback_routes.py:524-611` (submit), `492-521` (search).

**What:**

The submit path has no "is this title already submitted by you in the
last hour" check. The 10/hour user cap (MED-1) is the only barrier
to filling the inbox with 10 identical bug reports. The `q` search
exists explicitly to nudge users away from dups (line 494) but is a
client-side hint only — the server doesn't enforce it.

This isn't a critical security issue but is the difference between an
admin inbox that's usable and one that's drowning in dup noise after
one frustrated user mass-clicks the submit button.

**Attack:** Authenticated user submits 10 identical "BUG: page broken"
items in 5 minutes. All pass; all show as 10 separate rows in the
admin triage. Admins must close them one by one (or use bulk-status,
which is itself unsafe — see MED-3).

**Fix:**

Before insert, run a single-row SELECT for `feedback_items WHERE user_id = ?
AND title = ? AND created_at > datetime('now', '-1 hour')` and 409 on
match. The shape mirrors how feedback intake works at every issue
tracker (Linear, Jira).

---

### LOW-1 — `is_public` flag-flipping not gated by submission ownership

**Location:** `feedback_routes.py:578` (submit), `feedback_routes.py:411-414` (detail visibility).

**What:** A user can submit a "private" item, then there is **no route
to re-publish or re-privatise it later**. So `is_public` becomes a
write-once flag at submission. That's fine — but the audit notes the
absence:

- An admin **can** change status, duplicate, ship, and comment on a
  private item but **cannot** flip `is_public` from the UI. Migration
  130 makes `is_public` editable in the DB; no route does. Means a
  legitimate "user mistakenly marked private" path is missing.
- Conversely, no route to flip `is_public` from `1` to `0` after
  submission — so a user can't redact a sensitive submission they
  realised they shouldn't have made public. Not a security bug;
  a *security-UX* gap worth filing.

**Fix:**

Add `POST /api/feedback/{id}/visibility` (owner or admin) that toggles
`is_public`. Same CSRF / rate-limit story as the other endpoints.

---

### LOW-2 — Vote race window between SELECT and INSERT

**Location:** `feedback_routes.py:614-660` (`api_feedback_vote`).

**What:** The vote logic is:

```python
existing = c.execute(
    "SELECT 1 FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
    (user["user_id"], item_id),
).fetchone()
if existing:
    DELETE; UPDATE upvotes = MAX(0, upvotes - 1)
else:
    INSERT; UPDATE upvotes = upvotes + 1
```

Within a single SQLite connection (`with db.conn() as c`) this is
atomic; SQLite serializes writes. **But** the per-row sequence is
read-modify-write and not wrapped in `BEGIN IMMEDIATE`. Two
concurrent requests from the same user hitting `/vote` in parallel
**could** both observe `existing=None` in their SELECT, then both
INSERT — and the composite PK `(user_id, feedback_id)` (migration 130
line 86) will fail on the second INSERT with `IntegrityError`. Result:
one request 500s. The first INSERT plus its `upvotes + 1` does
succeed, so the **count is consistent**, but the user sees a 500.

If SQLite were swapped for Postgres without re-checking, the race
becomes lossier (no PK enforcement → double-vote possible if no
UNIQUE constraint added).

**Why LOW:** the composite PK prevents the actual vote-stuffing
attack. Worst case is a 500 the user can retry past. Documented here
because the comment block at line 224-229 already calls out a static
analysis flag on the SELECT/INSERT pattern; this is its sibling.

**Fix:**

Either:
1. `INSERT … ON CONFLICT DO NOTHING` and read `c.rowcount` to detect
   the prior-vote case, or
2. Wrap the whole block in `BEGIN IMMEDIATE` so the read locks the row.

The bigger win is option 1 — one round-trip instead of three.

---

### LOW-3 — `upvotes` counter drift if INSERT races with `UPDATE`

**Location:** Same as LOW-2.

**What:** The vote handler updates `feedback_items.upvotes` separately
from inserting the row in `feedback_votes`. If SQLite were to roll back
the `INSERT` for some reason (disk full, FK constraint), the `UPDATE
upvotes = upvotes + 1` is still in flight in the same transaction —
fine for SQLite's auto-rollback. But the rollback is **silent** to the
caller: the handler returns `voted=True, upvotes=new_count` to the
client even though the DB rolled back. The `_user_has_voted` call on a
subsequent page load would correctly return `False`, exposing the
desync to the user as a "you weren't voted after all" jitter.

**Fix:** Don't trust the in-memory `voted` flag past the `with` block —
re-read after commit. Or simpler: derive `voted` from a final SELECT
on `feedback_votes`.

---

### LOW-4 — `/feedback/{id}` private-item check leaks "not found" for both "doesn't exist" and "no permission"

**Location:** `feedback_routes.py:404-414`.

```python
if not row: raise HTTPException(status_code=404, detail="Feedback item not found")
item = dict(row)
if not item["is_public"]:
    is_owner = item["user_id"] == user["user_id"]
    if not (is_owner or user.get("is_admin")):
        raise HTTPException(status_code=404, detail="Feedback item not found")
```

The handler intentionally returns 404 for both "doesn't exist" and
"private and not yours". This is **correct** behaviour for not leaking
existence. Documenting here as INFO so future refactors don't
"helpfully" convert one of the branches to 403, which would be a
regression.

**Severity:** LOW because the behaviour is fine **today**; the risk
is regression by a well-meaning future contributor.

**Fix:** Add a `# NOTE: deliberate 404 for both branches — see audit LOW-4`
comment so the intent doesn't get lost.

---

### LOW-5 — `_list_items` `q` parameter is lowercased and LIKE'd but not LIKE-escaped

**Location:** `feedback_routes.py:210-214`.

```python
if q:
    q_clean = q.strip()[:60]
    if q_clean:
        where.append("LOWER(title) LIKE ?")
        params.append(f"%{q_clean.lower()}%")
```

**What:** `q_clean` is interpolated **as a parameter** (no SQL injection)
but its content is taken verbatim into the LIKE pattern. SQL `LIKE`
treats `%` and `_` as wildcards. A user passing `q=%` matches every
row. `q=_` matches every single-character title. Performance, not
correctness — but `LOWER(title) LIKE '%%%%%%...'` on a million-row
table is a free DoS vector.

Mitigations already in place:
- `[:60]` limits pattern length.
- `len(q_clean) < 3` early-return in `api_feedback_search` (line 508)
  blocks 1- and 2-char patterns from the modal hint.

But `_list_items` is also called from `/feedback` and `/admin/feedback`
where `q` is **not** sourced — except `feedback_list_page` doesn't pass
`q=...` at all (line 321-328). So the LIKE-wildcard issue is gated
inside `api_feedback_search`, which already requires `len >= 3`. Still
worth escaping `%`/`_` in `q_clean` defensively before the LIKE:
`q_clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")`
and add `ESCAPE '\\'` to the LIKE.

**Severity:** LOW because the path that exposes `q` enforces a 3-char
floor and a 60-char ceiling and a `LIMIT 3` (line 510), so worst-case
scan is bounded. Bumps to MED if any future route forwards `?q=`
into `_list_items` without those guards.

---

### INFO-1 — No attachment endpoints exist on this surface

**Location:** entire file.

The brief asked for attachment validation. There are **no attachment
fields, no upload endpoints, and no presigned-URL flows** in
`feedback_routes.py` or in migration 130. `feedback_items` has
columns `(id, user_id, type, title, body, status, upvotes, ...
admin_note, shipped_commit_sha, duplicate_of, is_public)`. No
`attachment_url`, no `image_id`, no `file_id`.

If/when attachments are added, the existing patterns to follow are:
- `api/uploads_routes.py` (presumed; check `gateway/api/`).
- `security/input_hygiene.py` does **not** today validate image
  uploads — content-type sniffing is enforced elsewhere.

**Action:** None for this audit. Filed as INFO so the brief item is
formally closed.

---

### INFO-2 — `_user_plan_info` is a transitive dependency for vote eligibility

**Location:** `feedback_routes.py:81-90` (`_is_subscriber`).

`_is_subscriber` is the only vote-eligibility check. It calls
`db.list_subscriptions(user["user_id"])` and `_user_plan_info` from
`server`. If either changes its return shape (`pinfo.get("plan")`
becoming falsy on legitimate paid plans, or `list_subscriptions`
silently returning `[]` on a DB error), every paid user becomes
"free" and the vote flow returns 402. Worse, if the inverse happens
(any error returns a truthy `plan`), every free user can vote and
the vote-stuffing protection collapses.

The `except Exception: return False` (line 89-90) at least fails
**closed** for the error case, which is the right direction. But
no test asserts that the fallback is reached. Add a
`@patch("db.list_subscriptions", side_effect=Exception)` test that
asserts a 402.

**Severity:** INFO. Worth a test, not a code fix.

---

### INFO-3 — `_notify_submitter` writes raw user-controlled excerpt into `notifications.body`

**Location:** `feedback_routes.py:128-152`.

`body = (extra or {}).get("excerpt") or ...` (line 132) — `excerpt` is
admin-supplied (line 964: `body_clean[:120]`). The notifications row
then renders via whatever `notification_routes.py` does. Cross-cutting
risk: if the renderer ever stops escaping `body`, this becomes a stored
XSS (admin → all submitters). Out of scope for *this* file but
flagged so the next audit of `notification_routes.py` knows the
sink exists.

---

## Cross-check against the brief

| Brief area | Findings |
|---|---|
| Anonymous-submit spam rate-limit | MED-1, MED-4 |
| Comment XSS via raw_ | MED-2 (latent, not active today) |
| Vote-stuffing | LOW-2, LOW-3 (composite PK prevents actual stuffing; only edge-case 500s) |
| Admin moderation guards | MED-3, LOW-1 |
| Attachment validation | INFO-1 (no attachments exist) |

## Verification commands

Pure read-only — run from `/Users/shocakarel/Habbig/gateway/`:

```bash
# Confirm composite PK on feedback_votes (vote-stuffing primary defence)
grep -n "PRIMARY KEY(user_id, feedback_id)" migrations/130_feedback.py

# Confirm comment body bypasses clean_text (MED-2)
grep -n "clean_text\|body or" feedback_routes.py | head

# Confirm no rate-limit call on any admin handler (MED-3)
awk '/^async def admin_feedback_/,/^@app.post|^@app.get|^# /' feedback_routes.py \
  | grep -c _is_rate_limited     # expect 0

# Confirm sha is not validated (MED-3)
grep -n "sha_clean\|shipped_commit_sha" feedback_routes.py
```

## Sign-off

No code changes made by this audit. All findings are static review of
the file as committed at `feature/platform-build` HEAD.
