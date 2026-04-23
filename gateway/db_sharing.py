"""DB layer for shareable artifacts + per-user invite tokens.

Parallel sibling to ``db.py`` / ``db_referrals.py`` / ``db_affiliate.py``
— the pattern of "one domain per db_* module, re-using the same
``db.conn()`` connection" is established; this just extends it to the
sharing / invite feature set.

Covers:

  * Create / read / expire shared_market_cards, shared_source_cards,
    shared_predictions (migrations 110-112).
  * Per-user invite tokens with monthly replenishment semantics
    (migration 113).
  * Share-metrics logging (migration 114) including conversion
    linkage back to a signup event.

All functions assume migrations 110-114 have run. If a table is
missing the sqlite3 error propagates — we don't defensively catch
it, because masking a schema-drift bug here would make it invisible
for days until an admin page error got reported.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Optional

import db
import share_tokens


# ── Monthly invite allotments (keep the one source of truth) ────────

INVITE_ALLOTMENT_BY_TIER: dict[str, int] = {
    "trader": 2,
    "pro": 5,
    "enterprise": 20,
}
# Unused tokens cap at 2× the monthly allotment. Without the cap, a
# light-user would accumulate a pile of never-used tokens and create a
# spam vector if their account were ever compromised.
ROLLOVER_MULTIPLIER = 2


# ── Shared market cards (migration 110) ─────────────────────────────


def create_shared_market(
    *, market_slug: str, sharer_user_id: int, sharer_handle: Optional[str],
    ttl_seconds: int = share_tokens.DEFAULT_TTL_SECONDS,
) -> dict:
    """Mint + persist a new shared_market_cards row.

    Two-phase insert: we INSERT with a placeholder token (a random
    string) first to get the autoincrement id, then UPDATE with the
    final HMAC-signed token that embeds that id. This keeps the DB
    row_id inside the signed payload — a tampered token that points at
    a different row won't verify.

    Returns the full row as a dict (token + URL + expiry) so callers
    don't need a follow-up SELECT."""
    now = int(time.time())
    placeholder = secrets.token_urlsafe(16)
    expires_at = now + ttl_seconds
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO shared_market_cards "
            "(token, market_slug, sharer_user_id, sharer_handle, "
            " created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (placeholder, market_slug, sharer_user_id, sharer_handle,
             now, expires_at),
        )
        row_id = cur.lastrowid
        token, _ = share_tokens.encode(
            kind="m", row_id=row_id, sharer_user_id=sharer_user_id,
            ttl_seconds=ttl_seconds, now=now,
        )
        c.execute(
            "UPDATE shared_market_cards SET token = ? WHERE id = ?",
            (token, row_id),
        )
    return {
        "id": row_id,
        "token": token,
        "market_slug": market_slug,
        "sharer_user_id": sharer_user_id,
        "sharer_handle": sharer_handle,
        "created_at": now,
        "expires_at": expires_at,
    }


