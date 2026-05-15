# Adversarial audit — `gateway/db.py`

**Scope:** `gateway/db.py` (1527 lines — note: spec said ~4500, file is materially smaller because most query code has been re-exported from `queries/*` modules since the split). Bottom ~500 LoC (1028–1527) audited with extra care since it contains the recently added API-key, webhook, and system-secret code.

**Date:** 2026-05-15
**Auditor focus:** f-string SQL, `sqlite3.Row.get()` misuse, `PRAGMA foreign_keys` consistency, transaction scoping with `conn()`, UNIQUE-column races, PBKDF2 iterations ≥ 600k, connection-pool leaks.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0     |
| High     | 1     |
| Medium   | 4     |
| Low      | 5     |
| Info     | 3     |

No SQL injection, no plaintext credentials, no `Row.get()` misuse, no f-string SQL anywhere. PBKDF2 iteration count is correct at 600k (modern) with 200k accepted only for opportunistic rehash. The dominant risks are concurrency-related: missing WAL/busy_timeout, an exception-path commit/rollback gap in the connection context manager, and a few small TOCTOU windows.

---

## Top 5 findings

### 1. [HIGH] `conn()` context manager has no `rollback()` on exception, no `busy_timeout`, no `journal_mode=WAL`
**Location:** `gateway/db.py:257–266`

```python
@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()
```

Three independent problems in the most-used function in the file:

- **No `except`/`rollback`.** If the `yield`ed block raises, control jumps to `finally` and `c.close()` runs without an explicit `c.commit()` (good) — but Python's `sqlite3` will *also* not auto-rollback uncommitted DML in some edge cases (it relies on the connection going out of scope). Worse, `c.close()` on an open implicit transaction is implementation-defined: on CPython it does an internal rollback for you, but this is brittle behavior to depend on, especially since multiple writers can be blocked behind it. An explicit `except: c.rollback(); raise` is the safe pattern.
- **No `PRAGMA busy_timeout`.** SQLite default is 0 ms — concurrent writers (e.g. two webhook-delivery workers + the request thread) instantly fail with `OperationalError: database is locked` instead of waiting. Recommend `c.execute("PRAGMA busy_timeout = 5000")` on every connection.
- **No `PRAGMA journal_mode = WAL`.** With rollback-journal mode (default), every writer locks the entire database against readers. WAL allows concurrent reads during a write transaction and is the standard recommendation for any multi-threaded SQLite app. This is set at the database level (not per-connection) so it would need to be done once at `init_db`.

**Why HIGH and not MEDIUM:** every single `with conn() as c:` in the file relies on this primitive. Under sustained webhook-delivery load (background worker) + request load (API serving), "database is locked" errors are essentially guaranteed eventually with default 0 ms timeout. The audit log, rate limit table, and webhook DLQ all suffer.

---

### 2. [MEDIUM] `bump_api_usage` legacy fallback has UPSERT TOCTOU race
**Location:** `gateway/db.py:1160–1194`

The primary path uses `INSERT … ON CONFLICT DO UPDATE` — atomic and correct. The fallback (for sqlite < 3.24):

```python
except sqlite3.OperationalError:
    cur = c.execute("UPDATE … WHERE api_key_id = ? AND hour_bucket = ?", …)
    if cur.rowcount == 0:
        c.execute("INSERT INTO api_usage_hourly … VALUES (?, ?, 1)", …)
```

Under concurrent calls for the same `(api_key_id, hour_bucket)`:
1. Both threads run `UPDATE` and both see `rowcount == 0`.
2. Both run `INSERT`. The composite PRIMARY KEY on `(api_key_id, hour_bucket)` (from `migrations/128_api_keys_ext.py:38`) means the second `INSERT` raises `sqlite3.IntegrityError` and the request 500s.

Realistic exposure is low (modern Pythons ship sqlite ≥ 3.31), but if you ever land on an old Alpine/musl host or someone strips features, you'd get rate-limit-counter loss + unhandled 500s. Either drop the fallback or wrap it in `try/except IntegrityError` + retry the UPDATE.

---

### 3. [MEDIUM] `webhook_subscriptions` has no UNIQUE on `(user_id, url)`
**Locations:** `gateway/db.py:1218–1234` (insert path) and `gateway/migrations/129_webhooks.py:28–41` (schema)

`create_webhook_subscription` lets the same user register the same URL N times. Each one then fires on the same event, which means:

- N× outbound HTTP traffic (the destination might rate-limit or block the source).
- N× rows in `webhook_deliveries` per event, exploding the table.
- An attacker who has compromised a user account can blast their own URL with thousands of subscriptions then delete just one and leave the rest hidden.

