"""Queries extracted from gateway/db.py — markets domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db
from db import _fts_sanitize_query  # noqa: F401 — stays bound; shared helper


def search_markets(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against market_snapshots (latest per slug)."""
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with db.conn() as c:
        try:
            return c.execute(
                """
                SELECT ms.market_slug, ms.market_question, ms.category,
                       ms.yes_price, ms.snapshotted_at,
                       snippet(markets_fts, 1, '<mark>', '</mark>', '…', 16) AS highlight,
                       bm25(markets_fts) AS rank
                FROM markets_fts
                JOIN market_snapshots ms ON ms.id = markets_fts.rowid
                WHERE markets_fts MATCH ?
                  AND ms.id = (
                      SELECT MAX(id) FROM market_snapshots
                      WHERE market_slug = ms.market_slug
                  )
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def insert_market_snapshot(
    market_slug: str,
    yes_price: float,
    snapshotted_at: Optional[int] = None,
    market_question: Optional[str] = None,
    category: Optional[str] = None,
    no_price: Optional[float] = None,
    volume: Optional[float] = None,
    source_platform: str = "polymarket",
) -> int:
    """Insert a new market snapshot.

    If market_question or category is omitted, we backfill from the most
    recent snapshot for this slug — dashboard backends typically only send
    the question on the first ingest and push price-only updates after that.
    Without this backfill the FTS index would only contain the first row,
    and the "latest snapshot per slug" filter in search_markets() would
    yield zero hits once a price update arrives.
    """
    slug = market_slug.strip()
    ts = snapshotted_at if snapshotted_at is not None else int(time.time())
    with db.conn() as c:
        if market_question is None or category is None:
            prev = c.execute(
                "SELECT market_question, category FROM market_snapshots "
                "WHERE market_slug = ? ORDER BY snapshotted_at DESC LIMIT 1",
                (slug,),
            ).fetchone()
            if prev:
                if market_question is None:
                    market_question = prev["market_question"]
                if category is None:
                    category = prev["category"]
        cur = c.execute(
            "INSERT INTO market_snapshots (market_slug, market_question, category, "
            "yes_price, no_price, volume, snapshotted_at, source_platform) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, market_question, category,
             float(yes_price), no_price, volume, ts, source_platform),
        )
        row_id = cur.lastrowid
    # Realtime fan-out outside the transaction so a slow hub never holds
    # the sqlite lock. Best-effort — a failed broadcast just means this
    # tick doesn't reach live charts; the next snapshot (60s later) will.
    try:
        from realtime.broadcast import emit_price_tick
        emit_price_tick(
            market_slug=slug,
            yes_price=float(yes_price),
            no_price=(float(no_price) if no_price is not None else None),
            volume_24h=(float(volume) if volume is not None else None),
        )
    except Exception:
        pass
    return row_id


def get_market_history(market_slug: str, limit: int = 500) -> list[sqlite3.Row]:
    """Snapshots for a market ordered ascending by time — suitable for charting."""
    with db.conn() as c:
        return c.execute(
            "SELECT yes_price, snapshotted_at, volume FROM market_snapshots "
            "WHERE market_slug = ? ORDER BY snapshotted_at ASC LIMIT ?",
            (market_slug.strip(), limit),
        ).fetchall()


def get_latest_market_snapshot(market_slug: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM market_snapshots WHERE market_slug = ? "
            "ORDER BY snapshotted_at DESC LIMIT 1",
            (market_slug.strip(),),
        ).fetchone()


def get_market_snapshot_at(market_slug: str, at_time: int) -> Optional[sqlite3.Row]:
    """Return the snapshot closest to at_time (<=) for market_slug, or None.

    Used to annotate prediction markers with "market odds at the time this
    prediction was made".
    """
    with db.conn() as c:
        return c.execute(
            "SELECT yes_price, snapshotted_at FROM market_snapshots "
            "WHERE market_slug = ? AND snapshotted_at <= ? "
            "ORDER BY snapshotted_at DESC LIMIT 1",
            (market_slug.strip(), int(at_time)),
        ).fetchone()


def get_prediction_markers_for_market(market_slug: str) -> list[sqlite3.Row]:
    """Predictions tied to this market, joined with credibility + nearest snapshot.

    Used by the historical odds chart as the marker layer.
    """
    with db.conn() as c:
        return c.execute(
            """
            SELECT p.id, p.source_handle, p.content, p.direction,
                   p.predicted_probability, p.extracted_at,
                   sc.global_credibility,
                   (
                     SELECT yes_price FROM market_snapshots
                     WHERE market_slug = ? AND snapshotted_at <= p.extracted_at
                     ORDER BY snapshotted_at DESC LIMIT 1
                   ) AS market_yes_price_at_time
            FROM predictions p
            LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
            WHERE p.market_id = ?
            ORDER BY p.extracted_at ASC
            """,
            (market_slug.strip(), market_slug.strip()),
        ).fetchall()


def upsert_market_credential(
    user_id: int,
    source: str,
    *,
    kalshi_token: Optional[str] = None,
    kalshi_member_id: Optional[str] = None,
    kalshi_token_expires_at: Optional[int] = None,
    polymarket_wallet_address: Optional[str] = None,
) -> None:
    """Insert or update market credentials for a user/source pair. Always
    marks the row is_active=1 so reconnecting after an expiry reactivates."""
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO user_market_credentials
                (user_id, source, kalshi_token, kalshi_member_id, kalshi_token_expires_at,
                 polymarket_wallet_address, connected_at, last_used_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id, source) DO UPDATE SET
                kalshi_token = excluded.kalshi_token,
                kalshi_member_id = excluded.kalshi_member_id,
                kalshi_token_expires_at = excluded.kalshi_token_expires_at,
                polymarket_wallet_address = excluded.polymarket_wallet_address,
                connected_at = excluded.connected_at,
                last_used_at = excluded.last_used_at,
                is_active = 1
            """,
            (user_id, source, kalshi_token, kalshi_member_id,
             kalshi_token_expires_at, polymarket_wallet_address, now, now),
        )


