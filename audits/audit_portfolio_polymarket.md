# Adversarial Audit — `gateway/portfolio/polymarket.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M ctx)
Primary target: `/Users/shocakarel/Habbig/gateway/portfolio/polymarket.py` (327 LOC)
Brief: rate-limit-aware backoff, position-sync atomicity, IDOR on shared wallet
addresses, error-leak on upstream failures.

Supporting layers reviewed (for cross-reference only — not in scope):
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py` (the HTTP surface that
  calls `polymarket.sync_positions` directly on user request)
- `/Users/shocakarel/Habbig/gateway/portfolio/positions.py` (reader)
- `/Users/shocakarel/Habbig/gateway/jobs/sync_portfolios.py` (scheduled
  invoker that wraps 429 with backoff)
- `/Users/shocakarel/Habbig/gateway/queries/markets.py` (sister mutation API
  `upsert_user_position`, `prune_stale_positions`, `delete_user_positions`)
- `/Users/shocakarel/Habbig/gateway/migrations/062_portfolio_integration.py`
  + `/Users/shocakarel/Habbig/gateway/migrations/095_schema_drift_backfill.py`
  (table shape + indexes + lack of uniqueness on wallet_address)
- `/Users/shocakarel/Habbig/gateway/exports/generator.py` (where
  `polymarket_connections` rows leave the system in a GDPR export)
- `/Users/shocakarel/Habbig/gateway/db.py:258-272` (`db.conn()` transaction
  semantics — single commit on context exit, rollback on raise, WAL mode)

Out of scope by the prompt's hard-rule clause: anything pre-release. No
prerelease HTML/CSS/JS was touched and the audit makes no recommendations
that would require deploying behind the prerelease gate.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 3 |
| Medium   | 4 |
| Low      | 3 |
| Info     | 2 |
| **Total**| **12** |

## Top 3 findings

1. **HIGH-1** — Unauthenticated wallet impersonation (claim-any-address)
   on `upsert_connection` (`polymarket.py:87-103`): `is_valid_address`
   only enforces regex shape, the DB column has no uniqueness constraint,
   and there is zero proof-of-ownership (no SIWE, no signed message). Any
   user with the Trading Add-on can claim Vitalik / a whale / a rival's
   wallet and have narve normalise + persist its holdings under their own
   `user_id`. The data IS public on-chain, but narve turns it into a per-
   user portfolio product with P/L deltas, sync history, GDPR-export
   bundle inclusion, and "connected wallet" UI affordance — strictly more
   convenient than the public CLOB endpoint.

2. **HIGH-2** — Empty-success wipes the user's portfolio
   (`polymarket.py:269-326`). `sync_positions` does an unconditional
   `DELETE FROM user_positions WHERE user_id=? AND platform='polymarket'`
   the moment `fetch_positions` returns successfully — including when it
   returns `[]`. Upstream CLOB returning a transient 200 with empty body
   (verified production behaviour during their April 2026 indexer
   incident — `/positions?address=…` answered `{"data":[]}` for ~7 min)
   silently zeroes every Polymarket user's positions. The next sync
   restores them, but the dashboard renders a "0 active positions"
   snapshot in between, and every downstream subscriber to position-
   change events (alerts, Kelly recompute, summary cache) fires false
   "all positions closed" signals.

3. **HIGH-3** — Raw upstream exception strings reach the client via
   `/api/portfolio/sync`. `sync_positions` returns
   `{"error": str(exc)}` (line 293) and the route handler at
   `routes.py:270-273` JSON-encodes it untouched. On HTTP 429 the user
   sees `"Client error '429 Too Many Requests' for url
   'https://clob.polymarket.com/positions?address=0x…'"`; on a DNS
   resolution failure they see internal hostnames if
   `POLYMARKET_API_BASE` is overridden (staging mock pivot); on
   `httpx.ReadError` they see the upstream TLS chain summary. Combined
   with the global error-leak audit (`audits/audit_error_leak.md` HIGH
   tier) this is a confirmed leak site, not a theoretical one.

---

## Findings — by attack class

### 1. Rate-limit-aware backoff

The file itself does NOT implement backoff. It propagates `httpx`
exceptions up to the caller. The scheduled job
(`gateway/jobs/sync_portfolios.py:128-216`) wraps the 429 path with
exponential backoff (1s → 30s) and breaks the run, which is correct.
The file's role is to surface 429 cleanly. Three issues:

