# narve.ai edge-case handling

Last updated: 2026-04-22

Companion to [`BUGFIX_LOG.md`](BUGFIX_LOG.md). Lists the inputs, race
windows, and boundary conditions that have an explicit test in
[`gateway/tests/test_edge_cases.py`](gateway/tests/test_edge_cases.py).

Every row here is either **handled** (covered by a regression test)
or **accepted** (documented behaviour the business has signed off on,
not a bug). Items marked **pending** are known but not yet protected;
they're logged so the next edge-case sweep can pick them up.

---

## Phase 1 — Input matrix

The shared normaliser is [`gateway/security/input_hygiene.py`](gateway/security/input_hygiene.py).
Every handler that reads free-form text / numbers / handles should
route through `clean_text`, `clean_int`, `clean_float`, `clean_email`,
`clean_handle`. Handlers that don't yet go through the normaliser are
listed under **pending**.

| # | Input | Behaviour | Status |
|---|---|---|---|
| 1 | `""` (empty string) | Collapses to `None` unless `allow_empty=True`; raises 400 if `required=True`. | ✅ handled (`TestEmptyAndWhitespace`) |
| 2 | `"   "` (whitespace only) | Stripped → `None`. | ✅ handled |
| 3 | 10 k chars | Rejected with 400 when over `max_len`; absolute hard cap of 1 MB. | ✅ handled (`TestVeryLong`) |
| 4 | Emoji / RTL | Passes through after NFC normalisation. | ✅ handled (`TestUnicode`) |
| 5 | Zero-width / BOM / bidi control | Stripped before length check. | ✅ handled |
| 6 | Zalgo (combining marks) | Kept as valid unicode; length cap still enforced on code points. | ✅ handled |
| 7 | `' OR 1=1 --` | Passes through unchanged — SQL safety comes from parameterised queries in `db.py`, not input filtering. | ✅ accepted |
| 8 | `<script>alert(1)</script>` | Passes through unchanged — HTML escaping happens at the template boundary. | ✅ accepted |
| 9 | `\x00` null byte | Rejected 400. | ✅ handled |
| 10 | Other C0 / C1 control chars | Rejected 400. | ✅ handled |
| 11 | `../../etc/passwd` as free text | Passes through — no semantic meaning until it becomes a filename. | ✅ accepted |
| 11b | `../../etc/passwd` as handle / slug | Rejected 400 by `clean_handle` charset regex. | ✅ handled |
| 12 | Negative number where `lo=0` | Rejected 400. | ✅ handled (`TestNumbers`) |
| 13 | Zero where `lo=1` | Rejected 400. | ✅ handled |
| 14 | Decimal where int expected | Rejected 400; float integers (`5.0`) accepted. | ✅ handled |
| 15 | `"1e100"` scientific-notation string | Rejected 400 — explicit regex match against `-?\d+`. | ✅ handled |
| 16 | `NaN` / `±Infinity` | Rejected 400 in both `clean_int` and `clean_float`. | ✅ handled |
| 17 | Python bool as int | Rejected 400 — `True`/`False` are `int` subclasses in Python, easy to coerce accidentally. | ✅ handled |

### Pending inputs (logged, not yet gated)

* `gateway/server.py` — `/api/feedback` body. Currently relies on FastAPI / Pydantic limits.
* `gateway/ai_routes.py` — user prompts. Rate-limited, but no length cap.
* Outer trim of email display names (MIME header injection). Not a security path today (bot emails use fixed templates).

---

## Phase 2 — Pagination boundaries

Helpers: `security.input_hygiene.clean_page` / `clean_per_page`.

| Input | Outcome | Status |
|---|---|---|
| `page=0` | → `1` | ✅ |
| `page=-5` | → `1` | ✅ |
| `page=999_999_999` | Clamped to 10 000 | ✅ |
| `page="abc"` | → default (1) | ✅ |
| `per_page=0` | → default (20) | ✅ |
| `per_page=-1` | → default (20) | ✅ |
| `per_page=10_000` | Clamped to 100 | ✅ |
| `per_page="infinity"` | → default (20) | ✅ |

Endpoints that already clamp correctly (verified by code review):
`/api/v1/sources`, `/api/v1/sources/{handle}`, `/api/v1/predictions`,
`/api/v1/markets/edge`, `/api/markets/unified`,
`/api/markets/top-edge`, `/api/markets/false-consensus`.

### Pending pagination

* Migrate `/api/sources/following`, `/api/saved` to use `clean_page` /
  `clean_per_page` (they currently have bespoke clamps).

---

## Phase 3 — Concurrent writes / idempotency

Helper: [`gateway/security/idempotency.py`](gateway/security/idempotency.py).

Scope: subscription-critical writes. A client retry inside a 10 s
window (default) resolves to the cached first response. Redis when
available, in-process otherwise.

| Scenario | Outcome | Status |
|---|---|---|
| Same `Idempotency-Key` inside TTL | Body runs once; second call replays cached result. | ✅ handled |
| Same fingerprint (JSON body hash) when header missing | Same behaviour. | ✅ handled |
| Different user, same key/op | Isolated — each runs its own body. | ✅ handled |
| Different op, same key/user | Isolated — each runs. | ✅ handled |
| No key AND no fingerprint | Degrades open — body runs every call. | ✅ accepted (degraded mode) |
| Threaded race on same key | Collapses to ≤ 2 executions; documented as tab-race protection, not a lock. | ✅ handled within stated scope |

### Where it's wired (as of this commit)

**Not yet**. Module shipped in this commit; the callsite wiring into
`billing_routes.py` + `/api/kelly/bet` + `/api/portfolio/*` is a
follow-up PR so each integration gets a code-review pass.

---

## Phase 4 — Timezone

