"""Supabase database layer for the Midterm Elections Dashboard.

Replaces the previous SQLite/aiosqlite implementation. Uses the Supabase
Python client for Postgres storage. All methods are now synchronous (the
Supabase client makes HTTP calls which are inherently async-safe from
FastAPI's perspective when called from async endpoints via threadpool).

User auth (users, sessions) is NO LONGER handled here -- the gateway
manages that. User profiles live in the shared ``profiles`` table.
User IDs are UUID strings, not integers.

Required environment variables:
    SUPABASE_URL            - Your Supabase project URL
    SUPABASE_SERVICE_KEY    - Service role key (server-side, bypasses RLS)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

_client: Optional[Client] = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
                "Create a project at https://supabase.com and set these env vars."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


# ---------------------------------------------------------------------------
# Table names (all prefixed with midterm_)
# ---------------------------------------------------------------------------

T_MARKETS = "midterm_markets"
T_PRICE_HISTORY = "midterm_price_history"
T_POLLING_DATA = "midterm_polling_data"
T_POLLING_AVERAGES = "midterm_polling_averages"
T_DIVERGENCE = "midterm_divergence_snapshots"
T_WATCHLISTS = "midterm_user_watchlists"
T_ALERT_SETTINGS = "midterm_alert_settings"
T_ALERT_HISTORY = "midterm_alert_history"
T_AUDIT_LOG = "midterm_audit_log"
T_PROFILES = "profiles"


class Database:
    """Supabase-backed database for the midterm dashboard.

    Keeps the same public API as the old SQLite version so callers
    (main.py, background tasks) require minimal changes.
    """

    def __init__(self):
        self._client: Optional[Client] = None

    def connect(self):
        """Initialize the Supabase client and verify connectivity."""
        self._client = _get_client()
        # Quick health check
        try:
            self._client.table(T_MARKETS).select("id").limit(0).execute()
            logger.info("Supabase connection OK")
        except Exception as e:
            logger.error("Supabase connection failed: %s", e)
            raise

    def close(self):
        """No-op for Supabase (HTTP client, no persistent connection)."""
        pass

    # === Helper =============================================================

    @property
    def sb(self) -> Client:
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def _parse_outcomes(row: dict) -> dict:
        """Ensure 'outcomes' field is a Python list, not a JSON string."""
        if row and "outcomes" in row:
            o = row["outcomes"]
            if isinstance(o, str):
                row["outcomes"] = json.loads(o)
        return row

    # === Market Data ========================================================

    def upsert_market(self, market: dict):
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        row = {
            "source": market["source"],
            "source_id": market["source_id"],
            "event_id": market.get("event_id"),
            "title": market["title"],
            "event_title": market.get("event_title"),
            "slug": market.get("slug"),
            "race_type": market.get("race_type"),
            "state": market.get("state"),
            "outcomes": outcomes,
            "volume": market.get("volume", 0),
            "liquidity": market.get("liquidity", 0),
            "active": 1 if market.get("active") else 0,
            "closed": 1 if market.get("closed") else 0,
            "end_date": market.get("end_date"),
            "last_updated": market.get("last_updated"),
        }
        self.sb.table(T_MARKETS).upsert(
            row, on_conflict="source,source_id"
        ).execute()

    def upsert_markets_batch(self, markets: list[dict]):
        for market in markets:
            self.upsert_market(market)

    def get_markets(
        self,
        source: str = None,
        race_type: str = None,
        state: str = None,
        active_only: bool = True,
        search: str = None,
        min_volume: float = None,
    ) -> list[dict]:
        q = self.sb.table(T_MARKETS).select("*")
        if source:
            q = q.eq("source", source)
        if race_type:
            q = q.eq("race_type", race_type)
        if state:
            q = q.eq("state", state)
        if active_only:
            q = q.eq("active", 1).or_("closed.is.null,closed.eq.0")
        if search:
            pattern = f"%{search}%"
            q = q.or_(f"title.ilike.{pattern},event_title.ilike.{pattern}")
        if min_volume is not None:
            q = q.gte("volume", min_volume)
        q = q.order("volume", desc=True)

        resp = q.execute()
        return [self._parse_outcomes(r) for r in (resp.data or [])]

    def get_all_markets(self, active_only: bool = True) -> list[dict]:
        q = self.sb.table(T_MARKETS).select("*")
        if active_only:
            q = q.eq("active", 1)
        q = q.order("volume", desc=True)
        resp = q.execute()
        return [self._parse_outcomes(r) for r in (resp.data or [])]

    # === Price History ======================================================

    def record_price_snapshot(
        self, market_id: int, source: str, prices: dict, volume: float = None
    ):
        row = {
            "market_id": market_id,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prices": prices,
            "volume": volume,
        }
        self.sb.table(T_PRICE_HISTORY).insert(row).execute()

    def get_price_history(self, market_id: int, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        resp = (
            self.sb.table(T_PRICE_HISTORY)
            .select("*")
            .eq("market_id", market_id)
            .gte("timestamp", cutoff)
            .order("timestamp")
            .execute()
        )
        results = []
        for row in resp.data or []:
            if isinstance(row.get("prices"), str):
                row["prices"] = json.loads(row["prices"])
            results.append(row)
        return results

    # === Divergence =========================================================

    def record_divergence(self, race_key: str, state: str, race_type: str, data: dict):
        details = data.get("details", {})
        row = {
            "race_key": race_key,
            "state": state,
            "race_type": race_type,
            "polymarket_prob": data.get("polymarket"),
            "kalshi_prob": data.get("kalshi"),
            "predictit_prob": data.get("predictit"),
            "polling_avg": data.get("polling"),
            "max_divergence": data.get("max_divergence"),
            "divergence_details": details,
        }
        self.sb.table(T_DIVERGENCE).insert(row).execute()

    def get_divergence_history(
        self, race_key: str = None, days: int = 30
    ) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        q = self.sb.table(T_DIVERGENCE).select("*").gte("snapshot_time", cutoff)
        if race_key:
            q = q.eq("race_key", race_key).order("snapshot_time")
        else:
            q = q.order("max_divergence", desc=True)

        resp = q.execute()
        results = []
        for row in resp.data or []:
            dd = row.get("divergence_details")
            if isinstance(dd, str):
                row["divergence_details"] = json.loads(dd)
            results.append(row)
        return results

    # === Polling Data =======================================================

    def store_polls_batch(self, polls: list[dict]):
        rows = []
        for p in polls:
            rows.append({
                "poll_type": p.get("poll_type"),
                "state": p.get("state"),
                "candidate": p.get("candidate"),
                "party": p.get("party"),
                "percentage": p.get("percentage"),
                "pollster": p.get("pollster"),
                "sample_size": p.get("sample_size"),
                "population": p.get("population"),
                "start_date": p.get("start_date"),
                "end_date": p.get("end_date"),
                "race_id": p.get("race_id"),
                "source": p.get("source", "538"),
            })
        if rows:
            # Use upsert with ignore duplicates behavior -- Supabase doesn't
            # have INSERT OR IGNORE natively, so we insert and handle errors
            # gracefully.  For polling data without a unique constraint in the
            # Supabase schema, a plain insert works.
            self.sb.table(T_POLLING_DATA).insert(rows).execute()

    def get_polls(self, state: str = None, poll_type: str = None) -> list[dict]:
        q = self.sb.table(T_POLLING_DATA).select("*")
        if state:
            q = q.eq("state", state)
        if poll_type:
            q = q.eq("poll_type", poll_type)
        q = q.order("end_date", desc=True).limit(500)
        resp = q.execute()
        return resp.data or []

    def get_recent_polls(self, limit: int = 50) -> list[dict]:
        resp = (
            self.sb.table(T_POLLING_DATA)
            .select("*")
            .order("end_date", desc=True)
            .order("id", desc=True)
            .limit(min(limit, 200))
            .execute()
        )
        return resp.data or []

    # === User Watchlists ====================================================

    def get_watchlist(self, user_id: str) -> list[dict]:
        resp = (
            self.sb.table(T_WATCHLISTS)
            .select("race_key, created_at")
            .eq("user_id", user_id)
            .execute()
        )
        return resp.data or []

    def add_to_watchlist(self, user_id: str, race_key: str):
        self.sb.table(T_WATCHLISTS).upsert(
            {"user_id": user_id, "race_key": race_key},
            on_conflict="user_id,race_key",
        ).execute()

    def remove_from_watchlist(self, user_id: str, race_key: str):
        self.sb.table(T_WATCHLISTS).delete().eq(
            "user_id", user_id
        ).eq("race_key", race_key).execute()

    # === Alert Settings =====================================================

    def get_alerts(self, user_id: str) -> list[dict]:
        resp = (
            self.sb.table(T_ALERT_SETTINGS)
            .select("*")
            .eq("user_id", user_id)
            .eq("enabled", 1)
            .execute()
        )
        return resp.data or []

    def upsert_alert(self, user_id: str, race_key: str, threshold: float = 5.0):
        self.sb.table(T_ALERT_SETTINGS).upsert(
            {
                "user_id": user_id,
                "race_key": race_key,
                "threshold": threshold,
                "enabled": 1,
            },
            on_conflict="user_id,race_key,alert_type",
        ).execute()

    # === Audit Log ==========================================================

    def log_action(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        details: str = None,
        ip: str = None,
    ):
        row = {
            "action": action,
            "details": details,
            "ip_address": ip,
        }
        if user_id:
            row["user_id"] = user_id
        self.sb.table(T_AUDIT_LOG).insert(row).execute()

    def get_audit_log(self, user_id: str = None, limit: int = 100) -> list[dict]:
        q = self.sb.table(T_AUDIT_LOG).select("*")
        if user_id:
            q = q.eq("user_id", user_id)
        q = q.order("created_at", desc=True).limit(limit)
        resp = q.execute()
        return resp.data or []

    # === Admin Analytics ====================================================
    # NOTE: User-related admin stats (users_by_tier, total_users, subscriptions,
    # new_users, active_sessions, daily_signups, growth, churn) are now in the
    # gateway admin panel since user management moved there.  The midterm admin
    # endpoints only report data-layer stats.

    def get_admin_stats(self) -> dict:
        stats = {}

        # Active markets
        resp = (
            self.sb.table(T_MARKETS)
            .select("id", count="exact")
            .eq("active", 1)
            .execute()
        )
        stats["active_markets"] = resp.count or 0

        # Price snapshots
        resp = (
            self.sb.table(T_PRICE_HISTORY)
            .select("id", count="exact")
            .execute()
        )
        stats["price_snapshots"] = resp.count or 0

        # Divergence snapshots
        resp = (
            self.sb.table(T_DIVERGENCE)
            .select("id", count="exact")
            .execute()
        )
        stats["divergence_snapshots"] = resp.count or 0

        return stats

    def get_all_users(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch user profiles from the shared profiles table."""
        resp = (
            self.sb.table(T_PROFILES)
            .select("id, email, display_name, tier, created_at, last_login")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data or []
