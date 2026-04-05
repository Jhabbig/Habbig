import aiosqlite
import hashlib
import secrets
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "midterm_dashboard.db"

class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._init_tables()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _init_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                display_name TEXT,
                tier TEXT DEFAULT 'free' CHECK(tier IN ('free', 'premium', 'admin')),
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                subscription_status TEXT DEFAULT 'none' CHECK(subscription_status IN ('none', 'active', 'cancelled', 'past_due', 'trialing')),
                subscription_end_date TEXT,
                email_verified INTEGER DEFAULT 0,
                email_verify_token TEXT,
                reset_token TEXT,
                reset_token_expiry TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT,
                login_count INTEGER DEFAULT 0,
                settings TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT
            );

            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                event_id TEXT,
                title TEXT NOT NULL,
                event_title TEXT,
                slug TEXT,
                race_type TEXT,
                state TEXT,
                outcomes TEXT NOT NULL,  -- JSON
                volume REAL DEFAULT 0,
                liquidity REAL DEFAULT 0,
                active INTEGER DEFAULT 1,
                closed INTEGER DEFAULT 0,
                end_date TEXT,
                last_updated TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(source, source_id)
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                prices TEXT NOT NULL,  -- JSON: {outcome_name: probability}
                volume REAL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS polling_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_type TEXT NOT NULL,
                state TEXT,
                candidate TEXT,
                party TEXT,
                percentage REAL,
                pollster TEXT,
                sample_size INTEGER,
                population TEXT,
                start_date TEXT,
                end_date TEXT,
                race_id TEXT,
                source TEXT DEFAULT '538',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS polling_averages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state TEXT NOT NULL,
                race_type TEXT NOT NULL,
                candidate TEXT NOT NULL,
                party TEXT,
                average REAL NOT NULL,
                num_polls INTEGER,
                period_days INTEGER DEFAULT 30,
                computed_at TEXT DEFAULT (datetime('now')),
                UNIQUE(state, race_type, candidate, period_days)
            );

            CREATE TABLE IF NOT EXISTS divergence_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_key TEXT NOT NULL,  -- e.g. "senate_PA"
                state TEXT,
                race_type TEXT,
                polymarket_prob REAL,
                kalshi_prob REAL,
                predictit_prob REAL,
                polling_avg REAL,
                max_divergence REAL,
                divergence_details TEXT,  -- JSON
                snapshot_time TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                race_key TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, race_key)
            );

            CREATE TABLE IF NOT EXISTS alert_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                race_key TEXT,
                alert_type TEXT DEFAULT 'divergence',
                threshold REAL DEFAULT 5.0,
                enabled INTEGER DEFAULT 1,
                UNIQUE(user_id, race_key, alert_type)
            );

            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                race_key TEXT,
                alert_type TEXT,
                message TEXT,
                divergence_value REAL,
                delivered INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_markets_source ON markets(source, source_id);
            CREATE INDEX IF NOT EXISTS idx_markets_race ON markets(race_type, state);
            CREATE INDEX IF NOT EXISTS idx_price_history_market ON price_history(market_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_polling_state ON polling_data(state, poll_type);
            CREATE INDEX IF NOT EXISTS idx_divergence_race ON divergence_snapshots(race_key, snapshot_time);
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);
        """)
        await self._db.commit()

    # === User Management ===

    def _hash_password(self, password: str, salt: str = None) -> tuple[str, str]:
        if salt is None:
            salt = secrets.token_hex(32)
        hash_val = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
        return hash_val.hex(), salt

    async def create_user(self, email: str, password: str, display_name: str = None) -> Optional[int]:
        password_hash, salt = self._hash_password(password)
        verify_token = secrets.token_urlsafe(32)
        try:
            cursor = await self._db.execute(
                """INSERT INTO users (email, password_hash, salt, display_name, email_verify_token)
                   VALUES (?, ?, ?, ?, ?)""",
                (email.lower().strip(), password_hash, salt, display_name, verify_token)
            )
            await self._db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def verify_login(self, email: str, password: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            hash_val, _ = self._hash_password(password, row["salt"])
            if hash_val != row["password_hash"]:
                return None
            # Update login stats
            await self._db.execute(
                "UPDATE users SET last_login = datetime('now'), login_count = login_count + 1 WHERE id = ?",
                (row["id"],)
            )
            await self._db.commit()
            return dict(row)

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        async with self._db.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_user_tier(self, user_id: int, tier: str, stripe_sub_id: str = None):
        sub_status = "active" if tier == "premium" else "none"
        await self._db.execute(
            """UPDATE users SET tier = ?, subscription_status = ?, stripe_subscription_id = ? WHERE id = ?""",
            (tier, sub_status, stripe_sub_id, user_id)
        )
        await self._db.commit()

    async def update_subscription(self, user_id: int, status: str, end_date: str = None):
        tier = "premium" if status == "active" else "free"
        await self._db.execute(
            """UPDATE users SET subscription_status = ?, subscription_end_date = ?, tier = ? WHERE id = ?""",
            (status, end_date, tier, user_id)
        )
        await self._db.commit()

    async def update_user_settings(self, user_id: int, settings: dict):
        current = await self.get_user(user_id)
        current_settings = json.loads(current.get("settings") or "{}")
        current_settings.update(settings)
        await self._db.execute(
            "UPDATE users SET settings = ? WHERE id = ?",
            (json.dumps(current_settings), user_id)
        )
        await self._db.commit()
        return current_settings

    # === Session Management ===

    async def create_session(self, user_id: int, ip: str = None, user_agent: str = None, days: int = 7) -> str:
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        await self._db.execute(
            "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, expires, ip, user_agent)
        )
        await self._db.commit()
        return token

    async def validate_session(self, token: str) -> Optional[dict]:
        async with self._db.execute(
            """SELECT s.*, u.email, u.tier, u.display_name, u.subscription_status
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.token = ? AND s.expires_at > datetime('now')""",
            (token,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_session(self, token: str):
        await self._db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await self._db.commit()

    async def cleanup_expired_sessions(self):
        await self._db.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        await self._db.commit()

    # === Market Data ===

    async def upsert_market(self, market: dict):
        await self._db.execute(
            """INSERT INTO markets (source, source_id, event_id, title, event_title, slug,
                                   race_type, state, outcomes, volume, liquidity, active, closed, end_date, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, source_id) DO UPDATE SET
                   title=excluded.title, outcomes=excluded.outcomes, volume=excluded.volume,
                   liquidity=excluded.liquidity, active=excluded.active, closed=excluded.closed,
                   last_updated=excluded.last_updated""",
            (market["source"], market["source_id"], market.get("event_id"),
             market["title"], market.get("event_title"), market.get("slug"),
             market.get("race_type"), market.get("state"),
             json.dumps(market.get("outcomes", [])),
             market.get("volume", 0), market.get("liquidity", 0),
             1 if market.get("active") else 0, 1 if market.get("closed") else 0,
             market.get("end_date"), market.get("last_updated"))
        )
        await self._db.commit()

    async def upsert_markets_batch(self, markets: list[dict]):
        for market in markets:
            await self.upsert_market(market)

    async def get_markets(self, source: str = None, race_type: str = None,
                          state: str = None, active_only: bool = True,
                          search: str = None, min_volume: float = None) -> list[dict]:
        query = "SELECT * FROM markets WHERE 1=1"
        params = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if race_type:
            query += " AND race_type = ?"
            params.append(race_type)
        if state:
            query += " AND state = ?"
            params.append(state)
        if active_only:
            query += " AND active = 1 AND (closed IS NULL OR closed = 0)"
        if search:
            query += " AND (LOWER(title) LIKE ? OR LOWER(event_title) LIKE ?)"
            like = f"%{search.lower()}%"
            params.extend([like, like])
        if min_volume is not None:
            query += " AND volume >= ?"
            params.append(min_volume)
        query += " ORDER BY volume DESC"

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["outcomes"] = json.loads(d.get("outcomes", "[]"))
                results.append(d)
            return results

    async def get_all_markets(self, active_only: bool = True) -> list[dict]:
        query = "SELECT * FROM markets"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY volume DESC"
        async with self._db.execute(query) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["outcomes"] = json.loads(d.get("outcomes", "[]"))
                results.append(d)
            return results

    # === Price History ===

    async def record_price_snapshot(self, market_id: int, source: str, prices: dict, volume: float = None):
        await self._db.execute(
            "INSERT INTO price_history (market_id, source, timestamp, prices, volume) VALUES (?, ?, ?, ?, ?)",
            (market_id, source, datetime.now(timezone.utc).isoformat(), json.dumps(prices), volume)
        )
        await self._db.commit()

    async def get_price_history(self, market_id: int, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            "SELECT * FROM price_history WHERE market_id = ? AND timestamp >= ? ORDER BY timestamp",
            (market_id, cutoff)
        ) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["prices"] = json.loads(d.get("prices", "{}"))
                results.append(d)
            return results

    # === Divergence ===

    async def record_divergence(self, race_key: str, state: str, race_type: str, data: dict):
        await self._db.execute(
            """INSERT INTO divergence_snapshots
               (race_key, state, race_type, polymarket_prob, kalshi_prob, predictit_prob,
                polling_avg, max_divergence, divergence_details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (race_key, state, race_type,
             data.get("polymarket"), data.get("kalshi"), data.get("predictit"),
             data.get("polling"), data.get("max_divergence"),
             json.dumps(data.get("details", {})))
        )
        await self._db.commit()

    async def get_divergence_history(self, race_key: str = None, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if race_key:
            query = "SELECT * FROM divergence_snapshots WHERE race_key = ? AND snapshot_time >= ? ORDER BY snapshot_time"
            params = (race_key, cutoff)
        else:
            query = "SELECT * FROM divergence_snapshots WHERE snapshot_time >= ? ORDER BY max_divergence DESC"
            params = (cutoff,)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["divergence_details"] = json.loads(d.get("divergence_details", "{}"))
                results.append(d)
            return results

    # === Polling Data ===

    async def store_polls_batch(self, polls: list[dict]):
        for p in polls:
            await self._db.execute(
                """INSERT OR IGNORE INTO polling_data
                   (poll_type, state, candidate, party, percentage, pollster, sample_size,
                    population, start_date, end_date, race_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.get("poll_type"), p.get("state"), p.get("candidate"), p.get("party"),
                 p.get("percentage"), p.get("pollster"), p.get("sample_size"),
                 p.get("population"), p.get("start_date"), p.get("end_date"),
                 p.get("race_id"), p.get("source", "538"))
            )
        await self._db.commit()

    async def get_polls(self, state: str = None, poll_type: str = None) -> list[dict]:
        query = "SELECT * FROM polling_data WHERE 1=1"
        params = []
        if state:
            query += " AND state = ?"
            params.append(state)
        if poll_type:
            query += " AND poll_type = ?"
            params.append(poll_type)
        query += " ORDER BY end_date DESC LIMIT 500"
        async with self._db.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    # === Admin Analytics ===

    async def get_admin_stats(self) -> dict:
        stats = {}

        # User counts by tier
        async with self._db.execute(
            "SELECT tier, COUNT(*) as count FROM users GROUP BY tier"
        ) as cursor:
            stats["users_by_tier"] = {row["tier"]: row["count"] for row in await cursor.fetchall()}

        # Total users
        async with self._db.execute("SELECT COUNT(*) as total FROM users") as cursor:
            stats["total_users"] = (await cursor.fetchone())["total"]

        # Subscription stats
        async with self._db.execute(
            "SELECT subscription_status, COUNT(*) as count FROM users GROUP BY subscription_status"
        ) as cursor:
            stats["subscriptions"] = {row["subscription_status"]: row["count"] for row in await cursor.fetchall()}

        # New users (last 7 days)
        async with self._db.execute(
            "SELECT COUNT(*) as count FROM users WHERE created_at >= datetime('now', '-7 days')"
        ) as cursor:
            stats["new_users_7d"] = (await cursor.fetchone())["count"]

        # New users (last 30 days)
        async with self._db.execute(
            "SELECT COUNT(*) as count FROM users WHERE created_at >= datetime('now', '-30 days')"
        ) as cursor:
            stats["new_users_30d"] = (await cursor.fetchone())["count"]

        # Active sessions
        async with self._db.execute(
            "SELECT COUNT(*) as count FROM sessions WHERE expires_at > datetime('now')"
        ) as cursor:
            stats["active_sessions"] = (await cursor.fetchone())["count"]

        # Daily signups (last 30 days)
        async with self._db.execute(
            """SELECT date(created_at) as day, COUNT(*) as count
               FROM users WHERE created_at >= datetime('now', '-30 days')
               GROUP BY date(created_at) ORDER BY day"""
        ) as cursor:
            stats["daily_signups"] = [dict(r) for r in await cursor.fetchall()]

        # Revenue estimate (premium users * price)
        premium_count = stats["users_by_tier"].get("premium", 0)
        stats["estimated_mrr"] = premium_count * 9.99  # Placeholder price

        # Market data stats
        async with self._db.execute("SELECT COUNT(*) as count FROM markets WHERE active = 1") as cursor:
            stats["active_markets"] = (await cursor.fetchone())["count"]

        async with self._db.execute("SELECT COUNT(*) as count FROM price_history") as cursor:
            stats["price_snapshots"] = (await cursor.fetchone())["count"]

        async with self._db.execute("SELECT COUNT(*) as count FROM divergence_snapshots") as cursor:
            stats["divergence_snapshots"] = (await cursor.fetchone())["count"]

        return stats

    async def get_all_users(self, limit: int = 100, offset: int = 0) -> list[dict]:
        async with self._db.execute(
            """SELECT id, email, display_name, tier, subscription_status, subscription_end_date,
                      email_verified, created_at, last_login, login_count
               FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_user_growth(self, days: int = 90) -> list[dict]:
        async with self._db.execute(
            """SELECT date(created_at) as day,
                      COUNT(*) as new_users,
                      SUM(CASE WHEN tier = 'premium' THEN 1 ELSE 0 END) as new_premium
               FROM users WHERE created_at >= datetime('now', ? || ' days')
               GROUP BY date(created_at) ORDER BY day""",
            (f"-{days}",)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_churn_data(self) -> dict:
        async with self._db.execute(
            """SELECT COUNT(*) as count FROM users
               WHERE subscription_status = 'cancelled'
               AND subscription_end_date >= datetime('now', '-30 days')"""
        ) as cursor:
            recent_churn = (await cursor.fetchone())["count"]

        async with self._db.execute(
            "SELECT COUNT(*) as count FROM users WHERE subscription_status = 'active'"
        ) as cursor:
            active = (await cursor.fetchone())["count"]

        churn_rate = (recent_churn / max(active + recent_churn, 1)) * 100
        return {"recent_churn": recent_churn, "active_subs": active, "churn_rate_pct": round(churn_rate, 2)}

    # === Audit Log ===

    async def log_action(self, user_id: int, action: str, details: str = None, ip: str = None):
        await self._db.execute(
            "INSERT INTO audit_log (user_id, action, details, ip_address) VALUES (?, ?, ?, ?)",
            (user_id, action, details, ip)
        )
        await self._db.commit()

    async def get_audit_log(self, user_id: int = None, limit: int = 100) -> list[dict]:
        if user_id:
            query = "SELECT * FROM audit_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"
            params = (user_id, limit)
        else:
            query = "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with self._db.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]
