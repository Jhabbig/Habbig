#!/usr/bin/env python3
"""
Database layer for CryptoEdge — Supabase-backed.

Replaces the previous SQLite implementation. Uses Supabase for Postgres
storage via the crypto_* prefixed tables. Auth is handled by the gateway;
this module only manages dashboard-specific data (predictions, watchlists,
alerts, accuracy, Kalshi markets).

Required environment variables:
    SUPABASE_URL            - Your Supabase project URL
    SUPABASE_SERVICE_KEY    - Service role key (server-side, bypasses RLS)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client

log = logging.getLogger("crypto.db")

# ── Supabase client ─────────────────────────────────────────────────────────

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


def init_db() -> None:
    """Verify Supabase connection is working. Called on startup."""
    client = _get_client()
    try:
        client.table("crypto_predictions").select("id").limit(0).execute()
        log.info("Supabase connection OK (crypto dashboard)")
    except Exception as e:
        log.error("Supabase connection failed: %s", e)
        raise


# ── Helper to convert Supabase row to dict ──────────────────────────────────

class Row(dict):
    """Dict subclass that supports both dict['key'] and dict.key access,
    mimicking sqlite3.Row interface for backward compatibility."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def keys(self):
        return super().keys()


def _row(data: Optional[dict]) -> Optional[Row]:
    if data is None:
        return None
    return Row(data)


def _rows(data: list[dict]) -> list[Row]:
    return [Row(d) for d in data]


# ─── Predictions & Accuracy ─────────────────────────────────────────

def log_prediction(ticker: str, window_start: str, pred_direction: str,
                   pred_delta: float, pred_prob: float, confidence: float,
                   ensemble_agreement: str = "", model_details: str = ""):
    """Insert a prediction, ignoring duplicates on (ticker, window_start)."""
    client = _get_client()
    try:
        client.table("crypto_predictions").upsert({
            "ticker": ticker,
            "window_start": window_start,
            "pred_direction": pred_direction,
            "pred_delta": pred_delta,
            "pred_prob": pred_prob,
            "confidence": confidence,
            "ensemble_agreement": ensemble_agreement,
            "model_details": model_details,
        }, on_conflict="ticker,window_start", ignore_duplicates=True).execute()
    except Exception as e:
        log.warning("log_prediction error: %s", e)


