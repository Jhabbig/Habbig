# Invite-Token Entropy Audit

**Date:** 2026-05-15
**Scope:** Invite-token entropy — bit-length, RNG source, and absence of sequential-ID enumeration vectors.
**Hard rule observed:** synchronous bash only; pre-release routes **not** examined.

---

## Summary

Two distinct invite-token primitives coexist. The primary one — the one the entire `/token` / `/invite` / referral signup flow validates — passes the spec. The secondary one — the per-user "hand-out" invite codes shown on the `/settings/invites` page — falls **below** the 128-bit threshold and is the only real gap.

| Primitive | Generator | Entropy | RNG | Lookup key | Verdict |
|-----------|-----------|---------|-----|------------|---------|
| `invite_tokens.token` (admin-minted + referral-flow) | `secrets.token_urlsafe(24)` → 32-char string | **192 bits** | `secrets` (CSPRNG) | `token` string only (`gateway/queries/auth.py:335`) | **PASS** |
| `user_invite_tokens.token` (per-user replenishment, "hand a code to a friend") | `"".join(secrets.choice(alphabet) for _ in range(16))` over a 32-char unambiguous alphabet | **80 bits** (16 × log2(32)) | `secrets.choice` (CSPRNG) | `token` string only (`gateway/db_sharing.py:327, 333, 344`) | **FAIL (entropy)** |

No code path looks up either invite primitive by row `id`. The only `WHERE id =` writes against `invite_tokens` are the admin revoke and the internal replenish-job prune — neither is reachable from a client-supplied identifier. **Sequential-ID enumeration is not a vector.**

---

## Findings

### Entropy — `invite_tokens.token` (primary)
- **`gateway/queries/auth.py:304-306`** — `generate_invite_token()` returns `secrets.token_urlsafe(24)`. 24 random bytes = **192 bits** of entropy. Output is 32 url-safe characters. Comfortably ≥128 bits. ✓
- Used by every public/auth path that materialises an invite: admin mint (`server.py:5857`, `server.py:6005`), referral accept (`routes_referrals.py:152`), subscription checkout (`public_routes.py:167`), replacement-token mint (`server.py:6271`), bootstrap admin token (`server.py:443`).
- `FIELD_MAX["invite_token"] = 64` (`gateway/server.py:316`) bounds inbound submissions — generous margin around the 32-char output.