def get_shared_market(token: str) -> Optional[sqlite3.Row]:
    """Resolve a signed token to its row. Does NOT verify the signature
    (that's the route-layer job via ``share_tokens.decode``) — this is
    the cheap index lookup after verification succeeds."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM shared_market_cards WHERE token = ?",
            (token,),
        ).fetchone()


def record_shared_market_view(row_id: int) -> None:
    """Bump view counter + last_viewed_at. Called on every successful
    GET /s/m/{token}. Separate from share_metrics so the card row
    itself shows an always-current "N views" counter."""
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE shared_market_cards "
            "SET view_count = view_count + 1, last_viewed_at = ? "
            "WHERE id = ?",
            (now, row_id),
        )


# ── Shared source cards (migration 111) ─────────────────────────────


def create_shared_source(
    *, source_handle: str, sharer_user_id: int, sharer_handle: Optional[str],
    ttl_seconds: int = share_tokens.DEFAULT_TTL_SECONDS,
) -> dict:
    now = int(time.time())
    placeholder = secrets.token_urlsafe(16)
    expires_at = now + ttl_seconds
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO shared_source_cards "
            "(token, source_handle, sharer_user_id, sharer_handle, "
            " created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (placeholder, source_handle, sharer_user_id, sharer_handle,
             now, expires_at),
        )
        row_id = cur.lastrowid
        token, _ = share_tokens.encode(
            kind="s", row_id=row_id, sharer_user_id=sharer_user_id,
            ttl_seconds=ttl_seconds, now=now,
        )
        c.execute(
            "UPDATE shared_source_cards SET token = ? WHERE id = ?",
            (token, row_id),
        )
    return {
        "id": row_id,
        "token": token,
        "source_handle": source_handle,
        "sharer_user_id": sharer_user_id,
        "sharer_handle": sharer_handle,
        "created_at": now,
        "expires_at": expires_at,
    }


def get_shared_source(token: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM shared_source_cards WHERE token = ?",
            (token,),
        ).fetchone()


def record_shared_source_view(row_id: int) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE shared_source_cards "
            "SET view_count = view_count + 1, last_viewed_at = ? "
            "WHERE id = ?",
            (now, row_id),
        )


# ── Shared predictions (migration 112) ──────────────────────────────


def create_shared_prediction(
    *, user_prediction_id: int, sharer_user_id: int,
    sharer_handle: Optional[str],
    ttl_seconds: int = share_tokens.DEFAULT_TTL_SECONDS,
) -> Optional[dict]:
    """Mint a shared_predictions row ONLY if the prediction is resolved
    AND resolved_correct = 1.

    Returns None if the prediction doesn't meet the criteria. The
    resolved-correct-only rule is the product invariant that keeps
    ego-shares of losing bets from polluting the surface.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT user_id, resolved_correct, resolved_at "
            "FROM user_predictions WHERE id = ?",
            (user_prediction_id,),
        ).fetchone()
    if not row:
        return None
    if row["user_id"] != sharer_user_id:
        # Can't share someone else's prediction.
        return None
    if not row["resolved_at"] or not row["resolved_correct"]:
        return None

    now = int(time.time())
    placeholder = secrets.token_urlsafe(16)
    expires_at = now + ttl_seconds
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO shared_predictions "
            "(token, user_prediction_id, sharer_user_id, sharer_handle, "
            " created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (placeholder, user_prediction_id, sharer_user_id, sharer_handle,
             now, expires_at),
        )
        row_id = cur.lastrowid
        token, _ = share_tokens.encode(
            kind="p", row_id=row_id, sharer_user_id=sharer_user_id,
            ttl_seconds=ttl_seconds, now=now,
        )
        c.execute(
            "UPDATE shared_predictions SET token = ? WHERE id = ?",
            (token, row_id),
        )
    return {
        "id": row_id,
        "token": token,
        "user_prediction_id": user_prediction_id,
        "sharer_user_id": sharer_user_id,
        "sharer_handle": sharer_handle,
        "created_at": now,
        "expires_at": expires_at,
    }


def get_shared_prediction(token: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM shared_predictions WHERE token = ?",
            (token,),
        ).fetchone()


def record_shared_prediction_view(row_id: int) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE shared_predictions "
            "SET view_count = view_count + 1, last_viewed_at = ? "
            "WHERE id = ?",
            (now, row_id),
        )


# ── Per-user invite tokens (migration 113) ──────────────────────────


def _mint_invite_token_string() -> str:
    """Human-typable invite code: 16 chars from an unambiguous alphabet.
    Same shape as ``db_referrals.generate_referral_code`` but longer
    (single-use so we want more entropy). Collision odds negligible."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def count_unused_invite_tokens(user_id: int) -> int:
    """For the /settings/invites balance badge."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM user_invite_tokens "
            "WHERE user_id = ? AND is_active = 1 AND used_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(row["n"] if row else 0)