Adding `UNIQUE(user_id, url) WHERE is_active = 1` (partial unique index) preserves the ability to re-create a deleted one but blocks active duplicates.

---

### 4. [MEDIUM] `_fts_sanitize_query` is correct but the surrounding `MATCH` interface is fragile
**Location:** `gateway/db.py:603–622`

The sanitizer correctly escapes `"` → `""` and wraps each term in quotes — this is the right FTS5 escape. **But:** any caller that bypasses this helper and builds a `MATCH ?` parameter directly with raw user input *will* hit FTS5 syntax errors and (more importantly) can craft prefix-match queries that scan more index than intended. The helper is private (`_fts_sanitize_query`) but the call-site discipline lives across `queries/markets.py`, `queries/sources.py`, `queries/predictions.py` etc. — none of those are forced through this helper by the type system.

Recommend a `MatchQuery` wrapper class or an assertion in every `MATCH ?` call site. This isn't a SQLi (parameter binding still works) but the lack of a single chokepoint is a latent footgun. Look in particular at `queries/markets.search_markets`, `queries/sources.search_sources`, `queries/predictions.search_predictions`.

---

### 5. [MEDIUM] No explicit transaction wrapping for multi-statement operations in `init_db`
**Location:** `gateway/db.py:269–595`

`init_db()` does ~80 sequential `c.execute(...)` calls inside one `with conn() as c:` — every CREATE TABLE, every ALTER TABLE, every CREATE INDEX, every backfill UPDATE. Python's sqlite3 module runs these in **deferred-transaction mode** with implicit `BEGIN`s inserted before DML, but `CREATE TABLE` and `CREATE INDEX` are DDL and not wrapped in the same implicit transaction. If the server crashes halfway through a fresh-DB bootstrap, you can end up with a partially-applied schema — some tables exist with `default_dashboard` column, others don't, FTS triggers reference tables that haven't been created yet.

The risk window is narrow (`init_db()` runs once at boot), but the consequence is "schema in an undefined state and the next boot will silently keep using it". Recommend `c.execute("BEGIN IMMEDIATE")` + explicit `COMMIT` at the end. Combined with finding #1, this also fixes the "raise during init aborts the connection without rolling back" hole.

---

## Detailed findings

### 6. [LOW] `system_secret_meta` decrypts twice on the success path
**Location:** `gateway/db.py:1493–1520`

The function decrypts the secret just to compute `len(plain)`, then discards it. If `decrypt_token` is slow (Fernet involves HMAC), and an admin lists multiple secrets, the rendering cost adds up. Storing `value_length` as a separate column (or trusting the ciphertext length minus the Fernet overhead) avoids ever decrypting in the metadata path. Also: the decrypted plaintext lives briefly in Python memory and isn't zeroed — `secrets`-like hygiene would be nice but isn't a real attack surface here.

### 7. [LOW] `list_webhook_dead_letter` builds SQL by string concatenation
**Location:** `gateway/db.py:1423–1438`

```python
q = ("SELECT d.*, w.url … LEFT JOIN users u ON u.id = w.user_id ")
if not include_requeued:
    q += "WHERE d.requeued_at IS NULL "
q += "ORDER BY d.first_failed_at DESC LIMIT ?"
```

The concatenated fragments are hardcoded literals (not user data), so this is safe. But the pattern is a foot-gun magnet — a future contributor adding `if filter_for_user:` could trivially turn it into `q += f"AND user_id = {uid}"`. Recommend either a static SQL string with `WHERE (? OR d.requeued_at IS NULL)` parameter trick, or two separate query strings.

### 8. [LOW] `get_api_key_by_hash` uses `SELECT *`
**Location:** `gateway/db.py:1153–1157`

`SELECT *` couples the call site to schema migration order. If migration 180 adds a column the API middleware doesn't expect, no breakage; if migration 200 *removes* a column the middleware reads via `row["expected_col"]`, you get `IndexError` at request time. Convention in the rest of the file is explicit column lists. Same applies to `list_webhooks_for_user` (1238), `get_webhook_subscription` (1258), `list_active_webhooks_for_event` (1292), `get_webhook_dead_letter` (1415).

### 9. [LOW] No `purge` for `webhook_deliveries` or `webhook_dead_letter`
**Location:** Schema + missing function

