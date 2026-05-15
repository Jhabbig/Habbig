# Audit — `gateway/market_routes.py`

Adversarial review focused on:

1. Polymarket wallet-connect SIWE flow correctness (nonce, expiration, chain id, audience)
2. Kalshi token encryption-at-rest
3. Market-data fetch SSRF — URL params reaching `httpx.get`
4. Unbounded result-set queries
5. IDOR on viewing other users' positions

**File audited:** `gateway/market_routes.py` (1116 lines, reviewed at HEAD `2aabe7b` on `feature/platform-build`).
**Date:** 2026-05-15
**Auditor:** automated adversarial pass — no code changes (per task hard rule).

---

## 0. Result summary

| Severity | Count |
|----------|------:|
| Critical | 0 |
| High     | 2 |
| Medium   | 4 |
| Low      | 4 |
| Info     | 3 |

**Top 3 findings:**

1. **HIGH-1 — SIWE message `Domain:` / first-line audience is NEVER verified, only the `URI:` line.** `_siwe_parse_message` (L95–122) only extracts `URI`, `Version`, `Chain ID`, `Nonce`, `Issued At`. The `{SIWE_DOMAIN} wants you to sign in with your Ethereum account:\n{address}\n` prefix — the field a wallet shows most prominently in the popup — is **not parsed and not compared**. A malicious dApp can request a fresh nonce against narve.ai under the attacker's own session, build a SIWE body whose first line is `popular-defi-site.io wants you to sign in...` but whose `URI: https://narve.ai` line matches, get a victim to sign it via wallet phishing, then POST `{address: victim_wallet, signature, message}` from the attacker's session. The nonce check passes (it belongs to the attacker), `recovered.lower() == signed_address.lower()` passes (the victim really signed), and the victim's wallet is now attached to the **attacker's** account at `db.upsert_market_credential(user_id=attacker, polymarket_wallet_address=victim)`. This is the exact attack the SIWE block was built to prevent — the original threat in `gateway/migrations/181_wallet_connect_nonces.py` lines 6–10. **Additional gap:** the SIWE-internal `Address:` line (the wallet inside the message body) is also never extracted and never compared to `signed_address` or to the nonce row, so an attacker who controls the SIWE body can decouple "who the wallet claims to be addressing" from "who is actually being granted access".
2. **HIGH-2 — Service-account-authenticated path traversal to upstream Kalshi/Polymarket via `market_id`.** `api_market_detail`, `api_poly_order_params`, and `api_kelly_calculate` all accept `market_id` and pass `market_id[5:]` (for `poly:`) or `market_id[7:]` (for `kalshi:`) verbatim into `fetch_single_market` → `poly_client.get_market(slug)` / `kalshi_client.get_market(ticker)` → `client.get(f"{base}/markets/{slug}")` with **no character validation**. httpx normalises `../` in path segments (verified in this audit run: `httpx.URL('https://gamma-api.polymarket.com/markets/' + '../../v1/internal').path == '/v1/internal'`). A user sending `market_id=poly:../../v1/internal` or `kalshi:../../portfolio/balance` therefore drives a GET against arbitrary path on the upstream host. The Kalshi call attaches a **service-account** bearer (via `_public_headers()` → `_get_service_token()`), so a path-traversal payload effectively becomes a SSRF-with-stolen-creds against `api.elections.kalshi.com` with narve.ai's service account. The blast radius is limited to those two domains (httpx rejects CRLF and refuses host-change via path), but it's still an authenticated probe across an upstream API surface narve.ai pays for and is liable for. **Fix:** validate `slug` against `^[a-zA-Z0-9_-]{1,128}$` and `ticker` against Kalshi's documented format (`^[A-Z0-9_.-]{1,64}$`) in the route handler before calling `fetch_single_market`.
3. **MEDIUM-1 — DELETE `/api/markets/connect/{source}` is currently a CSRF-loggable verb (not enforced).** `api_disconnect_market` is wired as `DELETE` (L1105). The site-wide CSRF middleware enforces double-submit cookie on POST today; `CSRF_PATCH_DELETE_ENFORCE` is gated behind an env var defaulting `false` (server.py L1095) so PATCH/PUT/DELETE only **log** mismatches in Phase-1. A cross-origin forgery (e.g., a malicious page exploiting an authenticated narve.ai session via `<form method="POST">` over a method-spoofing proxy, or a `fetch('/api/markets/connect/polymarket', {method:'DELETE'})` from a same-site XSS) can disconnect a user's wallet *and* `db.delete_user_positions` wipes their cached portfolio (L682). The disconnect call also scrubs the Kalshi token (`disconnect_market_credential` sets `kalshi_token=NULL`), so a forgery forces the user to re-login + re-encrypt + re-sync — denial of service plus a window where positions are missing from any signal-based downstream. **Fix:** turn on `CSRF_PATCH_DELETE_ENFORCE=true` in the production env, or move the disconnect to a POST with explicit CSRF token. The 5/min/user rate limit is not a substitute — it's about preventing log-spam, not forgery.

