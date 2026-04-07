#!/usr/bin/env python3
"""
Migrate existing SQLite data to Supabase.

Usage:
    1. Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables
    2. Run the Supabase schema SQL first (supabase_schema.sql)
    3. python migrate_sqlite_to_supabase.py

This script migrates data from all SQLite databases:
    - gateway/auth.db          -> profiles, sessions, subscriptions, invite_tokens, enquiries, password_resets
    - crypto-dashboard/*.db    -> crypto_* tables
    - sports-dashboard/*.db    -> sports_* tables
    - midterm-dashboard/*.db   -> midterm_* tables
    - weather-dashboard/*.db   -> weather_* tables
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

from supabase import create_client, Client

BASE_DIR = Path(__file__).parent.parent  # Project root

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables")
    sys.exit(1)

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_sqlite_rows(db_path: Path, query: str) -> list[dict]:
    """Read all rows from a SQLite query as list of dicts."""
    if not db_path.exists():
        print(f"  SKIP: {db_path} does not exist")
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


def batch_insert(table: str, rows: list[dict], batch_size: int = 500) -> int:
    """Insert rows into Supabase in batches. Returns count inserted."""
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            sb.table(table).upsert(batch).execute()
            total += len(batch)
        except Exception as e:
            print(f"  ERROR inserting batch into {table}: {e}")
            # Try one-by-one for this batch
            for row in batch:
                try:
                    sb.table(table).upsert(row).execute()
                    total += 1
                except Exception as e2:
                    print(f"  SKIP row in {table}: {e2}")
    return total


# ── Gateway auth.db ─────────────────────────────────────────────────────────

def migrate_gateway():
    db_path = BASE_DIR / "gateway" / "auth.db"
    if not db_path.exists():
        print("Gateway auth.db not found, skipping")
        return

    print("\n=== Migrating Gateway (auth.db) ===")

    # 1. Users -> Supabase Auth + profiles
    users = get_sqlite_rows(db_path, "SELECT * FROM users")
    print(f"  Found {len(users)} users")
    user_id_map = {}  # old int id -> new UUID

    for u in users:
        try:
            # Create user in Supabase Auth
            auth_resp = sb.auth.admin.create_user({
                "email": u["email"],
                "password": "MIGRATE_" + os.urandom(16).hex(),  # Temp password, users must reset
                "email_confirm": True,
                "user_metadata": {"username": u.get("username", u["email"].split("@")[0])},
            })
            new_id = auth_resp.user.id
            user_id_map[u["id"]] = new_id

            # Update profile with additional fields
            sb.table("profiles").update({
                "is_admin": u.get("is_admin", 0),
                "suspended": u.get("suspended", 0),
                "default_dashboard": u.get("default_dashboard"),
                "invite_token_id": u.get("invite_token_id"),
            }).eq("id", new_id).execute()

            print(f"  Migrated user: {u['email']} -> {new_id}")
        except Exception as e:
            print(f"  ERROR migrating user {u['email']}: {e}")

    # 2. Invite tokens
    tokens = get_sqlite_rows(db_path, "SELECT * FROM invite_tokens")
    print(f"  Found {len(tokens)} invite tokens")
    for t in tokens:
        row = {
            "token": t["token"],
            "status": t["status"],
            "claimed_by_user_id": user_id_map.get(t.get("claimed_by_user_id")),
            "claimed_by_email": t.get("claimed_by_email"),
            "note": t.get("note", ""),
            "target_email": t.get("target_email"),
            "created_at": t["created_at"],
            "claimed_at": t.get("claimed_at"),
        }
        try:
            sb.table("invite_tokens").insert(row).execute()
        except Exception as e:
            print(f"  ERROR migrating token {t['token'][:8]}...: {e}")
    print(f"  Migrated {len(tokens)} invite tokens")

    # 3. Subscriptions
    subs = get_sqlite_rows(db_path, "SELECT * FROM subscriptions")
    print(f"  Found {len(subs)} subscriptions")
    for s in subs:
        new_uid = user_id_map.get(s["user_id"])
        if not new_uid:
            continue
        row = {
            "user_id": new_uid,
            "dashboard_key": s["dashboard_key"],
            "plan": s["plan"],
            "status": s["status"],
            "started_at": s["started_at"],
            "expires_at": s.get("expires_at"),
            "stripe_sub_id": s.get("stripe_sub_id"),
            "source": s.get("source", "placeholder"),
        }
        try:
            sb.table("subscriptions").upsert(row, on_conflict="user_id,dashboard_key").execute()
        except Exception as e:
            print(f"  ERROR migrating subscription: {e}")
    print(f"  Migrated subscriptions")

    # 4. Enquiries
    enquiries = get_sqlite_rows(db_path, "SELECT * FROM enquiries")
    n = batch_insert("enquiries", enquiries)
    print(f"  Migrated {n}/{len(enquiries)} enquiries")

    return user_id_map


# ── Crypto Dashboard ────────────────────────────────────────────────────────

def migrate_crypto(user_id_map: dict):
    db_path = BASE_DIR / "crypto-dashboard" / "cryptoedge.db"
    if not db_path.exists():
        print("\nCrypto DB not found, skipping")
        return

    print("\n=== Migrating Crypto Dashboard ===")

    # Predictions (no user FK)
    rows = get_sqlite_rows(db_path, "SELECT * FROM predictions")
    mapped = [{
        "ticker": r["ticker"], "window_start": r["window_start"],
        "pred_direction": r["pred_direction"], "pred_delta": r["pred_delta"],
        "pred_prob": r["pred_prob"], "confidence": r["confidence"],
        "ensemble_agreement": r.get("ensemble_agreement"),
        "model_details": r.get("model_details"),
        "actual_direction": r.get("actual_direction"),
        "actual_delta": r.get("actual_delta"),
        "was_correct": r.get("was_correct"),
    } for r in rows]
    n = batch_insert("crypto_predictions", mapped)
    print(f"  Migrated {n}/{len(rows)} predictions")

    # Watchlists
    rows = get_sqlite_rows(db_path, "SELECT * FROM watchlists")
    mapped = [{
        "user_id": user_id_map.get(r["user_id"]),
        "name": r.get("name", "Default"),
        "tickers": r.get("tickers", "[]"),
    } for r in rows if user_id_map.get(r["user_id"])]
    n = batch_insert("crypto_watchlists", mapped)
    print(f"  Migrated {n}/{len(rows)} watchlists")

    # Alert preferences
    rows = get_sqlite_rows(db_path, "SELECT * FROM alert_preferences")
    mapped = [{
        "user_id": user_id_map.get(r["user_id"]),
        "ticker": r["ticker"],
        "min_confidence": r.get("min_confidence", 0.6),
        "alert_email": r.get("alert_email", 1),
        "alert_browser": r.get("alert_browser", 1),
    } for r in rows if user_id_map.get(r["user_id"])]
    n = batch_insert("crypto_alert_preferences", mapped)
    print(f"  Migrated {n}/{len(rows)} alert preferences")

    # Accuracy daily
    rows = get_sqlite_rows(db_path, "SELECT * FROM accuracy_daily")
    mapped = [{
        "ticker": r["ticker"], "date": r["date"],
        "total_predictions": r.get("total_predictions", 0),
        "correct_predictions": r.get("correct_predictions", 0),
        "high_conf_total": r.get("high_conf_total", 0),
        "high_conf_correct": r.get("high_conf_correct", 0),
        "avg_confidence": r.get("avg_confidence", 0),
        "avg_mae": r.get("avg_mae", 0),
    } for r in rows]
    n = batch_insert("crypto_accuracy_daily", mapped)
    print(f"  Migrated {n}/{len(rows)} accuracy records")

    # Kalshi markets
    rows = get_sqlite_rows(db_path, "SELECT * FROM kalshi_markets")
    mapped = [{
        "ticker": r["ticker"], "title": r["title"],
        "category": r.get("category"), "status": r.get("status"),
        "yes_price": r.get("yes_price"), "no_price": r.get("no_price"),
        "volume": r.get("volume", 0), "data": r.get("data"),
    } for r in rows]
    n = batch_insert("crypto_kalshi_markets", mapped)
    print(f"  Migrated {n}/{len(rows)} Kalshi markets")


# ── Sports Dashboard ────────────────────────────────────────────────────────

def migrate_sports(user_id_map: dict):
    # Find the sports DB
    db_path = None
    for name in ["sharpe.db", "sports.db", "sports_dashboard.db"]:
        p = BASE_DIR / "sports-dashboard" / name
        if p.exists():
            db_path = p
            break
    if not db_path:
        print("\nSports DB not found, skipping")
        return

    print(f"\n=== Migrating Sports Dashboard ({db_path.name}) ===")

    # Edge history (no user FK)
    rows = get_sqlite_rows(db_path, "SELECT * FROM edge_history")
    mapped = [{
        "sport": r.get("sport"), "home_team": r.get("home_team"),
        "away_team": r.get("away_team"), "outcome": r.get("outcome"),
        "sharp_prob": r.get("sharp_prob"), "poly_prob": r.get("poly_prob"),
        "divergence": r.get("divergence"), "kelly_pct": r.get("kelly_pct"),
        "confidence_score": r.get("confidence_score"),
        "resolved": r.get("resolved", 0), "resolution": r.get("resolution"),
    } for r in rows]
    n = batch_insert("sports_edge_history", mapped)
    print(f"  Migrated {n}/{len(rows)} edge history records")

    # Trades
    rows = get_sqlite_rows(db_path, "SELECT * FROM trades")
    mapped = [{
        "user_id": user_id_map.get(r["user_id"]),
        "market_name": r.get("market_name"), "outcome": r.get("outcome"),
        "entry_price": r.get("entry_price"), "amount": r.get("amount"),
        "status": r.get("status", "open"),
        "exit_price": r.get("exit_price"), "pnl": r.get("pnl"),
    } for r in rows if user_id_map.get(r.get("user_id"))]
    n = batch_insert("sports_trades", mapped)
    print(f"  Migrated {n}/{len(rows)} trades")

    # Market snapshots
    rows = get_sqlite_rows(db_path, "SELECT * FROM market_snapshots")
    mapped = [{
        "sport": r["sport"], "event_name": r["event_name"],
        "outcome": r["outcome"], "book_prob": r.get("book_prob"),
        "poly_prob": r.get("poly_prob"), "kalshi_prob": r.get("kalshi_prob"),
        "divergence": r.get("divergence"),
        "poly_volume": r.get("poly_volume"), "kalshi_volume": r.get("kalshi_volume"),
    } for r in rows]
    n = batch_insert("sports_market_snapshots", mapped)
    print(f"  Migrated {n}/{len(rows)} market snapshots")

    # Historical markets
    rows = get_sqlite_rows(db_path, "SELECT * FROM historical_markets")
    mapped = [{
        "sport": r.get("sport"), "event_title": r["event_title"],
        "market_question": r.get("market_question"), "outcome": r.get("outcome"),
        "final_price": r.get("final_price"), "volume": r.get("volume"),
        "start_date": r.get("start_date"), "end_date": r.get("end_date"),
        "resolution": r.get("resolution"),
        "source": r.get("source", "polymarket"), "slug": r.get("slug"),
    } for r in rows]
    n = batch_insert("sports_historical_markets", mapped)
    print(f"  Migrated {n}/{len(rows)} historical markets")


# ── Midterm Dashboard ───────────────────────────────────────────────────────

def migrate_midterm(user_id_map: dict):
    db_path = BASE_DIR / "midterm-dashboard" / "backend" / "midterm_dashboard.db"
    if not db_path.exists():
        db_path = BASE_DIR / "midterm-dashboard" / "midterm_dashboard.db"
    if not db_path.exists():
        print("\nMidterm DB not found, skipping")
        return

    print(f"\n=== Migrating Midterm Dashboard ===")

    # Markets
    rows = get_sqlite_rows(db_path, "SELECT * FROM markets")
    mapped = [{
        "source": r["source"], "source_id": r["source_id"],
        "event_id": r.get("event_id"), "title": r["title"],
        "event_title": r.get("event_title"), "slug": r.get("slug"),
        "race_type": r.get("race_type"), "state": r.get("state"),
        "outcomes": r["outcomes"],
        "volume": r.get("volume", 0), "liquidity": r.get("liquidity", 0),
        "active": r.get("active", 1), "closed": r.get("closed", 0),
        "end_date": r.get("end_date"),
    } for r in rows]
    n = batch_insert("midterm_markets", mapped)
    print(f"  Migrated {n}/{len(rows)} markets")

    # Polling data
    rows = get_sqlite_rows(db_path, "SELECT * FROM polling_data")
    mapped = [{
        "poll_type": r["poll_type"], "state": r.get("state"),
        "candidate": r.get("candidate"), "party": r.get("party"),
        "percentage": r.get("percentage"), "pollster": r.get("pollster"),
        "sample_size": r.get("sample_size"), "population": r.get("population"),
        "start_date": r.get("start_date"), "end_date": r.get("end_date"),
        "race_id": r.get("race_id"), "source": r.get("source", "538"),
    } for r in rows]
    n = batch_insert("midterm_polling_data", mapped)
    print(f"  Migrated {n}/{len(rows)} polling records")

    # Divergence snapshots
    rows = get_sqlite_rows(db_path, "SELECT * FROM divergence_snapshots")
    mapped = [{
        "race_key": r["race_key"], "state": r.get("state"),
        "race_type": r.get("race_type"),
        "polymarket_prob": r.get("polymarket_prob"),
        "kalshi_prob": r.get("kalshi_prob"),
        "predictit_prob": r.get("predictit_prob"),
        "polling_avg": r.get("polling_avg"),
        "max_divergence": r.get("max_divergence"),
        "divergence_details": r.get("divergence_details"),
    } for r in rows]
    n = batch_insert("midterm_divergence_snapshots", mapped)
    print(f"  Migrated {n}/{len(rows)} divergence snapshots")


# ── Weather Dashboard ───────────────────────────────────────────────────────

def migrate_weather(user_id_map: dict):
    db_path = BASE_DIR / "polymarket_weather_dashboard" / "history.db"
    if not db_path.exists():
        print("\nWeather DB not found, skipping")
        return

    print(f"\n=== Migrating Weather Dashboard ===")

    # Signals log
    rows = get_sqlite_rows(db_path, "SELECT * FROM signals_log")
    mapped = [{
        "market_id": r["market_id"], "question": r.get("question"),
        "category": r.get("category"), "yes_price": r.get("yes_price"),
        "model_prob": r.get("model_prob"), "edge": r.get("edge"),
        "action": r.get("action"),
    } for r in rows]
    n = batch_insert("weather_signals_log", mapped)
    print(f"  Migrated {n}/{len(rows)} signals")

    # Resolutions
    rows = get_sqlite_rows(db_path, "SELECT * FROM resolutions")
    mapped = [{
        "market_id": r["market_id"],
        "actual_outcome": r.get("actual_outcome"),
        "payout": r.get("payout"),
    } for r in rows]
    n = batch_insert("weather_resolutions", mapped)
    print(f"  Migrated {n}/{len(rows)} resolutions")

    # Price snapshots
    rows = get_sqlite_rows(db_path, "SELECT * FROM price_snapshots")
    mapped = [{
        "market_id": r["market_id"],
        "source": r.get("source", "polymarket"),
        "question": r.get("question"), "city": r.get("city"),
        "target_date": r.get("target_date"),
        "yes_price": r.get("yes_price"),
        "model_prob": r.get("model_prob"), "edge": r.get("edge"),
        "volume": r.get("volume"),
    } for r in rows]
    n = batch_insert("weather_price_snapshots", mapped)
    print(f"  Migrated {n}/{len(rows)} price snapshots")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("SQLite -> Supabase Migration")
    print("=" * 60)
    print(f"Supabase URL: {SUPABASE_URL}")
    print(f"Project root: {BASE_DIR}")

    # Verify Supabase connection
    try:
        sb.table("profiles").select("id").limit(0).execute()
        print("Supabase connection OK\n")
    except Exception as e:
        print(f"ERROR: Cannot connect to Supabase: {e}")
        sys.exit(1)

    start = time.time()

    # Migrate gateway first (creates user_id_map)
    user_id_map = migrate_gateway() or {}
    print(f"\n  User ID mapping: {len(user_id_map)} users")

    # Migrate dashboards
    migrate_crypto(user_id_map)
    migrate_sports(user_id_map)
    migrate_midterm(user_id_map)
    migrate_weather(user_id_map)

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"Migration complete in {elapsed:.1f}s")
    print(f"{'=' * 60}")
    print("\nIMPORTANT: All migrated users have temporary passwords.")
    print("They must use 'Forgot Password' to set a new one.")
    print("Or use the Supabase dashboard to manually set passwords.")


if __name__ == "__main__":
    main()
