"""Unified read API over ``user_positions``.

Both platforms upsert into the same table; this module is the single
place route handlers + jobs go to read. Everything returned has already
been normalised by the platform-specific sync jobs, so callers don't
need to know whether a row came from Polymarket or Kalshi.

Shapes a summary dict the dashboard consumes directly:

  {
    "total_value_usd": 1234.56,
    "unrealised_pnl_usd": -12.34,
    "realised_pnl_usd": 45.67,
    "active_positions": 8,
    "by_platform": {"polymarket": {...}, "kalshi": {...}},
    "positions": [...]
  }
"""

from __future__ import annotations

from typing import Optional


def list_positions(user_id: int, platform: Optional[str] = None) -> list[dict]:
    """Return all tracked positions for ``user_id``, newest first."""
    import db
    sql = (
        "SELECT * FROM user_positions WHERE user_id = ? "
        "AND (? IS NULL OR platform = ?) "
        "ORDER BY last_synced_at DESC, id DESC"
    )
    with db.conn() as c:
        rows = c.execute(sql, (user_id, platform, platform)).fetchall()
    return [dict(r) for r in rows]


def summary(user_id: int) -> dict:
    """Aggregate stats for the portfolio dashboard card."""
    rows = list_positions(user_id)
    by_platform: dict[str, dict] = {}
    total_value = 0.0
    unrealised = 0.0
    realised = 0.0
    active = 0
    for r in rows:
        pv = float(r.get("position_value_usd") or 0)
        upl = float(r.get("unrealised_pnl_usd") or 0)
        rpl = float(r.get("realised_pnl_usd") or 0)
        total_value += pv
        unrealised += upl
        realised += rpl
        if (r.get("shares") or 0) > 0:
            active += 1
        p = r.get("platform") or "unknown"
        bucket = by_platform.setdefault(p, {
            "value_usd": 0.0, "unrealised_pnl_usd": 0.0, "realised_pnl_usd": 0.0,
            "active_positions": 0,
        })
        bucket["value_usd"] += pv
        bucket["unrealised_pnl_usd"] += upl
        bucket["realised_pnl_usd"] += rpl
        if (r.get("shares") or 0) > 0:
            bucket["active_positions"] += 1

    return {
        "total_value_usd": round(total_value, 2),
        "unrealised_pnl_usd": round(unrealised, 2),
        "realised_pnl_usd": round(realised, 2),
        "active_positions": active,
        "by_platform": {
            k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                for kk, vv in v.items()}
            for k, v in by_platform.items()
        },
        "positions": rows,
    }