---

## 1. Findings detail

### HIGH-1 — `Domain:` and `Address:` lines unparsed in SIWE message (L72–122, L544–664)

**Where:** `_siwe_parse_message` only matches lines starting with `URI: `, `Version: `, `Chain ID: `, `Nonce: `, `Issued At: `. The first line of the canonical SIWE body — `{SIWE_DOMAIN} wants you to sign in with your Ethereum account:` — and the second line — the wallet address inside the message — are never extracted.

**Server-built template (L81–92):**
```
{SIWE_DOMAIN} wants you to sign in with your Ethereum account:
{address}

Verify wallet ownership for narve.ai portfolio sync.

URI: {SIWE_URI}
Version: {SIWE_VERSION}
Chain ID: {SIWE_CHAIN_ID}
Nonce: {nonce}
Issued At: {issued_at}
```

**The verify-side parse skips the first two lines.** That means:

- Wallet UX (MetaMask, Rabby, Frame) renders the first line in bold as the "audience" of the sign-in. A user who reads "evil-site.io wants you to sign in..." would refuse. But a user who reads "narve.ai wants you to sign in..." in a phishing context where the *server* doesn't enforce the same string can be tricked into signing a message that the narve.ai backend *accepts* even though the user thought they were signing for a different site.
- The SIWE spec (EIP-4361) §4 ("Verifying a Signed Message") explicitly says the verifier MUST check that the domain in the message matches the verifier's expected domain. Skipping this is a spec deviation.
- The `Address:` line (L84 of the template) is meant to be the wallet the message is *bound to*. The current code recovers the signer from the signature and compares it to `signed_address` (the JSON body field). The in-message address is never used. An attacker who controls the message body can therefore decouple "who the wallet popup told the user they were signing for" from "what address the server attaches to the session".

**Concrete chained attack path:**

1. Attacker logs into narve.ai with their own account, requests a nonce — gets a row in `wallet_connect_nonces` keyed to `user_id=attacker`.
2. Attacker hosts a phishing dApp that prompts the victim to sign a SIWE message. The phishing message says `popular-defi.io wants you to sign in with your Ethereum account:\n{victim_wallet}\n\n...\nURI: https://narve.ai\nVersion: 1\nChain ID: 1\nNonce: {attacker_nonce}\nIssued At: {now}`.
3. Victim's wallet popup shows "popular-defi.io" as the audience — victim assumes they're connecting to popular-defi.io and signs.
4. Attacker captures `{victim_wallet, signature, message}` and POSTs to `/api/markets/connect/polymarket` from their *attacker* session.
5. Server-side: `_siwe_recover_signer(message, signature)` returns `victim_wallet` (the signature is mathematically valid for that message). `recovered.lower() == signed_address.lower()` passes. `_siwe_consume_nonce(nonce, user_id=attacker)` passes (the nonce belongs to attacker). `db.upsert_market_credential(user_id=attacker, polymarket_wallet_address=victim_wallet)`.
6. Attacker's narve.ai dashboard now shows victim's positions, signal-following telemetry, and portfolio sync history.

**Why the existing nonce binding doesn't help:** the nonce is bound to *whichever account requested it*. The whole attack rests on the attacker requesting the nonce from their own narve.ai session and then convincing a victim to sign it via a phishing dApp pretending to be a different site.

