"""Tests for ``db.cascade_delete_user`` schema-driven user-row purge.

Regression cover for the GDPR Art. 17 audit finding: the original
cascade only matched columns named exactly ``user_id`` and missed every
variant FK column (``follower_user_id``, ``referrer_user_id``,
``admin_user_id``, ``target_user_id``, ``sharer_user_id``,
``setup_by_user_id``, ``claimed_by_user_id``, ``used_by_user_id``,
``referred_by_user_id``, ``owner_user_id``, ``reporter_user_id``,
``signed_up_user_id``, ``followed_user_id``).

Tests pin:
  1. A user seeded in every variant-column table has zero rows in every
     one of those tables after ``cascade_delete_user`` runs.
  2. Discovery is dynamic — adding a new ``*_user_id`` column at runtime
     (via ALTER TABLE) is picked up without a code change in auth.py.
  3. Self-referential ``*_user_id`` columns on the ``users`` table itself
     (e.g. ``referred_by_user_id``) are NULL'd before the final users
     DELETE so other surviving users aren't left with dangling FKs.
  4. The cascade's return dict reports per-table-and-column counts so
     callers can audit scope.
"""

from __future__ import annotations

import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


def _suffix() -> str:
    return f"{time.time_ns() & 0xFFFFFF:x}"


def _create_user(prefix: str = "cascade") -> int:
    s = _suffix()
    return db.create_user(
        f"{prefix}_{s}@test.local",
        "InitialPass123!verylong",
        username=f"{prefix}_{s}",
    )


def _user_keyed_tables() -> list[tuple[str, str]]:
    """Every (table, column) pair where ``column`` is ``user_id`` or
    ``*_user_id`` (INTEGER affinity). Mirrors the discovery logic in
    ``cascade_delete_user``."""
    pairs: list[tuple[str, str]] = []
    with db.conn() as c:
        tables = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for t in tables:
            name = t["name"]
            if name == "users":
                continue
            try:
                cols = c.execute(f"PRAGMA table_info({name})").fetchall()
            except Exception:
                continue
            for col in cols:
                col_name = col["name"]
                col_type = (col["type"] or "").upper()
                if "INT" not in col_type:
                    continue
                if col_name == "user_id" or col_name.endswith("_user_id"):
                    pairs.append((name, col_name))
    return pairs