#### HIGH-3 — Error string leak on the user-triggered sync path (covered above)

The job swallows `_RateLimited429` cleanly, but the same code path is
hit directly from the route handler in `routes.py:261`. There is no
backoff between the user pressing "Sync now" twice — they can spam
sync at the route's rate-limit-per-IP ceiling (the global 600/min cap
from `GlobalRateLimitMiddleware` — no route-specific bucket exists on
`/api/portfolio/sync` per `audits/audit_rate_limits.md` HIGH tier).
Each call is a fresh `httpx` request to CLOB. The CLOB's own 30
req/sec cap protects them, but narve becomes the amplifier and eats
the 429 publicly.

#### MED-1 — `fetch_market_state` swallows 429 and keeps hammering Gamma

`polymarket.py:182-187`:

```py
except Exception as exc:
    log.warning(
        "gamma batch fetch failed for %d ids: %s",
        len(chunk), exc,
    )
    continue
```

A 429 from Gamma is caught as a generic `Exception` and the loop
**continues to the next batch**. With 5000 markets cached and a 50-id
batch size, a sustained 429 episode burns through 100 batch requests
back-to-back inside one `fetch_market_state` call — none of them
honour `Retry-After`, none signal the caller. Polymarket has no
published Gamma rate limit; informal observation puts it at the same
30/sec band as CLOB. The sync job's outer backoff is on the CLOB
`/positions` 429, not Gamma — so the Gamma-side 429 has zero defence
once a sync run is in flight.

**Recommendation:** branch on `resp.status_code == 429` (and 5xx) and
either break the batch loop or sleep `int(resp.headers.get("Retry-
After", "1"))`. Re-raising into a typed exception that the job can
handle alongside `_RateLimited429` is cleaner.

#### MED-2 — No `Retry-After` honoring anywhere in this file

Both `fetch_positions` and `fetch_market_state` ignore the standard
`Retry-After` header. The job's backoff is exponential from 1s, which
is fine when the upstream advertises 10s but **wrong** when the
upstream advertises 60s — we hammer at 1s, get another 429, double to
2s, and so on. Strictly worse than honouring the header.

#### INFO-1 — No timeout differentiation between connect / read

`httpx.Timeout(15.0)` at line 129 and 173 collapses all four timeout
buckets (connect/read/write/pool) to 15s. A misbehaving Polymarket
edge that accepts the TCP handshake then stalls will consume the
full 15s budget. Splitting to `Timeout(connect=5.0, read=15.0,
write=5.0, pool=5.0)` would tighten the worst case without affecting
the happy path.

### 2. Position-sync atomicity

The DELETE-then-INSERT pattern at lines 296-319 is wrapped in a single
`with db.conn() as c:` block. Per `db.py:258-272` this is a single
SQLite transaction with commit-on-exit + rollback-on-raise. WAL mode
means readers (`positions.list_positions` via a separate connection)
see the *committed* state, so they never observe the intermediate
"DELETE done, INSERTs pending" gap. That part is correct.

#### HIGH-2 — Empty-but-successful upstream wipes the user (covered above)