**Why URI: https://narve.ai inside the message doesn't help:** advanced wallet UIs (Frame, hardware wallets) do display the URI line, but the wallet UX studies are clear that the bold first line is the field users actually read. Server-side enforcement is the only reliable check.

**Recommended fix:**

```python
# in _siwe_parse_message, add:
elif line.startswith(f"{SIWE_DOMAIN} wants you to sign in"):
    out["domain_line"] = line
# ... and an address line:
# (the address is on line 2, no prefix — match by position not prefix)
```

Then at L588 (right after parsing nonce):
```python
expected_first_line = f"{SIWE_DOMAIN} wants you to sign in with your Ethereum account:"
if not message.startswith(expected_first_line):
    return JSONResponse({"error": "Signed message audience mismatch"}, status_code=400)
# message.splitlines()[1] is the address line
in_message_addr = message.splitlines()[1].strip()
if in_message_addr.lower() != signed_address.lower():
    return JSONResponse({"error": "Signed message address mismatch"}, status_code=400)
```

Two added equality checks; closes the gap.

---

### HIGH-2 — Path-traversal SSRF via `market_id` slug/ticker to upstream APIs (L395–438, L861–905, L989–991, L398–400)

**Where:**

- `api_market_detail(request, market_id)` → `fetch_single_market(..., market_id, cache_ttl=120)`
- `api_poly_order_params(request, market_id)` → same
- `api_kelly_calculate` reads `body.get("market_id")` → same

**Path of taint:**

```
market_id (user, URL path) ──> fetch_single_market
  if market_id.startswith("poly:"):
    slug = market_id[5:]                            # ← no validation
    raw = await poly_client.get_market(slug)
  elif market_id.startswith("kalshi:"):
    ticker = market_id[7:]                          # ← no validation
    raw = await kalshi_client.get_market(ticker)
```

```
poly_client.get_market(slug):
    resp = await client.get(f"{self.gamma_base}/markets/{slug}")
                                                   # ← f-string into URL
kalshi_client.get_market(ticker):
    resp = await client.get(
        f"{self.base_url}/markets/{ticker}",
        headers=await self._public_headers(),     # ← SERVICE TOKEN attached
    )
```

**Verified behaviour:**

```
$ python3 -c "import httpx; print(httpx.URL('https://gamma-api.polymarket.com/markets/' + '../../v1/internal').path)"
/v1/internal
```

httpx normalises `..` segments per RFC 3986; the resulting GET hits `https://gamma-api.polymarket.com/v1/internal` (or whatever the path traversal lands on).

**What an attacker can do:**

- Force narve.ai's backend to make authenticated requests to arbitrary paths on `gamma-api.polymarket.com` and (more concerning) `api.elections.kalshi.com` — with narve.ai's service-account bearer in the latter case.
- Probe upstream APIs for rate-limit, auth-rule, or schema info that narve.ai's normal traffic doesn't surface.
- If Polymarket / Kalshi ever expose an internal endpoint behind their public host (e.g., admin metrics, version probes that leak stack details), narve.ai's IP is the one talking to them.
- Cache poisoning of the in-process `_get_cached(f"market:{market_id}")` — a crafted `market_id="poly:../../v1/internal"` writes the response under that key, but the unified cache lookup uses the same suffix, so the next legitimate fetch of any market hashes to a separate key. The poisoning blast is small but the cache key is user-controlled.

**Bounds (why this is HIGH not CRITICAL):**