def list_unused_invite_tokens(user_id: int) -> list[sqlite3.Row]:
    """Return the full set of unused, active tokens for a user. The
    UI renders these as copyable codes so the user can hand one out
    without going through the invite/{code} flow."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_invite_tokens "
            "WHERE user_id = ? AND is_active = 1 AND used_at IS NULL "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()


def mint_invite_token_for_user(
    *, user_id: int, tier: str, source: str = "monthly_replenish",
) -> str:
    """Insert one new active token. Caller (the replenish job) supplies
    the tier so we record the grant context even if the user's tier
    changes later."""
    t = _mint_invite_token_string()
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO user_invite_tokens "
            "(token, user_id, tier_at_grant, created_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (t, user_id, tier, now, source),
        )
    return t


def redeem_invite_token(
    *, token: str, redeemed_by_user_id: int,
) -> Optional[int]:
    """Atomically mark a user_invite_token as used. Returns the
    user_id of the original owner on success, None if the token is
    invalid / already used / revoked.

    Idempotency: the UPDATE is guarded on ``used_at IS NULL AND
    is_active = 1`` so a concurrent redeem by two different users
    produces exactly one winner."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_invite_tokens "
            "SET used_at = ?, used_by_user_id = ? "
            "WHERE token = ? AND used_at IS NULL AND is_active = 1",
            (now, redeemed_by_user_id, token),
        )
        if cur.rowcount == 0:
            return None
        row = c.execute(
            "SELECT user_id FROM user_invite_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    return int(row["user_id"]) if row else None


def revoke_invite_token(token: str) -> bool:
    """Admin-only. Flips ``is_active`` off without deleting the row so
    the token's history (owner, grant context) stays auditable."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_invite_tokens SET is_active = 0 WHERE token = ?",
            (token,),
        )
        return cur.rowcount > 0


def replenish_invites_for_user(
    *, user_id: int, tier: str, yyyymm: int,
) -> dict:
    """Grant this user's monthly allotment if they haven't already been
    replenished for *yyyymm*. Enforces the 2× rollover cap by pruning
    the oldest un-redeemed tokens before minting new ones when the
    resulting total would exceed the cap.

    Called by the monthly cron; safe to call multiple times within the
    same ``yyyymm`` window (idempotent — the
    ``invites_replenished_yyyymm`` guard skips the second call).

    Returns ``{"granted": N, "pruned": M, "skipped": bool}``."""
    allotment = INVITE_ALLOTMENT_BY_TIER.get(tier, 0)
    if allotment <= 0:
        return {"granted": 0, "pruned": 0, "skipped": True}

    with db.conn() as c:
        u = c.execute(
            "SELECT invites_replenished_yyyymm FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if u and u["invites_replenished_yyyymm"] == yyyymm:
        return {"granted": 0, "pruned": 0, "skipped": True}

    cap = allotment * ROLLOVER_MULTIPLIER
    current_unused = count_unused_invite_tokens(user_id)
    # How many we can safely mint without breaking the cap:
    allowed = max(0, cap - current_unused)
    to_mint = min(allotment, allowed)

    pruned = 0
    if allotment > allowed:
        # We want to mint *allotment*, but that would blow past cap.
        # Prune (allotment - allowed) oldest unused tokens so the user
        # always gets a fresh full allotment; discards the staleness
        # rather than silently granting fewer.
        to_prune = allotment - allowed
        with db.conn() as c:
            rows = c.execute(
                "SELECT id FROM user_invite_tokens "
                "WHERE user_id = ? AND is_active = 1 AND used_at IS NULL "
                "ORDER BY created_at ASC LIMIT ?",
                (user_id, to_prune),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join(["?"] * len(ids))
                c.execute(
                    f"UPDATE user_invite_tokens SET is_active = 0 "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
                pruned = len(ids)
        to_mint = allotment

    for _ in range(to_mint):
        mint_invite_token_for_user(user_id=user_id, tier=tier)

    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE users SET invites_replenished_yyyymm = ?, "
            "invites_replenished_at = ? WHERE id = ?",
            (yyyymm, now, user_id),
        )
    return {"granted": to_mint, "pruned": pruned, "skipped": False}


# ── Share metrics (migration 114) ───────────────────────────────────


_VALID_SHARE_TYPES: frozenset[str] = frozenset({"market", "source", "prediction"})


def _classify_referrer(referer_header: Optional[str]) -> str:
    """Bucket a full Referer URL into a coarse label. We do NOT store
    the raw URL — that's a privacy leak with no analytic value."""
    if not referer_header:
        return "direct"
    h = referer_header.lower()
    if "twitter.com" in h or "x.com" in h or "t.co/" in h:
        return "twitter"
    if "linkedin.com" in h:
        return "linkedin"
    if "slack.com" in h:
        return "slack"
    if "reddit.com" in h:
        return "reddit"
    if "hn.algolia.com" in h or "news.ycombinator.com" in h:
        return "hackernews"
    if "facebook.com" in h or "fb.me" in h:
        return "facebook"
    return "other"


def record_share_view(
    *, share_type: str, share_id: int,
    referer: Optional[str], cf_country: Optional[str],
) -> int:
    """Write one share_metrics row. Returns the new row id so the
    route can set a cookie tying a subsequent signup back to this
    specific view (see ``link_share_to_signup``)."""
    if share_type not in _VALID_SHARE_TYPES:
        raise ValueError(f"invalid share_type: {share_type!r}")
    referrer_bucket = _classify_referrer(referer)
    country = (cf_country or "").strip().upper() or None
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO share_metrics "
            "(share_type, share_id, referrer, viewer_country, viewed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (share_type, share_id, referrer_bucket, country, now),
        )
        return int(cur.lastrowid)