### Entropy — `user_invite_tokens.token` (secondary) — **GAP**
- **`gateway/db_sharing.py:262-267`** — `_mint_invite_token_string()` returns 16 chars from the 32-char unambiguous alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`. Entropy = 16 × log2(32) = **80 bits**.
- Below the 128-bit requirement by 48 bits.
- The accompanying comment ("single-use so we want more entropy. Collision odds negligible") only reasons about *collision*, not about *guessability*. The migration table is `UNIQUE` on `token`, so collisions are auto-handled; the threat the audit cares about — an attacker brute-forcing a redeemable code — is not addressed by the design comment.
- Mitigations in place that lower (but do not eliminate) the practical impact:
  - These codes are short-lived per redemption and one-shot (atomic `UPDATE … WHERE token = ? AND used_at IS NULL AND is_active = 1`, `db_sharing.py:325-329`).
  - Redemption is gated on an authenticated session in `routes_sharing.py` (verified by following the redeem call sites).
  - Account-level + per-IP rate limits live on other auth surfaces; this specific redeem path inherits the global auth-route limiter via `auth_rate_limit` (`gateway/security/rate_limiter.py:227`).
- Even so, 80 bits is the wrong primitive for an "anyone with the code can claim a paid invite" capability token. Recommend swapping to `secrets.token_urlsafe(24)` (or, if the UI requires the unambiguous human-typable alphabet, lengthen to **≥26 chars** to clear 128 bits: 26 × log2(32) ≈ 130). **Gap (high):** under-entropy invite-redemption token.

### RNG source
- Every invite-related generator imports the stdlib `secrets` module: `gateway/queries/auth.py:14`, `gateway/db_sharing.py:25`, `gateway/db_referrals.py` (referral codes, adjacent), `gateway/db_affiliate.py:38` (affiliate codes, adjacent).
- No use of `random.*`, `time`-seeded RNGs, `uuid1`, or any predictable source for invite primitives. ✓
- The primary primitive uses `secrets.token_urlsafe`; the secondary uses `secrets.choice`. Both draw from `os.urandom` underneath. ✓

### Sequential-ID enumeration vectors
- `invite_tokens` and `user_invite_tokens` both use `INTEGER PRIMARY KEY AUTOINCREMENT` (`gateway/db.py:62`, `gateway/migrations/113_user_invite_tokens.py:39`). The integer id is **never** exposed in any client-facing route or used as a lookup key by an unauthenticated path.
- All public/auth lookups use the token string:
  - `get_invite_token` → `WHERE token = ?` (`gateway/queries/auth.py:335`).
  - `claim_invite_token` → `WHERE token = ? AND status = 'unclaimed' …` (`gateway/queries/auth.py:362-367`).
  - `redeem_invite_token` → `WHERE token = ? AND used_at IS NULL AND is_active = 1` (`gateway/db_sharing.py:327`).
- The two `WHERE id =` writes that exist (`revoke_invite_token` at `gateway/queries/auth.py:390`, replenish-prune at `gateway/db_sharing.py:399-400`) are admin-only / internal-job-only paths that receive their `id` from a server-side query, not from request input.
- The token's row-`id` is returned to the referrer in `routes_referrals.py:157-162` only as the link from the `referrals` row back to the issued invite — it is never echoed to the invitee or used as a redeem key.
- No `/invite/{id}`-style numeric route exists. The only client-facing numeric path adjacent to this surface is `/api/invite/{code}/accept`, where `{code}` is the **referral** code (random `secrets.choice` over 32-char alphabet, 10 chars = 50 bits — out of scope for this audit, but flagged for awareness as it gates the issuance side). ✓ (no enumeration)

### Target-email pinning (defence-in-depth, not entropy but adjacent)
- `claim_invite_token` enforces `target_email` match (`gateway/queries/auth.py:341-373`). A leaked primary-flow token cannot be redeemed by a different email when one was pinned at mint time. Halves the practical impact of any future entropy-leak bug on the primary primitive. ✓

### Rate-limiting against guessing
- `POST /auth/validate-token` (`gateway/server_features.py:1410-1431`) — 5/min per IP **and** 10/600s per-token-prefix. With 192 bits of primary-token entropy and these limits, brute-forcing remains computationally infeasible. ✓
- `POST /api/invite/{code}/accept` (`gateway/routes_referrals.py:122-129`) — 20/h per IP and 3/day per email. This rate-limits the **issuance** side, not the per-user-invite-token redemption side. The 80-bit secondary primitive's redemption path relies on the generic auth-route limiter only; if entropy is lifted to ≥128 bits per the gap above, this is no longer load-bearing. ✓ (issuance side)

---

## Gaps

| Severity | Location | Gap |
|----------|----------|-----|
| **High** | `gateway/db_sharing.py:262-267` | `user_invite_tokens.token` has only ~80 bits of entropy (16 chars × log2(32)). Below the 128-bit requirement. Swap to `secrets.token_urlsafe(24)` (192 bits, 32 chars), or — if the human-typable alphabet must be retained — lengthen to ≥26 chars. |
| Info | `gateway/db_sharing.py:265` | Comment justifies length on collision grounds only; doesn't acknowledge brute-force threat. Update when the length is lifted. |
| — | — | No sequential-ID enumeration vector found. No non-CSPRNG generator found. Primary `invite_tokens` primitive is conformant. |

---

## Files inspected

- `gateway/queries/auth.py` — primary invite-token generation, lookup, claim, revoke.
- `gateway/db.py` — `invite_tokens` schema, idx, `FIELD_MAX`.
- `gateway/db_sharing.py` — secondary per-user invite tokens.
- `gateway/migrations/113_user_invite_tokens.py`, `gateway/migrations/188_fix_users_invite_token_fk.py` — schema history.
- `gateway/routes_referrals.py` — public referral-flow invite issuance.
- `gateway/public_routes.py` — subscription-checkout invite issuance.
- `gateway/server.py` — admin mint / replace / revoke / `FIELD_MAX`.
- `gateway/server_features.py` — `/auth/validate-token`, `/register`, `/login` token consumption.
- `gateway/security/rate_limiter.py` — auth-route rate-limit decorator.
- `gateway/embed_tokens.py`, `gateway/db_affiliate.py`, `gateway/db_referrals.py` — adjacent token primitives (out of scope but RNG-source spot-checked).