All storage is **integer Unix epoch seconds**. All display-layer
formatting converts to the requested TZ at render time.

| Scenario | Behaviour | Status |
|---|---|---|
| Sign-up during DST transition | Stored as epoch; no wall-clock dependency. Physical-time math verified across `Europe/Berlin` spring-forward. | ✅ (`TestTimezone.test_dst_transition_day`) |
| Epoch round-trip | `datetime.fromtimestamp(ts, tz=UTC)` → `.timestamp()` returns the original epoch. | ✅ |
| Market close time displayed in user TZ | Client-side `toLocaleString` with explicit `timeZone` option. | ✅ accepted (JS layer) |
| Historical "yesterday" labels | Anchored to user TZ when available; UTC fallback logged. | ✅ accepted |

### Pending timezone

* Add `users.preferred_timezone TEXT` in a future migration — currently
  inferred from browser `Intl.DateTimeFormat().resolvedOptions().timeZone`
  on every render.

---

## Phase 5 — Deletion cascades

Foreign-key audit ran against the live `auth.db` schema. Summary:

* User-scoped tables: 40+ rows `REFERENCES users(id) ON DELETE CASCADE`.
* Logging / audit tables: `ON DELETE SET NULL` (preserve history even
  when the acting user is deleted).
* Prediction-history: `ON DELETE CASCADE` from user → personal rows,
  but research/financial rows (e.g. `user_bet_history`) are retained
  per the account-deletion policy in `jobs/pipeline_jobs.py`.

| Scenario | Behaviour | Status |
|---|---|---|
| User deletes account | Personal rows cascade; financial/research rows anonymised. | ✅ accepted (see `process_scheduled_deletions`) |
| Market deleted | Predictions keyed on `market_id` TEXT (not FK) — predictions remain, market lookup returns 404. | ✅ accepted |
| Source deleted | No FK from `followed_sources` to `source_credibility` — orphans are filtered at read time. | ✅ accepted |
| Source handle rebrand | Existing predictions keep old `source_handle`; admin tooling exposes a rename utility. | ⚠️ pending (no tool yet; handled manually via SQL) |

### Pending cascade

* `invite_tokens.claimed_by_user_id` has no explicit `ON DELETE` —
  when a user deletes, the token row keeps a dangling FK. Cosmetic
  (the `status = 'claimed'` still reads correctly), but worth adding
  `ON DELETE SET NULL` in the next migration.

---

## Phase 6 — Subscription lapse

See [`SUBSCRIPTION_STATE_MACHINE.md`](SUBSCRIPTION_STATE_MACHINE.md)
for the full transition table.

| Scenario | Behaviour | Status |
|---|---|---|
| Stripe webhook `customer.subscription.deleted` | Revokes sessions, deactivates embed widgets, invalidates access cache, enqueues cancellation email. | ✅ (`stripe_webhook_hardening.apply_subscription_cancelled`) |
| Webhook replayed same event id | Short-circuits via `processed_stripe_events` idempotency ledger. | ✅ |
| Webhook livemode ≠ production env | Rejected 400. | ✅ |
| Refund via Stripe dashboard | Currently routes through the same `customer.subscription.updated` path. | ✅ accepted |
| Gift sender cancels own sub | Recipient's gift is untouched (separate `gifted_subscriptions` row). | ✅ accepted |
| Mid-cycle downgrade | Access reduces at period end (Stripe default); `--rank-*` badges update only after the next `subscription.updated` webhook. | ✅ accepted |

---

## Phase 7 — Races

| Scenario | Behaviour | Status |
|---|---|---|
| Two users claim last invite token | `UPDATE invite_tokens SET status='claimed' WHERE id=? AND status='unclaimed'` is single-statement; only one UPDATE returns `rowcount=1`. | ✅ accepted |
| Two admins impersonate same user | Each impersonation session has a unique cookie token; both work in parallel; audit log shows both. | ✅ accepted (not a conflict) |
| User deleted mid-request | Middleware resolves `request.state.user = None` on next request; handlers return 401 not 500. | ✅ accepted |
| Tab double-click on Subscribe | Collapsed via `with_idempotency(..., client_key=<header>)` — wiring follow-up PR. | ⚠️ module in place, wiring pending |

---

## Phase 8 — Large data

Benchmarks on live prod data (2026-04-21 snapshot):

| Scenario | Rows | Latency p95 | Status |
|---|---|---|---|
| User with 5 000 saved predictions | 5 000 | 340 ms | ✅ (paginated) |
| Source with 50 000 predictions | 50 000 | 820 ms | ✅ (index `idx_predictions_source_resolved` carries it) |
| Market with 500 signals | 500 | 180 ms | ✅ |
| Admin users list (3 000 users) | 3 000 | 620 ms | ✅ after the N+1 fix in commit `f1c095c` |

### Pending large-data

* `/api/search` with > 10 k hits currently returns the first 100 and no
  total count. Acceptable per product — search is "best match", not
  "all matches".

---

## Phase 9 — Email

| Scenario | Behaviour | Status |
|---|---|---|
| `alice+narve@example.com` | Accepted; stored lowercased. | ✅ handled |
| Mixed-case local part | Lowercased on ingest; idempotent against case drift. | ✅ handled |
| Unicode in local part (`bøb@example.com`) | Accepted — not IDN-normalised, but passes regex. | ⚠️ latent (never seen in prod) |
| Multiple users typo to same address | UNIQUE index on `users.email` lowercased — second signup blocked. | ✅ handled |
| Whitespace in address | Rejected 400 by `clean_email`. | ✅ handled |

---

## Running the tests

```bash
cd gateway
python3 -m pytest tests/test_edge_cases.py -v
```

Expect all 54 tests to pass. If a new edge case is discovered, add
the regression here **first** (fail → fix → green), then update the
relevant table row in this file.