def get_market_credential(user_id: int, source: str) -> Optional[sqlite3.Row]:
    """Get stored market credentials for a user/source."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_market_credentials WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()


def get_all_market_credentials(user_id: int) -> list[sqlite3.Row]:
    """Get all market credentials for a user."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_market_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchall()


def delete_market_credential(user_id: int, source: str) -> bool:
    """Delete market credentials. Returns True if a row was deleted."""
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM user_market_credentials WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        return cur.rowcount > 0


def update_market_credential_last_used(user_id: int, source: str) -> None:
    """Touch the last_used_at timestamp."""
    with db.conn() as c:
        c.execute(
            "UPDATE user_market_credentials SET last_used_at = ? WHERE user_id = ? AND source = ?",
            (int(time.time()), user_id, source),
        )


def record_bet(
    user_id: int,
    source: str,
    external_order_id: str,
    market_id: str,
    market_title: str,
    side: str,
    amount_usd: float,
    price_at_bet: float,
    status: str = "pending",
) -> int:
    """Record a bet in history. Returns the row ID."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO user_bet_history "
            "(user_id, source, external_order_id, market_id, market_title, side, amount_usd, price_at_bet, status, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, source, external_order_id, market_id, market_title,
             side, amount_usd, price_at_bet, status, int(time.time())),
        )
        return cur.lastrowid


def list_bet_history(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """Get recent bet history for a user."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_bet_history WHERE user_id = ? ORDER BY placed_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def set_market_credential_active(user_id: int, source: str, active: bool) -> None:
    """Flip is_active on a user's market connection without deleting the row.

    Used when upstream credentials expire (e.g. Kalshi 401) so the UI can
    prompt for a reconnect instead of silently dropping the account."""
    with db.conn() as c:
        c.execute(
            "UPDATE user_market_credentials SET is_active = ? WHERE user_id = ? AND source = ?",
            (1 if active else 0, user_id, source),
        )