def resolve_prediction(ticker: str, window_start: str, actual_direction: str, actual_delta: float):
    """Resolve an open prediction with the actual outcome."""
    client = _get_client()
    # Find the unresolved prediction
    result = client.table("crypto_predictions").select("id, pred_direction").eq(
        "ticker", ticker
    ).eq("window_start", window_start).is_("was_correct", "null").limit(1).execute()

    if not result.data:
        return

    row = result.data[0]
    was_correct = 1 if row["pred_direction"] == actual_direction else 0
    client.table("crypto_predictions").update({
        "actual_direction": actual_direction,
        "actual_delta": actual_delta,
        "was_correct": was_correct,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()


def get_accuracy_stats(ticker: str = None, days: int = 30) -> dict:
    """Compute accuracy statistics from resolved predictions."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = _get_client()

    query = client.table("crypto_predictions").select("*").not_.is_(
        "was_correct", "null"
    ).gt("created_at", since).order("created_at", desc=True)

    if ticker:
        query = query.eq("ticker", ticker)

    result = query.execute()
    rows = result.data

    if not rows:
        return {"total": 0, "correct": 0, "accuracy": 0,
                "high_conf_total": 0, "high_conf_correct": 0, "high_conf_accuracy": 0}

    total = len(rows)
    correct = sum(1 for r in rows if r["was_correct"])
    hc = [r for r in rows if (r.get("confidence") or 0) >= 0.6]
    hc_correct = sum(1 for r in hc if r["was_correct"])

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "high_conf_total": len(hc),
        "high_conf_correct": hc_correct,
        "high_conf_accuracy": hc_correct / len(hc) if hc else 0,
        "avg_mae": sum(abs((r.get("pred_delta") or 0) - (r.get("actual_delta") or 0)) for r in rows) / total,
    }


def get_recent_predictions(ticker: str = None, limit: int = 50) -> list:
    """Fetch the most recent predictions."""
    client = _get_client()
    query = client.table("crypto_predictions").select("*").order("created_at", desc=True).limit(limit)
    if ticker:
        query = query.eq("ticker", ticker)
    result = query.execute()
    return _rows(result.data)


# ─── Watchlists ──────────────────────────────────────────────────────

def get_watchlists(user_id: str) -> list:
    """Get all watchlists for a user."""
    client = _get_client()
    result = client.table("crypto_watchlists").select("*").eq("user_id", user_id).execute()
    return _rows(result.data)


def create_watchlist(user_id: str, name: str, tickers: list) -> int:
    """Create a new watchlist. Returns the new row ID."""
    client = _get_client()
    result = client.table("crypto_watchlists").insert({
        "user_id": user_id,
        "name": name,
        "tickers": json.dumps(tickers),
    }).execute()
    return result.data[0]["id"] if result.data else 0


def update_watchlist(watchlist_id: int, user_id: str, tickers: list):
    """Update the tickers in a watchlist (owner-scoped)."""
    client = _get_client()
    client.table("crypto_watchlists").update({
        "tickers": json.dumps(tickers),
    }).eq("id", watchlist_id).eq("user_id", user_id).execute()


def delete_watchlist(watchlist_id: int, user_id: str):
    """Delete a watchlist (owner-scoped)."""
    client = _get_client()
    client.table("crypto_watchlists").delete().eq(
        "id", watchlist_id
    ).eq("user_id", user_id).execute()


# ─── Alert Preferences ──────────────────────────────────────────────

def get_alert_prefs(user_id: str) -> list:
    """Get all alert preferences for a user."""
    client = _get_client()
    result = client.table("crypto_alert_preferences").select("*").eq("user_id", user_id).execute()
    return _rows(result.data)


def set_alert_pref(user_id: str, ticker: str, min_confidence: float = 0.6,
                   alert_email: bool = True, alert_browser: bool = True):
    """Upsert an alert preference for a user+ticker pair."""
    client = _get_client()
    client.table("crypto_alert_preferences").upsert({
        "user_id": user_id,
        "ticker": ticker,
        "min_confidence": min_confidence,
        "alert_email": 1 if alert_email else 0,
        "alert_browser": 1 if alert_browser else 0,
    }, on_conflict="user_id,ticker").execute()


def get_alert_prefs_for_ticker(ticker: str) -> list:
    """Get all alert preferences for a specific ticker (across all users),
    joining with profiles to get the email."""
    client = _get_client()
    result = client.table("crypto_alert_preferences").select(
        "*, profiles!inner(email)"
    ).eq("ticker", ticker).eq("alert_email", 1).execute()

    rows = []
    for r in result.data:
        profile = r.pop("profiles", {})
        r["email"] = profile.get("email", "")
        rows.append(Row(r))
    return rows


def log_alert(user_id: str | None, ticker: str, alert_type: str, message: str, confidence: float = 0):
    """Log an alert that was sent."""
    client = _get_client()
    client.table("crypto_alert_history").insert({
        "user_id": user_id,
        "ticker": ticker,
        "alert_type": alert_type,
        "message": message,
        "confidence": confidence,
    }).execute()


# ─── Kalshi ──────────────────────────────────────────────────────────

def upsert_kalshi_market(ticker: str, title: str, category: str, status: str,
                         yes_price: float, no_price: float, volume: int, data: dict):
    """Insert or update a Kalshi market entry."""
    client = _get_client()
    # Check if market exists by ticker
    existing = client.table("crypto_kalshi_markets").select("id").eq(
        "ticker", ticker
    ).limit(1).execute()

    market_data = {
        "ticker": ticker,
        "title": title,
        "category": category,
        "status": status,
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": volume,
        "data": json.dumps(data),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    if existing.data:
        client.table("crypto_kalshi_markets").update(market_data).eq(
            "id", existing.data[0]["id"]
        ).execute()
    else:
        client.table("crypto_kalshi_markets").insert(market_data).execute()


def get_kalshi_markets(category: str = None, limit: int = 100) -> list:
    """Fetch Kalshi markets, optionally filtered by category."""
    client = _get_client()
    query = client.table("crypto_kalshi_markets").select("*").order(
        "volume", desc=True
    ).limit(limit)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return _rows(result.data)


# ─── User lookup (reads from gateway profiles table) ────────────────

def get_user(user_id: str) -> dict | None:
    """Look up a user profile by UUID. Used for email alert lookups."""
    client = _get_client()
    result = client.table("profiles").select("id, email, username").eq(
        "id", user_id
    ).limit(1).execute()
    if result.data:
        row = result.data[0]
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row.get("username", ""),
            "tier": "admin",  # tier is managed by gateway subscriptions now
        }
    return None


# ── Stubs for removed functions (gateway handles auth now) ──────────────────
# These are kept as no-ops so any residual server.py calls don't crash.

def validate_session(token: str) -> dict | None:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return None


def create_session(user_id: str, ip: str = "", user_agent: str = "", max_age: int = 604800) -> str:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return ""


def delete_session(token: str):
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass


def create_user(email: str, password: str, display_name: str = "", tier: str = "free") -> str | None:
    """User creation is managed by the gateway. This is a no-op stub."""
    return None


def cleanup_sessions():
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass


# Initialize on import
init_db()