- httpx rejects CRLF in URL strings (verified: `httpx.InvalidURL: Invalid non-printable ASCII character in URL`), so header-injection / response-splitting is closed.
- Path traversal cannot change the *host* (httpx parses URL up-front and won't let path bytes escape to authority).
- Outcome is bounded to "GET requests to two specific upstream hosts" — not full SSRF to `169.254.169.254` or internal services.
- Polymarket's Gamma API has no auth, so the Polymarket side leaks nothing the attacker couldn't already fetch directly.
- Kalshi side is the real risk because of the service-account bearer.

**Fix:**

In `market_routes.py`, validate `market_id` before calling `fetch_single_market`:

```python
_POLY_SLUG_RE = re.compile(r"^poly:[a-z0-9-]{1,128}$")
_KALSHI_TICKER_RE = re.compile(r"^kalshi:[A-Z0-9_.-]{1,64}$")

def _validate_market_id(market_id: str) -> None:
    if not (_POLY_SLUG_RE.fullmatch(market_id) or _KALSHI_TICKER_RE.fullmatch(market_id)):
        raise HTTPException(400, "Invalid market id format")
```

Apply at the top of `api_market_detail`, `api_poly_order_params`, `api_kelly_calculate`. Belt-and-braces: also percent-encode the slug in `polymarket_client.get_market` (`urllib.parse.quote(slug, safe='')`) so a future caller can't reintroduce the bug.

---

### MEDIUM-1 — DELETE wallet-disconnect not CSRF-enforced (L667–684, L1105)

Described in the Top-3. Additional context:

- The disconnect path scrubs `kalshi_token` (set NULL) and inactivates the row, so post-forgery the user *must* re-login to Kalshi with email+password — handing the attacker a second window to phish credentials by impersonating the reconnect prompt.
- `db.delete_user_positions(user["user_id"], platform=source)` (L682) drops every cached row for that platform. Any downstream signal that depends on `user_positions` (the `portfolio_signals.py` module, alerts) is silently degraded until the next sync. The user has no audit log indicating "your wallet was disconnected by external action".

**Fix:** flip `CSRF_PATCH_DELETE_ENFORCE=true` in production env. Belt-and-braces: emit an `audit_log` row from `disconnect_market_credential` with the request IP and User-Agent so an unwanted disconnect is investigable.

### MEDIUM-2 — `signature`, `message`, `address` body fields lack length caps (L576–578)

The SIWE verify path reads:
```python
signature = (body.get("signature") or "").strip()
message = body.get("message") or ""
signed_address = (body.get("address") or "").strip()
```

There is no per-field length cap. The global `MAX_REQUEST_BODY` ceiling (in `SecurityHeadersMiddleware`) prevents megabyte-scale bodies, but within that ceiling an attacker can send a 100KB `message` to force `_siwe_parse_message` (a `splitlines()` + linear scan over five `startswith` prefixes) to run on the full payload. With 5 concurrent requests at the rate-limit-window boundary (5/min/user × N attackers), this is meaningful CPU and log-noise.

Also: `_siwe_recover_signer` calls into `eth_account.Account.recover_message(encode_defunct(text=message), signature=signature)`. `encode_defunct` does a keccak hash over the message — O(len(message)) but cheap. The signature is parsed by `eth_account` — a 100KB hex signature is rejected by the library, so no risk there.

**Fix:** add explicit caps:
```python
if len(signature) > 200 or len(message) > 2048 or len(signed_address) > 50:
    return JSONResponse({"error": "Field too long"}, status_code=400)
```

### MEDIUM-3 — `Issued At` from the message is parsed but never validated (L121, L589–602)

`_siwe_parse_message` extracts `Issued At` but the verify path never compares it to the nonce row's `created_at` or to "now". An attacker who already controls a fresh nonce can include any plausible-looking `Issued At` in the message (e.g., a year ago). The nonce TTL of 300 seconds bounds replay, so the practical impact is small, but per SIWE §4 the verifier "MUST check the `Issued At` is not in the future" and "SHOULD check it is not unreasonably old".

The current behaviour is "ignore the value entirely" — meaning a future refactor that extends the nonce TTL also widens the replay window with no second-line defense.

**Fix:** add a parse-and-compare:
```python
try:
    iat = datetime.fromisoformat(parsed["issued_at"].replace("Z", "+00:00"))
except (TypeError, ValueError):
    return JSONResponse({"error": "Invalid Issued At"}, status_code=400)
now_dt = datetime.now(timezone.utc)
if iat > now_dt or (now_dt - iat).total_seconds() > SIWE_NONCE_TTL + 30:
    return JSONResponse({"error": "Issued At outside acceptable window"}, status_code=400)
```

### MEDIUM-4 — Kalshi token plaintext-fallback returns the input on decrypt failure (`gateway/backend/markets/encryption.py:54–67`)

`decrypt_token` silently falls back to returning the encrypted input verbatim when:
- `cryptography` is unavailable (no Fernet)
- `CREDENTIALS_ENCRYPTION_KEY` is unset
- Decryption raises (corrupted ciphertext, key rotated, etc.)

In production, `config.REQUIRED_VARS` blocks startup when `CREDENTIALS_ENCRYPTION_KEY` is missing (verified at `gateway/config.py:93–98` and `server.py:421-429`), so the "no key" branch only happens in dev. **However**, the "decryption raised" branch returns the *ciphertext* as if it were the token — meaning a freshly key-rotated token row, or one corrupted on disk, gets passed to `KALSHI_CLIENT.get_balance(token)` as an opaque blob. Kalshi returns 401, the code calls `db.set_market_credential_active(user_id, "kalshi", False)`, and the user sees "session expired — please reconnect". No data is leaked, but the failure mode is confusing and silently disables the user's Kalshi link.

More importantly: if the ciphertext format ever changes (e.g., the Fernet token version upgrades and an unencrypted token sneaks in from a migration), this fallback masks the bug. A bare `log.error` and `raise` would surface it.

**Fix:** in the `except Exception:` branch, `log.error("decrypt_token failed — possible key rotation or DB corruption")` and `raise RuntimeError(...)`. The legacy "this might be a pre-encryption plaintext token" path is now ~2 years old (per SECURITY_LOG.md) — the migration window is closed.

### LOW-1 — `api_markets_orders` returns unbounded result from upstream APIs (L915–935)

`get_combined_orders` aggregates whatever Polymarket CLOB and Kalshi return. There's no `limit` enforced on either side. A user with many open orders sees them all — fine for the user, but the response body size is unbounded from narve.ai's perspective. The portfolio sync path has the same shape but it writes to `user_positions` table without batching (`portfolio_sync.py`).

In practice, no individual user has 10k+ open orders on these exchanges, so the realistic worst case is "a few hundred rows" — bounded by user behaviour, not by validation. Worth a `[:500]` slice anyway.

### LOW-2 — `signature[:10]` / `address[:10]` in logs leak the public key prefix (L610, L633)

`log.warning("SIWE connect: signer mismatch for user %s — claimed %s, recovered %s", user.get("username"), signed_address[:10] + "...", recovered[:10] + "...")` — the first 10 chars of an EVM address is `0x` + 8 hex chars = 32 bits. Not enough to break the wallet, but cross-correlating two log entries can identify *which* wallet the user tried to connect (combined with the wallet-id from upstream order responses). 

For incident response, you almost always want the *full* address logged or none of it — the 10-char prefix is the worst of both worlds (recognisable on its own to someone with the wallet, but not unique enough to pivot). Either log the full address (and tighten log access) or hash it (`hashlib.sha256(addr).hexdigest()[:12]`).

### LOW-3 — `forensic_sign` is *not* applied to `api_market_detail`, `api_disconnect_market`, `api_markets_stats`, or `api_kelly_calculate` (L438, L684, L957, L1043)

The other surfaces in this file (`unified`, `top-edge`, `false-consensus`, `search`, `connections`, `portfolio`, `orders`) wrap their JSON in `srv._forensic_sign(...)`. Detail / disconnect / stats / Kelly do not. This is an inconsistency: if `_forensic_sign` is a security control (tampering detection on cached responses), then half the routes lack it. If it's purely cosmetic / observability, then the doc-strings should say so.

`server.py:2219` would clarify; this audit didn't inspect that function, so this is filed as LOW pending that check.

### LOW-4 — `body.get("order_type", "GTC")` in `api_bet_polymarket` passes through unverified (L843)

`api_bet_polymarket` constructs `clob_payload = {"order": signed_order, "owner": owner or connected_addr, "orderType": body.get("order_type", "GTC")}` — `orderType` is forwarded to the CLOB without any check against `{"GTC", "FOK", "GTD", "FAK"}` (the documented Polymarket CLOB order types). An invalid value will be rejected upstream, but a future field name collision (e.g., Polymarket adds `"adminOverride": "..."`) could allow privilege escalation by passing an unexpected key through. Defence-in-depth: whitelist the four documented values.

Same shape applies to `owner` — `body.get("owner") or ""` defaults to `connected_addr`, but if the body explicitly sets `owner` to anything other than the user's wallet, the value is forwarded. The CLOB will reject signature/maker/owner mismatches, but again — narve.ai should refuse to ferry obvious mismatches.

### INFO-1 — IDOR audit: **clean**.

Every database read in this file scopes by `user["user_id"]` from the session (`current_user(request)` → middleware-attached row). There is no path parameter or query parameter that accepts a user_id from the caller. Confirmed across:

- `api_market_connections` → `_get_market_connections(user["user_id"])`
- `api_markets_portfolio` → `_build_enriched_portfolio(user["user_id"])`
- `api_markets_orders` → `db.get_all_market_credentials(user["user_id"])`
- `api_markets_stats` → `db.get_portfolio_stats(user["user_id"])`
- `api_markets_sync` → uses `user["user_id"]`
- `api_user_bankroll_get/set` → uses `user["user_id"]`
- `api_bet_kalshi`, `api_bet_polymarket` → `db.get_market_credential(user["user_id"], ...)`
- `api_disconnect_market` → `db.disconnect_market_credential(user["user_id"], source)`

The `source` path parameter on `api_disconnect_market` is whitelisted (`if source not in ("polymarket", "kalshi"): raise 400`). The `market_id` path on `api_market_detail` does NOT identify a user — it's an external market identifier — so IDOR is not applicable there (see HIGH-2 for the unrelated SSRF on that field).

**Verdict:** IDOR on positions / portfolio / credentials is not reachable from this file.

### INFO-2 — Unbounded queries audit: **mostly clean**.

| Surface | Limit source | Verdict |
|---|---|---|
| `api_markets_unified` | clamped `1 ≤ limit ≤ 100` (L268–271) | OK |
| `api_markets_top_edge` | clamped `1 ≤ limit ≤ 50` (L340) | OK |
| `api_markets_false_consensus` | clamped `1 ≤ limit ≤ 50` (L380) | OK |
| `api_markets_search` | hardcoded `[:20]` (L451) | OK |
| `list_top_environmental_impacts(limit=200)` | hardcoded 200 (L302) | OK |
| `api_markets_orders` | **no cap** (L930–934) | LOW-1 above |
| `api_markets_portfolio` | bounded by upstream | OK |

### INFO-3 — Polymarket CLOB submission `signer/maker` ownership check is correctly enforced.

L820–834 of `api_bet_polymarket` lower-cases both `signed_order["signer"]` and `signed_order["maker"]` and compares to `cred["polymarket_wallet_address"]` from the connected-credential row. A user-B-signed order submitted from a user-A session is rejected with HTTP 403. This is a real defense — the CLOB itself would also reject the mismatch, but having narve.ai refuse first preserves a clean audit trail and avoids burning the user's quota.

The only nit: if a user has *never* connected a Polymarket wallet, the earlier `if not cred or not cred["polymarket_wallet_address"]:` check (L817–819) returns a generic 400 — which is fine, but `connected_addr = (cred["polymarket_wallet_address"] or "").lower()` would then crash on `None.lower()` if the earlier check were ever removed. Belt-and-braces: keep the explicit `if not cred` check.

---

## 2. Non-findings (verified clean)

- **Nonce single-use enforcement:** L144–178 `_siwe_consume_nonce` correctly checks `row["user_id"] != user_id`, then atomically `UPDATE … WHERE used_at IS NULL` — concurrent verifies on the same nonce race-lose at the UPDATE.
- **Nonce TTL:** 300 seconds enforced at L167; matches SIWE_NONCE_TTL constant; nightly job `trim_wallet_connect_nonces` clears stale rows after 3600s (`gateway/jobs/db_maintenance.py:260`).
- **Nonce entropy:** 128 bits via `secrets.token_hex(16)` (L132). Well above birthday bound for any realistic call volume.
- **Nonce per-session binding:** `INSERT INTO wallet_connect_nonces (nonce, user_id, ...)` (L136–140) — every nonce row has the issuing user_id; `_siwe_consume_nonce` rejects redemption against any other user_id.
- **Signature recovery library:** `eth_account.messages.encode_defunct` (EIP-191) — correct prefix for MetaMask `personal_sign`. Reject-on-import-fail (L191–196) so a missing dep is fail-closed.
- **Chain id check:** L599 enforces `parsed["chain_id"] != str(SIWE_CHAIN_ID)` → 400. SIWE_CHAIN_ID = 1 (Ethereum mainnet) — matches the wallet-default-network for `personal_sign`.
- **URI / Version check:** L593, L601 — both enforced.
- **Polymarket order maker/signer ownership:** L820–834 enforced (see INFO-3 above).
- **Address regex:** L69 `0x[a-fA-F0-9]{40}` with `fullmatch` at L583 — blocks the obvious junk including longer strings used as path traversal payloads via the *address* field.
- **Rate limit on nonce + connect + disconnect:** all three share `f"market_connect:{user_id}"` with 5/min/user (L459, L515, L566, L673). Sharing the budget across all three correctly prevents an attacker from spamming nonce issuance to evade the connect budget.
- **CSRF on POST endpoints:** `/api/markets/*` is not in `_CSRF_EXEMPT_POSTS` (server.py:1105) or `_CSRF_EXEMPT_POST_PREFIXES` (server.py:1142) — POST endpoints get the double-submit cookie check.
- **SQL injection:** all DB writes use parameterised queries (`?` placeholders) in `queries/markets.py`. The two interpolations in this file (`f"market:{market_id}"` cache key and `f"market_connect:{user_id}"` rate-limit key) are not SQL — they're cache/rate-limit dict keys.
- **`api_disconnect_market` `source` validation:** L679 whitelists `("polymarket", "kalshi")` — no SQL injection or path manipulation via source.
- **`signed_order` field whitelist:** L805–815 checks for missing required fields and rejects with 400 — though it doesn't check for *extra* fields (a defense-in-depth nit, not a finding).
- **Kalshi token encryption at rest:** Fernet-encrypted via `encrypt_token` (L479); decrypted only at the boundary call in `api_bet_kalshi` (L722) and `api_markets_orders` (L928). Encrypted ciphertext stored in `user_market_credentials.kalshi_token`. Plaintext fallback only when `CREDENTIALS_ENCRYPTION_KEY` is missing AND `PRODUCTION` is unset — gated correctly in `encryption.py:46–52`.
- **No persistence of Kalshi password:** L468–470 `password = body.get("password", "")` is passed to `KALSHI_CLIENT.login()` once and not stored. `encrypt_token(result["token"])` stores only the token.
- **No persistence of wallet private key:** never read, never received.
- **Bankroll bounds:** `api_user_bankroll_set` (L1051–1084) clamps bankroll to `[0, 1_000_000_000]` and Kelly fraction to `(0, 1]`.
- **Limit price clamping in `api_bet_kalshi`:** L740–748 — price must be `(0, 1)` exclusive, clamped to `[1, 99]` cents. OK.
- **Amount bounds:** Kalshi $25k cap (L715–716); Polymarket $100k cap (L799–800).

---

## 3. Re-aim suggestions

If the scope is extended in a follow-up:

- **`gateway/backend/markets/portfolio_sync.py`** — the `sync_user_portfolio` path that this file delegates to. Worth checking for race conditions when two `/api/markets/sync` requests fire near-simultaneously and the rate limit racing with the DB write.
- **`gateway/server.py:_forensic_sign`** (L2219) — what does the signing do? If it's a tamper-detection HMAC, why isn't it on every market route? See LOW-3.
- **`gateway/backend/markets/kalshi_client.py:_get_service_token`** — the source of the service-account auth that HIGH-2 leverages. Worth a separate audit pass focused on token storage, rotation, and scope.

---

*Audit run 2026-05-15 against `feature/platform-build` HEAD `2aabe7b`. Re-run after any change to `_siwe_parse_message`, `fetch_single_market`, the wallet-connect verify path, or `CSRF_PATCH_DELETE_ENFORCE`.*