def disconnect_market_credential(user_id: int, source: str) -> bool:
    """User-initiated disconnect. Keep the row so the UI can show
    'Reconnect', but scrub the Kalshi token and mark the row inactive.
    Returns True if a row was updated."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_market_credentials "
            "SET is_active = 0, kalshi_token = NULL, kalshi_token_expires_at = NULL "
            "WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        return cur.rowcount > 0


def upsert_user_position(
    user_id: int,
    platform: str,
    market_id: str,
    market_title: str,
    side: str,
    shares: float,
    avg_entry_price: float,
    current_price: float,
    unrealised_pnl: float,
    position_value_usd: float,
) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO user_positions
                (user_id, platform, market_id, market_title, side, shares,
                 avg_entry_price, current_price, unrealised_pnl,
                 position_value_usd, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, platform, market_id, side) DO UPDATE SET
                market_title       = excluded.market_title,
                shares             = excluded.shares,
                avg_entry_price    = excluded.avg_entry_price,
                current_price      = excluded.current_price,
                unrealised_pnl     = excluded.unrealised_pnl,
                position_value_usd = excluded.position_value_usd,
                last_synced_at     = excluded.last_synced_at
            """,
            (user_id, platform, market_id, market_title, side, shares,
             avg_entry_price, current_price, unrealised_pnl,
             position_value_usd, now),
        )


def get_user_positions(
    user_id: int, platform: Optional[str] = None,
) -> list[sqlite3.Row]:
    with db.conn() as c:
        if platform:
            return c.execute(
                "SELECT * FROM user_positions WHERE user_id = ? AND platform = ? "
                "ORDER BY position_value_usd DESC",
                (user_id, platform),
            ).fetchall()
        return c.execute(
            "SELECT * FROM user_positions WHERE user_id = ? "
            "ORDER BY position_value_usd DESC",
            (user_id,),
        ).fetchall()


def delete_user_positions(user_id: int, platform: Optional[str] = None) -> int:
    """Drop cached positions. Platform-scoped if given. Returns rows deleted."""
    with db.conn() as c:
        if platform:
            cur = c.execute(
                "DELETE FROM user_positions WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            )
        else:
            cur = c.execute(
                "DELETE FROM user_positions WHERE user_id = ?", (user_id,),
            )
        return cur.rowcount


def prune_stale_positions(
    user_id: int, platform: str, keep_keys: set[tuple[str, str]],
) -> int:
    """Delete rows for a platform that are NOT in *keep_keys* (set of
    (market_id, side) tuples). Used after a sync to drop positions the
    exchange no longer reports (closed trades)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT market_id, side FROM user_positions "
            "WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        ).fetchall()
        to_delete = [
            (r["market_id"], r["side"]) for r in rows
            if (r["market_id"], r["side"]) not in keep_keys
        ]
        for mid, side in to_delete:
            c.execute(
                "DELETE FROM user_positions "
                "WHERE user_id = ? AND platform = ? AND market_id = ? AND side = ?",
                (user_id, platform, mid, side),
            )
        return len(to_delete)


def get_portfolio_stats(user_id: int) -> dict:
    """Aggregate stats across cached positions.

    Value/P&L/active come from user_positions; resolved-bet win rate comes
    from user_bet_history (bets with resolved_correct set)."""
    with db.conn() as c:
        agg = c.execute(
            "SELECT "
            " COALESCE(SUM(position_value_usd), 0) AS total_value, "
            " COALESCE(SUM(unrealised_pnl), 0)    AS total_pnl, "
            " COUNT(*) AS active "
            "FROM user_positions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        resolved = c.execute(
            "SELECT "
            " COUNT(*)  AS total, "
            " SUM(CASE WHEN resolved_correct = 1 THEN 1 ELSE 0 END) AS wins "
            "FROM user_bet_history "
            "WHERE user_id = ? AND resolved_correct IS NOT NULL",
            (user_id,),
        ).fetchone()
    total_bets = int(resolved["total"] or 0)
    wins = int(resolved["wins"] or 0)
    win_rate = (wins / total_bets) if total_bets else None
    return {
        "total_value_usd": round(float(agg["total_value"]), 2),
        "unrealised_pnl_usd": round(float(agg["total_pnl"]), 2),
        "active_positions": int(agg["active"]),
        "resolved_bets": total_bets,
        "winning_bets": wins,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
    }


def get_user_bankroll(user_id: int) -> dict:
    """Return the user's stated bankroll and Kelly fraction preference."""
    with db.conn() as c:
        row = c.execute(
            "SELECT bankroll, kelly_fraction FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    if not row:
        return {"bankroll": None, "kelly_fraction": 0.5}
    return {
        "bankroll": float(row["bankroll"]) if row["bankroll"] is not None else None,
        "kelly_fraction": float(row["kelly_fraction"] or 0.5),
    }


def set_user_bankroll(
    user_id: int,
    bankroll: Optional[float] = None,
    kelly_fraction: Optional[float] = None,
) -> None:
    sets: list[str] = []
    params: list = []
    if bankroll is not None:
        sets.append("bankroll = ?")
        params.append(float(bankroll))
    if kelly_fraction is not None:
        sets.append("kelly_fraction = ?")
        params.append(float(kelly_fraction))
    if not sets:
        return
    params.append(user_id)
    with db.conn() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params))