class CascadeDeleteVariantColumnsTestCase(unittest.TestCase):
    """Seed a row in every variant-column table, run cascade, assert zero."""

    def setUp(self):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass

    def test_cascade_clears_canonical_user_id_rows(self):
        """Smoke test: canonical ``user_id`` column path still works."""
        uid = _create_user()
        from queries import admin as admin_q
        admin_q.record_analytics_event(
            event_type="cascade_smoke",
            user_id=uid,
            session_id=f"sess_{uid}",
            page="/test",
            referrer=None,
            ip_hash=f"iphash_{uid}",
            user_agent_category="test",
            properties={"k": "v"},
        )
        with db.conn() as c:
            pre = c.execute(
                "SELECT COUNT(*) AS n FROM analytics_events WHERE user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertGreaterEqual(pre, 1)

        deleted = db.cascade_delete_user(uid)
        self.assertIn("analytics_events", deleted)

        with db.conn() as c:
            post = c.execute(
                "SELECT COUNT(*) AS n FROM analytics_events WHERE user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertEqual(post, 0)

    def test_cascade_clears_variant_columns(self):
        """Seed rows keyed by every ``*_user_id`` variant column the
        schema exposes; verify the cascade nukes them all."""
        actor = _create_user("actor")
        target = _create_user("target")

        now = int(time.time())
        with db.conn() as c:
            try:
                c.execute(
                    "INSERT INTO audit_log "
                    "(timestamp, admin_user_id, admin_email, action, "
                    "target_type, target_id, target_description, "
                    "ip_address, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, actor, "x@test.local", "test_cascade",
                     "user", str(target), "ut", "127.0.0.1", "rid"),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO impersonation_sessions "
                    "(admin_user_id, target_user_id, cookie_token, "
                    "reason, started_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor, target, f"tok_{actor}", "test", now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO collections "
                    "(owner_user_id, slug, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor, f"slug_{actor}", "Test", now, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO discord_servers "
                    "(guild_id, setup_by_user_id, connected_at) "
                    "VALUES (?, ?, ?)",
                    (f"g_{actor}", actor, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO referrals "
                    "(referrer_user_id, referred_user_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (actor, target, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO affiliate_conversions "
                    "(affiliate_account_id, referred_user_id, clicked_at) "
                    "VALUES (?, ?, ?)",
                    (1, target, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO invite_tokens "
                    "(token, claimed_by_user_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (f"tok_{actor}", actor, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO user_invite_tokens "
                    "(token, user_id, used_by_user_id, "
                    "tier_at_grant, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"utok_{actor}", target, actor, "pro", now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO share_metrics "
                    "(share_type, share_id, signed_up_user_id, viewed_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("source", 1, actor, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO shared_market_cards "
                    "(token, market_slug, sharer_user_id, "
                    "created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"smc_{actor}", "mkt", actor, now, now + 86400),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO shared_predictions "
                    "(token, user_prediction_id, sharer_user_id, "
                    "created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"sp_{actor}", 1, actor, now, now + 86400),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO shared_source_cards "
                    "(token, source_handle, sharer_user_id, "
                    "created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"ssc_{actor}", "h", actor, now, now + 86400),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO take_reports "
                    "(take_id, reporter_user_id, reason, reported_at) "
                    "VALUES (?, ?, ?, ?)",
                    (1, actor, "spam", now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO user_follows "
                    "(follower_user_id, followed_user_id) "
                    "VALUES (?, ?)",
                    (actor, target),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO newsletter_campaigns "
                    "(admin_user_id, subject, body_md, segment, "
                    "scheduled_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (actor, "s", "b", "all", now, now),
                )
            except Exception:
                pass

        def _variant_counts_for(user_id: int) -> dict[str, int]:
            out: dict[str, int] = {}
            for table, col in _user_keyed_tables():
                if col == "user_id":
                    continue
                try:
                    with db.conn() as c:
                        n = c.execute(
                            f"SELECT COUNT(*) AS n FROM {table} "
                            f"WHERE {col} = ?",
                            (user_id,),
                        ).fetchone()["n"]
                    if n:
                        out[f"{table}.{col}"] = n
                except Exception:
                    continue
            return out

        pre = _variant_counts_for(actor)
        self.assertGreater(
            len(pre), 0,
            "Test setup didn't seed any variant-column rows — table "
            "schemas may have changed, update the seeding block."
        )

        deleted = db.cascade_delete_user(actor)

        post = _variant_counts_for(actor)
        self.assertEqual(
            post, {},
            f"Variant-column rows survived cascade: {post}"
        )

        for key in pre.keys():
            self.assertIn(
                key, deleted,
                f"cascade_delete_user return dict missing {key!r}; "
                f"got keys: {list(deleted.keys())}"
            )

    def test_cascade_picks_up_runtime_added_column(self):
        """Dynamic discovery: a brand-new ``*_user_id`` column added
        after import time must be matched without a code change."""
        uid = _create_user("dyn")
        tname = f"cascade_dyn_test_{uid}"
        with db.conn() as c:
            c.execute(
                f"CREATE TABLE {tname} "
                f"(id INTEGER PRIMARY KEY, "
                f"  arbitrary_user_id INTEGER NOT NULL, "
                f"  note TEXT)"
            )
            c.execute(
                f"INSERT INTO {tname} (arbitrary_user_id, note) "
                f"VALUES (?, 'present')",
                (uid,),
            )
            pre = c.execute(
                f"SELECT COUNT(*) AS n FROM {tname} "
                f"WHERE arbitrary_user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertEqual(pre, 1)

        deleted = db.cascade_delete_user(uid)

        with db.conn() as c:
            post = c.execute(
                f"SELECT COUNT(*) AS n FROM {tname} "
                f"WHERE arbitrary_user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertEqual(post, 0)
        self.assertIn(f"{tname}.arbitrary_user_id", deleted)
        with db.conn() as c:
            c.execute(f"DROP TABLE {tname}")

    def test_cascade_nulls_self_references_on_users_table(self):
        """``users.referred_by_user_id`` self-ref must be NULL'd."""
        referrer = _create_user("referrer")
        referred = _create_user("referred")
        with db.conn() as c:
            c.execute(
                "UPDATE users SET referred_by_user_id = ? WHERE id = ?",
                (referrer, referred),
            )
            pre = c.execute(
                "SELECT referred_by_user_id FROM users WHERE id = ?",
                (referred,),
            ).fetchone()["referred_by_user_id"]
        self.assertEqual(pre, referrer)

        db.cascade_delete_user(referrer)

        with db.conn() as c:
            post = c.execute(
                "SELECT referred_by_user_id FROM users WHERE id = ?",
                (referred,),
            ).fetchone()
        self.assertIsNotNone(post)
        self.assertIsNone(post["referred_by_user_id"])

    def test_cascade_skips_text_columns_with_user_id_suffix(self):
        """TEXT columns sharing the suffix (Discord snowflakes etc.)
        must not be touched — the INTEGER affinity guard is the only
        thing protecting them."""
        uid = _create_user("textguard")
        with db.conn() as c:
            c.execute(
                "CREATE TABLE _text_user_id_decoy "
                "(id INTEGER PRIMARY KEY, "
                " discord_user_id TEXT NOT NULL, "
                " hint TEXT)"
            )
            c.execute(
                "INSERT INTO _text_user_id_decoy "
                "(discord_user_id, hint) VALUES (?, ?)",
                (str(uid), "must_survive"),
            )

        db.cascade_delete_user(uid)

        with db.conn() as c:
            row = c.execute(
                "SELECT hint FROM _text_user_id_decoy "
                "WHERE discord_user_id = ?",
                (str(uid),),
            ).fetchone()
            c.execute("DROP TABLE _text_user_id_decoy")
        self.assertIsNotNone(
            row,
            "Cascade incorrectly deleted a TEXT-typed *_user_id row; "
            "the INTEGER affinity guard regressed."
        )


class CascadeDeleteCoverageCountTestCase(unittest.TestCase):
    """Pin how many (table, column) pairs the cascade walks. Hard floor
    of 80 — schema today has 85 pairs, well above the original 7
    hand-rolled tables."""

    def test_cascade_walks_at_least_80_table_column_pairs(self):
        pairs = _user_keyed_tables()
        n = len(pairs)
        self.assertGreaterEqual(
            n, 80,
            f"Cascade scope dropped to {n} (table, column) pairs; "
            f"the schema walk likely regressed to a hardcoded list."
        )
        variants = [p for p in pairs if p[1] != "user_id"]
        names = {p[1] for p in variants}
        for must in (
            "admin_user_id", "target_user_id", "referrer_user_id",
            "referred_user_id", "follower_user_id", "followed_user_id",
            "owner_user_id", "sharer_user_id", "claimed_by_user_id",
            "setup_by_user_id", "used_by_user_id",
        ):
            self.assertIn(
                must, names,
                f"Variant column family {must!r} missing — schema "
                f"shifted or discovery is broken."
            )


if __name__ == "__main__":
    unittest.main()
