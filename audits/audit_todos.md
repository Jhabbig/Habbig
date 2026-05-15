# TODO / FIXME / XXX / HACK Audit — `gateway/`

Generated: 2026-05-15
Scope: `gateway/` (`.py`, `.html`, `.css`, `.js`)
Command:

```bash
grep -rn -E "TODO|FIXME|XXX|HACK" gateway/ \
  --include='*.py' --include='*.html' --include='*.css' --include='*.js'
```

Raw match count: **18**
Real annotation comments (excluding string/placeholder false positives): **14**
Tagged **SECURITY**: **7**
Tagged **DATA-LOSS**: **0**
Tagged **UX**: **2**
Tagged **NICETY**: **5**

Classification heuristic:
- **SECURITY** — comment text mentions auth, token, signature, permission, wallet ownership, account-takeover, or links to the security audit.
- **DATA-LOSS** — comment text mentions migration that drops/rewrites, deletion of user data, or destructive backfill.
- **UX** — user-visible flow, copy, or interaction that still needs work.
- **NICETY** — perf, refactor, code-organisation, no user impact.

---

## SECURITY (7)

### `gateway/server.py:239`
```
# TODO(security C4): replace site-wide SITE_ACCESS_TOKEN with per-user invite-
# token gate validation. Single shared secret = full gate bypass if leaked,
# with no rotation story. See NARVE_SECURITY_AUDIT.md critical item C4.
SITE_ACCESS_TOKEN = os.environ.get("SITE_ACCESS_TOKEN", "")
```
Why: shared-secret gate bypass; explicitly tagged `security C4` and cross-referenced to the security audit. Highest-impact item in this list.

### `gateway/api_v1.py:50`
```
TODO: add first_displayed_at column to api_keys table (INTEGER
NULLABLE). Until the migration ships the INSERT below will fall
back to the legacy column set so existing deploys don't crash.
```
Why: API-key one-time-display invariant (M16). Without the column the "raw key shown once" guarantee degrades to "raw key may be re-fetched", which is a credential-exposure risk.

### `gateway/api_v1.py:93`
```
... (and a single TODO to track when the column lands).
```
Why: same `first_displayed_at` column; restates the dependency from the read-path side. Counted separately because it is a distinct annotation requiring action when the migration ships.

### `gateway/backend/markets/polymarket_client.py:21`
```
# helper and the TODO on the connect entry points.
```
Why: comment block documenting that wallet-address shape validation is not proof of ownership; cross-references the two TODOs below. Security-relevant pointer.

### `gateway/backend/markets/polymarket_client.py:132`
```
TODO(security): require EIP-191 signature challenge before
linking wallet — see NARVE_SECURITY_AUDIT.md C9. For now we
only validate the shape of the address; a user can still query
positions for an address they do not own ...
```
Why: explicit `security` tag, cross-references audit item C9 (wallet-link spoofing). Currently a soft-disclosure risk only, but the TODO blocks treating wallet as "verified-owned" downstream.

### `gateway/backend/markets/polymarket_client.py:179`
```
TODO(security): require EIP-191 signature challenge before
linking wallet — see NARVE_SECURITY_AUDIT.md C9. Until then
treat this purely as a read of public on-chain state, never as
proof that the calling user owns the wallet.
```
Why: same C9 item, applied to the orders read-path.

### `gateway/integrations/telegram_bot.py:97`
```
# TODO: table pending_telegram_links(user_id INTEGER NOT NULL,
#       code TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
#       created_at INTEGER NOT NULL) must be created, and a
#       corresponding web-UI endpoint must mint codes. Until the
#       table exists this handler refuses all link attempts ...
```
Why: directly above the H15 fix that closed an account-takeover vector. Handler safely refuses today, but the missing table means the link feature is offline — the moment someone wires it back without the table, the H15 bypass returns.

---

## UX (2)

### `gateway/server.py:7282`
```
# TODO: Replace /enquire with Stripe add-on checkout when payments configured
```
Why: trading add-on currently routes to a contact page rather than self-serve Stripe checkout. Friction for the active-state user.

### `gateway/server.py:7290`
```
# TODO: Replace /enquire with Stripe add-on checkout when payments configured
```
Why: same swap for the inactive/upsell state.

---

## NICETY (5)

### `gateway/server.py:4949`
```
TODO(perf): the DB write is still synchronous on the request path.
At very high event rates we should push to a background task / queue
so the beacon response never blocks on disk I/O.
```
Why: perf optimization for the analytics beacon; not on a hot path today.

### `gateway/tests/test_foundation_bundle.py:266`
```
# ── Stripe TODO cleanup ──────────────────────────────────────────────
```
Why: section banner inside the test file. Not a real action item — the matcher caught the literal word `TODO` in a section header.

### `gateway/tests/test_foundation_bundle.py:272`
```
assert "TODO" not in text, (
```
Why: this *is* the test that fails if `landing.html` contains a literal `TODO`. Meta-marker, no action.

### `gateway/tests/test_foundation_bundle.py:273`
```
"landing.html still has a TODO comment — resolve or "
```
Why: assertion-failure message string for the test above. Meta-marker, no action.

### `gateway/jobs/affiliate_jobs.py:15`
```
Gotcha: the Stripe webhook that populates ``first_payment_amount_pence``
isn't wired yet (see server_features.py TODO). Until it is, this job is
a safe no-op ...
```
Why: cross-reference to an out-of-scope TODO in `server_features.py`. Job is documented safe-no-op until the upstream webhook lands.

---

## False positives (not real annotations) — 4

These hits matched the regex but are placeholder strings inside docstrings, JSON examples, or UI hint text — not code-comment markers.

- `gateway/migrations/060_subproduct_subscriptions.py:10` — `"sub_XXX"` Stripe-subscription-id placeholder inside an example JSON blob in the migration's module docstring.
- `gateway/forensics/extract_watermark.py:11` — `sid:XXXX` describing the watermark fragment format in the module docstring.
- `gateway/static/admin_security_forensics.html:34` — `sid:XXXX` rendered as user-visible hint text under the screenshot input.
- `gateway/static/admin/security-forensics.html:18` — duplicate of the above in the new admin layout.

These four are excluded from the action-item counts above but listed here so the audit is verifiable against the raw `grep` output.