`webhook_deliveries` is append-only with no retention bound visible in db.py. Over 6+ months of webhooks at any volume this table dominates the auth.db size and slows `LEFT JOIN webhook_subscriptions` in `list_webhook_dead_letter`. Not a security bug, but the absence of a `purge_old_webhook_deliveries(before_ts)` helper is conspicuous when `purge_expired_sessions`, `purge_expired_resets`, `purge_expired_email_otps` all exist in the auth module.

### 10. [LOW] `record_webhook_delivery` takes raw `status_code` / `error` types
**Location:** `gateway/db.py:1307–1323`

The signature accepts `status_code` and `error` without type hints (`status_code` could be `int`, `None`, or anything; `error` is `None` default and otherwise stringly-typed). SQLite will happily store any type, so a caller passing a dict will silently get `<dict object at 0x…>` in the column. Tighten the signature to `int | None` / `str | None`.

### 11. [INFO] `from backend.markets.encryption import …` inside two functions
**Location:** `gateway/db.py:1464, 1486, 1510`

The import is intentionally lazy (comment on line 750 hints at this) so processes that don't use system secrets don't pay the Fernet import cost. Fine, just noting the pattern is repeated three times — a module-level lazy property would be more DRY but doesn't matter.

### 12. [INFO] `_db_override` parsing happens at module import time
**Location:** `gateway/db.py:14–20`

`GATEWAY_DB_PATH` is read once when `db` is imported and frozen into module state. This is fine and intentional, but means dynamic tests can't repoint the DB without `importlib.reload(db)`. Already works in practice (the test suite uses `monkeypatch.setenv` before first import) — just flagging that any in-process DB-switching helper would need careful sequencing.

### 13. [INFO] PBKDF2 iterations and verification path are correct
**Location:** `gateway/queries/auth.py:21–26`

```python
PBKDF2_ITERATIONS = 600_000        # OWASP 2023+ recommendation
PBKDF2_LEGACY_ITERATIONS = 200_000  # accepted only for rehash upgrade
```

`_hash_password`, `verify_password`, `password_needs_rehash` all behave correctly: new hashes use 600k, legacy 200k hashes are accepted on login and the caller is supposed to re-hash. SHA-256 + URL-safe salt, 32-byte derived key. Meets the audit bar.

---

## What I checked and did NOT find

- **f-string / `%s` SQL anywhere in db.py:** None. The two `f"…"` occurrences (line 294 username backfill, line 621 FTS quote wrapping) are not SQL fragments — line 294 is a default username string, line 621 builds a literal *value* that is then passed as a bound parameter elsewhere.
- **`sqlite3.Row.get()` calls:** None. All row access is `row["col"]` or `row[N]`, which is correct (`sqlite3.Row` is dict-like for `__getitem__` but has no `.get()` method — calling it would raise `AttributeError`).
- **Missing `PRAGMA foreign_keys`:** Set in `conn()` itself, so every connection enforces it. The DDL with `REFERENCES … ON DELETE CASCADE` actually works.
- **Connection leaks:** None. Every `conn()` is used as `with conn() as c:` and the `finally: c.close()` is unconditional. No bare `conn()` calls escape.
- **Transaction-scope misuse with bare `c.execute` outside `with conn()`:** None — every `c.execute` is inside the context.
- **Row data crossing a closed connection:** A couple of functions call `.fetchall()` *inside* `with conn() as c:` and return the list (correct — rows are detached once fetched), e.g. `list_webhooks_for_user`. None return a cursor or `c.execute(...)` directly without `.fetchall()` / `.fetchone()`.
- **UNIQUE-column races on the critical paths I examined:** `users.username`, `users.email`, `invite_tokens.token`, `sessions.token`, `system_secrets.key` are all PRIMARY KEY or `UNIQUE NOT NULL` — the SQLite engine rejects concurrent duplicate inserts. The notable gap is `webhook_subscriptions(user_id, url)` (finding #3).

---

## Recommended ordering for fixes

1. Add `PRAGMA busy_timeout = 5000` + `PRAGMA journal_mode = WAL` (one-time at `init_db`) to `conn()`. Single biggest reliability win.
2. Add `except: c.rollback(); raise` to `conn()`. Five lines, removes a silent-failure mode.
3. Add `UNIQUE(user_id, url) WHERE is_active = 1` to `webhook_subscriptions`.
4. Wrap `bump_api_usage` legacy fallback in `try/except IntegrityError` (or just delete it — sqlite 3.24 is from 2018).
5. Replace `SELECT *` with explicit columns in the five webhook helpers.

Findings #4, #6, #7, #9–#12 are polish-grade and can be deferred to a "DB hygiene" sweep.