def link_share_to_signup(share_metric_id: int, user_id: int) -> bool:
    """Mark a share_metrics row as converted. Called from the signup
    route when the visitor's session cookie carries a share-metric id
    set during their first share-URL view."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE share_metrics SET signed_up = 1, signed_up_user_id = ?, "
            "signed_up_at = ? WHERE id = ? AND signed_up = 0",
            (user_id, now, share_metric_id),
        )
        return cur.rowcount > 0


def get_sharer_for_share_metric(share_metric_id: int) -> Optional[int]:
    """Resolve the sharer's user_id for a share_metrics row. Joins
    back into the correct shared_* table by ``share_type`` — nullable
    because an expired / deleted share still leaves the metric row
    behind, and we don't want callers to crash when the table lookup
    returns nothing.

    Used by the auth_register attribution path to credit the sharer
    with a referral reward via db_referrals.create_referral. Kept
    here (not in db_referrals) because the schema coupling is in
    this module — db_referrals doesn't know share_metrics exists."""
    with db.conn() as c:
        mrow = c.execute(
            "SELECT share_type, share_id FROM share_metrics WHERE id = ?",
            (share_metric_id,),
        ).fetchone()
        if not mrow:
            return None
        table_by_type = {
            "market": "shared_market_cards",
            "source": "shared_source_cards",
            "prediction": "shared_predictions",
        }
        table = table_by_type.get(mrow["share_type"])
        if not table:
            return None
        # Parameterised share_type, but the table name itself is a
        # whitelisted lookup — not user input — so string interpolation
        # for the table is safe.
        srow = c.execute(
            f"SELECT sharer_user_id FROM {table} WHERE id = ?",
            (mrow["share_id"],),
        ).fetchone()
    return int(srow["sharer_user_id"]) if srow else None


# ── Convenience for /settings/referrals attribution join ────────────


def shares_by_user(user_id: int) -> dict:
    """Per-user share tally for the referrer panel. Returns shape::

        {"market": N, "source": N, "prediction": N, "total_views": N,
         "signups_attributed": N}

    signups_attributed = share_metrics rows with signed_up=1 whose
    share_id belongs to this user across all three share types.
    """
    out = {"market": 0, "source": 0, "prediction": 0,
           "total_views": 0, "signups_attributed": 0}
    with db.conn() as c:
        for share_type, table in (
            ("market", "shared_market_cards"),
            ("source", "shared_source_cards"),
            ("prediction", "shared_predictions"),
        ):
            row = c.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(view_count), 0) AS v "
                f"FROM {table} WHERE sharer_user_id = ?",
                (user_id,),
            ).fetchone()
            out[share_type] = int(row["n"] if row else 0)
            out["total_views"] += int(row["v"] if row else 0)

        # Signups: join share_metrics back to each share-type table to
        # match rows owned by this user. One query per type, union'd.
        row = c.execute(
            """
            SELECT COUNT(*) AS n FROM share_metrics sm
             WHERE sm.signed_up = 1
               AND (
                    (sm.share_type = 'market' AND sm.share_id IN
                         (SELECT id FROM shared_market_cards WHERE sharer_user_id = ?))
                 OR (sm.share_type = 'source' AND sm.share_id IN
                         (SELECT id FROM shared_source_cards WHERE sharer_user_id = ?))
                 OR (sm.share_type = 'prediction' AND sm.share_id IN
                         (SELECT id FROM shared_predictions WHERE sharer_user_id = ?))
               )
            """,
            (user_id, user_id, user_id),
        ).fetchone()
        out["signups_attributed"] = int(row["n"] if row else 0)
    return out