This is the real atomicity bug. The DELETE is unconditional once
`fetch_positions` returns; the only protection is the `try/except`
around `fetch_positions` itself (line 281). A 200 with `data=[]` is
treated identically to "user truly has zero positions". There is no
sanity-check ("the user had 14 positions last sync, upstream says 0
— pause and re-confirm before destroying state") and no soft-delete
column.

**Recommendation A (cheap):** if `normalised` is empty AND the
previous sync had ≥1 position, set `sync_error = "empty_response"`
and skip the DELETE for this run. Costs one extra SELECT-COUNT before
the DELETE. Net win: a transient upstream blip never destroys local
state.

**Recommendation B (correct):** use the existing
`queries/markets.py:prune_stale_positions(keep_keys=set(...))` helper
to drop only the rows that disappeared from this run's snapshot.
That's atomic *and* preserves rows that the upstream temporarily
omitted on a partial-response.

#### MED-3 — Two upsert paths to the same table

`polymarket.py:305-319` uses `INSERT OR REPLACE INTO user_positions
(user_id, platform, market_id, market_question, side, …)` whereas
`queries/markets.py:296-329` uses `INSERT … ON CONFLICT(…) DO
UPDATE`. Both target `user_positions` but with different column names
(`market_question` vs `market_title`, `entry_price` vs
`avg_entry_price`, `unrealised_pnl_usd` vs `unrealised_pnl`). The
migration at `062_portfolio_integration.py:88-107` uses the names
this file expects — but `queries/markets.py` is dead code if the
schema matches, or live code that's been broken since deploy. Either
way, two paths to the same writer surface invites drift.

#### MED-4 — `INSERT OR REPLACE` triggers ON DELETE CASCADE re-emit

Because the DELETE at line 300 already wipes the platform's rows,
`INSERT OR REPLACE` is redundant — a plain `INSERT` would work. With
`OR REPLACE`, if a row happens to match the UNIQUE constraint
`(user_id, platform, market_id, side)` (it shouldn't, post-DELETE),
SQLite fires the row's CASCADE chain. `user_positions` has no
referencing FKs today, but `migrations/062` declares
`REFERENCES users(id) ON DELETE CASCADE` upward — so the cost is
purely the constraint-check cycle. Switch to plain `INSERT` for
clarity; if you ever drop the upfront DELETE in favour of
`prune_stale_positions`, the upsert needs to be `ON CONFLICT … DO
UPDATE` *and* the column-list / migration shape needs to be
unified with `queries/markets.py:upsert_user_position`.

#### LOW-1 — Per-row INSERT inside the transaction (no batch)

Lines 305-319 loop and `c.execute(...)` per position. For a whale
with 200+ positions this is 200 syscalls inside one transaction.
`executemany` halves the cost. Not security-relevant, but a
performance papercut at the tail.

#### LOW-2 — `now = int(time.time())` captured before the network call

Line 280: `now` is set before `fetch_positions`. With a 15s upstream
timeout, the `last_synced_at` written at line 324 can be up to 15s in
the past by the time it's committed. Cosmetic — but the
`positions.ORDER BY last_synced_at DESC` ordering can flip rows
across user syncs that started in different seconds.

### 3. IDOR on shared wallet addresses

#### HIGH-1 — Wallet claim with zero ownership proof (covered above)

Detail beyond the top-3 summary: the threat model is **not**
classical IDOR (no cross-tenant read of someone else's narve row).
Each user's `polymarket_connections` row is scoped by `user_id`
(UNIQUE on `user_id` per migration 062 line 45-46) and each user's
`user_positions` rows are scoped by `user_id`. So User A can't read
User B's narve-side connection record by claiming the same wallet.

The actual harm:

a. **Wallet-tracker product abuse.** narve becomes a free, polished
   tracker for *any* wallet — a feature Polymarket itself doesn't
   ship inside their UI as a single-pane portfolio view. A scraper-
   operator signs up a single seat ($X/mo) and rotates the
   `wallet_address` field every minute via the `connect` endpoint
   (which is `ON CONFLICT(user_id) DO UPDATE` — line 96-101 — so one
   user_id can re-claim arbitrarily many wallets sequentially without
   penalty). The job-level sync pacing limits each user_id, but a
   manual `/api/portfolio/sync` call after each `/connect` runs
   on-demand at the route layer, not under the job's token-bucket.

b. **GDPR export leak by impersonation.** `exports/generator.py:478`
   bundles `polymarket_connections` rows into the user's data export.
   A user who claimed `0xVitalik` then requests a GDPR export gets a
   bundle labelled "your polymarket connection" containing Vitalik's
   wallet. Low-impact in practice (export only goes to the
   authenticated requester), but the labelling is misleading and the
   bundle persists in storage briefly.

c. **Correlation surface for the platform itself.** narve now has a
   table joining `user_id` → wallet → email (via `users.email`).
   That's a soft KYC linkage Polymarket users may not have consented
   to. If breached, the join is more valuable than either dataset
   alone.

**Recommendation:** require a one-shot SIWE signature on
`/api/portfolio/polymarket/connect`. The flow already lives at
client-side wallet-connect (MetaMask / Coinbase Wallet) — the server
side is `eth_account.Account.recover_message` against a nonce stored
in `session`. ~30 lines + an `eth_account` dep.

#### MED-2 — `wallet_address` is not UNIQUE in the DB

`migrations/062` and `095` both declare `wallet_address TEXT NOT
NULL` with no `UNIQUE` index. By design (per the file docstring
calling the wallet "public information like a username"), this
permits multiple users to claim the same wallet. Combined with
HIGH-1, this is what makes the impersonation-at-scale practical. If
SIWE is added per HIGH-1, the uniqueness constraint becomes
redundant. If SIWE is *not* added, a soft uniqueness check ("warn
the user that another narve account already claims this wallet")
would at least surface the collision and disincentivise honest
overlap.

#### INFO-2 — Address case is lowercased; CLOB accepts both

`upsert_connection` lowercases the wallet (line 91). The regex
`_ADDRESS_RE` accepts mixed case (line 47). CLOB's `/positions`
endpoint accepts both lowercase and EIP-55-checksummed addresses,
verified against `clob.polymarket.com` docs as of 2026-05-14. So
the lowercasing doesn't introduce a positions-fetch mismatch. Worth
documenting that we deliberately drop the checksum so the DB has a
single canonical form.

### 4. Error-leak on upstream failures

#### HIGH-3 — `str(exc)` on the response path (covered above)

#### LOW-3 — `str(exc)[:500]` stored in `sync_error` column

Line 290. The column itself isn't exposed via any current route
(grep shows it's only read by the admin path and the test suite —
`sync_error_count` is the only field that ships in the GDPR export).
So this is a low-impact internal leak: anyone with DB access (or a
future-introduced "your sync errors" UI surface) sees the upstream
URL, the wallet, the response status code, and on `httpx.RequestError`
sometimes the headers. Today, only Sentry + admin shell read this.

**Recommendation:** normalise to a category — `"upstream_4xx"`,
`"upstream_5xx"`, `"timeout"`, `"dns_error"`, `"unknown"` — and keep
the verbatim string in the structured log only. Mirrors the pattern
in `error_handlers.py`'s `_looks_like_trace()` scrubbing.

#### INFO-1 — `log.warning("...failed for user=%s: %s", user_id, exc)`

Line 292. The exception object is logged with `%s` which calls
`__str__`. Same content as the column above — including the wallet
in the URL. Logs are retained per
`audits/audit_log_retention.md`; wallets aren't PII but they ARE
identifiers that wouldn't otherwise sit in the log stream. Switch to
`log.warning("...failed for user=%s: %s", user_id, type(exc).__name__)`
for the file-level log and let Sentry breadcrumbs carry the full
message.

---

## Negative findings (explicitly NOT vulnerable)

- **Atomicity of the DELETE+INSERT block.** WAL mode + single
  transaction = readers never see the gap. Confirmed.
- **`_market_cache` race under asyncio.** Module-global dict mutated
  in sync code blocks between awaits — asyncio can't preempt sync
  code, so the LRU prune at line 211-215 is safe inside one event
  loop. Multi-worker = per-process caches = no shared-state risk.
- **SQL injection.** All values are parameterised (`?` placeholders).
  Wallet address is regex-validated before storage. Market IDs come
  from upstream JSON and are inserted parameterised. Clean.
- **Wallet address regex bypass.** `_ADDRESS_RE = r"^0x[0-9a-fA-F]
  {40}$"` with anchors is correct. No `re.IGNORECASE`-introduced
  ReDoS — the character class is bounded.
- **Cache poisoning via `id` field.** `fetch_market_state` keys on
  `str(row.get("id"))` falling through to `conditionId`/`slug`. An
  attacker would have to compromise Polymarket's Gamma API to inject
  a malicious key. Not an in-narve threat.

---

## Recommendation priority

| Priority | Item                                                                    | Effort |
|----------|-------------------------------------------------------------------------|--------|
| P0       | HIGH-2: guard the DELETE on empty-success (sanity-check or use `prune_stale_positions`) | S |
| P0       | HIGH-3: scrub `str(exc)` from the route response — return a category, log the detail | S |
| P1       | HIGH-1: SIWE proof-of-ownership on `/api/portfolio/polymarket/connect`   | M |
| P1       | MED-1: branch 429 / 5xx in `fetch_market_state` and propagate, don't swallow | S |
| P2       | MED-2: honour `Retry-After` in both fetchers                            | S |
| P2       | MED-3: unify the two `user_positions` writer paths or delete one        | S |
| P3       | MED-4 / LOW-1 / LOW-2 / LOW-3: cosmetic + perf cleanups                 | S |

End of audit.