def get_trading_addon_status(user_id: int) -> dict:
    """Return trading add-on status for a user."""
    with db.conn() as c:
        row = c.execute(
            "SELECT trading_addon_active, trading_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"active": False, "period_end": None}
    active = bool(row["trading_addon_active"])
    period_end = row["trading_addon_period_end"]
    # Check expiry
    if active and period_end and period_end <= int(time.time()):
        active = False
    return {"active": active, "period_end": period_end}


def set_trading_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    """Admin toggle for trading add-on."""
    with db.conn() as c:
        c.execute(
            "UPDATE users SET trading_addon_active = ?, trading_addon_period_end = ? WHERE id = ?",
            (1 if active else 0, period_end, user_id),
        )


def has_trading_addon(user_id: int) -> bool:
    """Check if user has active trading add-on (or is admin/enterprise)."""
    with db.conn() as c:
        row = c.execute(
            "SELECT is_admin, trading_addon_active, trading_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return False
    if row["is_admin"]:
        return True
    if not row["trading_addon_active"]:
        return False
    period_end = row["trading_addon_period_end"]
    if period_end and period_end <= int(time.time()):
        return False
    return True


def get_market_categorisation(market_id: str) -> Optional[sqlite3.Row]:
    if not market_id:
        return None
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM market_categorisations "
            "WHERE market_id = ? AND cache_valid_until > ?",
            (market_id, now),
        ).fetchone()


def upsert_market_categorisation(market_id: str, payload: dict) -> int:
    import json as _json
    tags_json = _json.dumps(payload.get("tags") or [])
    with db.conn() as c:
        c.execute("DELETE FROM market_categorisations WHERE market_id = ?", (market_id,))
        cur = c.execute(
            """
            INSERT INTO market_categorisations (
                market_id, market_title, generated_at, generated_by,
                cache_valid_until, primary_category, sub_category, tags,
                political_leaning, sensitivity,
                insider_trading_relevant, environmental_relevant,
                requires_expert_knowledge
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market_id,
                payload.get("market_title") or "",
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 365 * 86400)),
                payload.get("primary_category") or "other",
                payload.get("sub_category"),
                tags_json,
                payload.get("political_leaning"),
                payload.get("sensitivity") or "normal",
                1 if payload.get("insider_trading_relevant") else 0,
                1 if payload.get("environmental_relevant") else 0,
                1 if payload.get("requires_expert_knowledge") else 0,
            ),
        )
        return cur.lastrowid


def list_uncategorised_market_ids(market_ids: list[str]) -> list[str]:
    if not market_ids:
        return []
    now = int(time.time())
    placeholders = ",".join("?" * len(market_ids))
    with db.conn() as c:
        rows = c.execute(
            f"SELECT market_id FROM market_categorisations "
            f"WHERE market_id IN ({placeholders}) AND cache_valid_until > ?",
            (*market_ids, now),
        ).fetchall()
    cached = {r["market_id"] for r in rows}
    return [mid for mid in market_ids if mid not in cached]


__all__ = [
    'search_markets',
    'insert_market_snapshot',
    'get_market_history',
    'get_latest_market_snapshot',
    'get_market_snapshot_at',
    'get_prediction_markers_for_market',
    'upsert_market_credential',
    'get_market_credential',
    'get_all_market_credentials',
    'delete_market_credential',
    'update_market_credential_last_used',
    'record_bet',
    'list_bet_history',
    'set_market_credential_active',
    'disconnect_market_credential',
    'upsert_user_position',
    'get_user_positions',
    'delete_user_positions',
    'prune_stale_positions',
    'get_portfolio_stats',
    'get_user_bankroll',
    'set_user_bankroll',
    'get_trading_addon_status',
    'set_trading_addon',
    'has_trading_addon',
    'get_market_categorisation',
    'upsert_market_categorisation',
    'list_uncategorised_market_ids',
]
