"""Standalone tests for the alerts / portfolio / comments / SEO endpoints.

Run with: python3 test_features.py
"""
from __future__ import annotations

import asyncio
import sys


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


from database import Database, DB_PATH
import main as main_mod
import sqlite3 as _sql


_db = Database()
_db.connect()
main_mod.state.db = _db

# Reset all per-user state that previous test runs may have left behind.
with _sql.connect(DB_PATH) as _c:
    for tbl in (
        "midterm_push_subscriptions",
        "midterm_alert_dedup",
        "midterm_alert_history",
        "midterm_alert_settings",
        "midterm_race_comments",
        "midterm_paper_positions",
    ):
        _c.execute(f"DELETE FROM {tbl}")
    _c.commit()


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------

_db.add_push_subscription("u1", "https://example/endpoint1", {"p256dh": "k", "auth": "a"})
subs = _db.get_push_subscriptions("u1")
if len(subs) != 1 or subs[0]["endpoint"] != "https://example/endpoint1":
    fail("push: add+get round-trip", f"subs={subs}")
passed("push: add+get round-trip")

# Idempotent upsert
_db.add_push_subscription("u1", "https://example/endpoint1", {"p256dh": "k2", "auth": "a2"})
subs = _db.get_push_subscriptions("u1")
if len(subs) != 1 or subs[0]["keys"]["p256dh"] != "k2":
    fail("push: upsert overwrites keys", f"subs={subs}")
passed("push: upsert overwrites keys")

# Remove
if not _db.remove_push_subscription("u1", "https://example/endpoint1"):
    fail("push: remove returns True on hit")
passed("push: remove returns True on hit")
if _db.remove_push_subscription("u1", "https://example/endpoint1"):
    fail("push: remove returns False on miss")
passed("push: remove returns False on miss")


# ---------------------------------------------------------------------------
# Alert dedup watermark
# ---------------------------------------------------------------------------

assert _db.get_alert_watermark("u1", "senate_GA", "divergence") is None
passed("alerts: no watermark initially")

_db.record_alert_fired("u1", "senate_GA", "divergence", 0.12)
wm = _db.get_alert_watermark("u1", "senate_GA", "divergence")
if wm is None or abs(wm["last_probability"] - 0.12) > 0.0001:
    fail("alerts: watermark records probability", f"wm={wm}")
passed("alerts: watermark records probability")


# ---------------------------------------------------------------------------
# Alert worker top-prob helper
# ---------------------------------------------------------------------------

helper = main_mod._latest_top_prob_by_race
markets = [
    {"source": "polymarket", "source_id": "a", "race_type": "senate", "state": "TX",
     "outcomes": [{"name": "Yes", "probability": 0.5}]},
    {"source": "kalshi", "source_id": "b", "race_type": "senate", "state": "TX",
     "outcomes": [{"name": "Yes", "probability": 0.6}]},
    # Empty outcomes — must be skipped
    {"source": "predictit", "source_id": "c", "race_type": "senate", "state": "TX",
     "outcomes": []},
    # Unmatched (no race_type) — must not appear
    {"source": "polymarket", "source_id": "d", "race_type": "other", "state": None,
     "outcomes": [{"name": "Yes", "probability": 0.9}]},
]
top = helper(markets)
if "senate_TX" not in top:
    fail("alerts: helper buckets by race_key", str(top))
if top["senate_TX"].get("polymarket") != 0.5 or top["senate_TX"].get("kalshi") != 0.6:
    fail("alerts: per-source top probs", str(top))
if any(k.startswith("unmatched_") for k in top):
    fail("alerts: unmatched markets excluded", str(top))
passed("alerts: top-prob helper buckets only matched races with outcomes")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

cid = _db.add_comment("senate_GA", "u1", "alice@example.com", "premium", "good race")
comments = _db.get_comments("senate_GA")
if not any(c["id"] == cid and c["body"] == "good race" for c in comments):
    fail("comments: add+get round-trip", str(comments))
passed("comments: add+get round-trip")

# Self-delete works
if not _db.delete_comment(cid, user_id="u1"):
    fail("comments: self-delete returns True")
passed("comments: self-delete returns True")

# Foreign user can't delete
cid2 = _db.add_comment("senate_GA", "u1", "alice@example.com", "premium", "x")
if _db.delete_comment(cid2, user_id="u2"):
    fail("comments: foreign delete returns False")
passed("comments: foreign delete returns False (ownership enforced)")

# Admin path (user_id=None) deletes anything
if not _db.delete_comment(cid2, user_id=None):
    fail("comments: admin can delete any")
passed("comments: admin (no user_id filter) can delete any")


# ---------------------------------------------------------------------------
# Paper portfolio
# ---------------------------------------------------------------------------

pid = _db.open_paper_position(
    "u1", "senate_GA", "polymarket", "Ossoff", "yes", 0.55, 100.0,
)
positions = _db.get_paper_positions("u1", open_only=True)
if not any(p["id"] == pid for p in positions):
    fail("portfolio: opened position appears in open_only=True", str(positions))
passed("portfolio: open and list")

# Close idempotency: once closed, second close is no-op
if not _db.close_paper_position(pid, "u1", 0.65):
    fail("portfolio: close returns True on first close")
passed("portfolio: close returns True on first close")
if _db.close_paper_position(pid, "u1", 0.7):
    fail("portfolio: close returns False on already-closed")
passed("portfolio: close returns False on already-closed")

# Wrong user can't close
pid2 = _db.open_paper_position("u1", "senate_GA", "kalshi", "Ossoff", "no", 0.45, 50.0)
if _db.close_paper_position(pid2, "u2", 0.5):
    fail("portfolio: foreign-user close returns False")
passed("portfolio: foreign-user close returns False")


# ---------------------------------------------------------------------------
# Sitemap and SEO surfaces (route registration only — no live HTTP)
# ---------------------------------------------------------------------------

paths = {r.path for r in main_mod.app.routes if hasattr(r, "path")}
required = {
    "/sitemap.xml", "/robots.txt", "/og/race/{race_key}.png",
    "/embed/race/{race_key}",
    "/data/push/public-key", "/premium/push/subscribe", "/premium/push/unsubscribe",
    "/premium/alerts/history",
    "/data/race/{race_key}/comments", "/premium/race/{race_key}/comments",
    "/premium/comments/{comment_id}",
    "/premium/portfolio", "/premium/portfolio/{position_id}",
    "/data/accuracy", "/admin/resolve",
    "/data/race/{race_key}/movements",
}
missing = required - paths
if missing:
    fail("routes: all new endpoints registered", f"missing={missing}")
passed("routes: all new endpoints registered (16 routes)")


# ---------------------------------------------------------------------------
# Sitemap content
# ---------------------------------------------------------------------------

async def _check_sitemap():
    # Insert a market so the sitemap has at least one race entry
    _db.upsert_markets_batch([{
        "source": "polymarket", "source_id": "seo1", "title": "PA Senate",
        "race_type": "senate", "state": "PA",
        "outcomes": [{"name": "Yes", "probability": 0.5}], "volume": 1,
        "active": True, "closed": False,
    }])
    resp = await main_mod.sitemap_xml()
    body = resp.body.decode() if hasattr(resp, "body") else str(resp)
    if "/race/senate_PA" not in body:
        fail("sitemap: includes active race key", body[:200])
    if "<urlset" not in body or "</urlset>" not in body:
        fail("sitemap: well-formed urlset", body[:200])
    passed("sitemap: includes active race key and wraps in urlset")

    # robots.txt mentions the sitemap
    rresp = await main_mod.robots_txt()
    rbody = rresp.body.decode() if hasattr(rresp, "body") else str(rresp)
    if "Sitemap:" not in rbody or "Disallow: /admin/" not in rbody:
        fail("robots.txt: lists sitemap + disallows admin", rbody)
    passed("robots.txt: lists sitemap + disallows admin")
    # Clean up
    import sqlite3 as _sql
    from database import DB_PATH
    with _sql.connect(DB_PATH) as _c:
        _c.execute("DELETE FROM midterm_markets WHERE source_id='seo1'")
        _c.commit()


asyncio.run(_check_sitemap())


# ---------------------------------------------------------------------------
# Notifications config (channels reflect env)
# ---------------------------------------------------------------------------

import notifications
ch = notifications.channels_available()
if "email" not in ch or "push" not in ch:
    fail("notifications: channels dict shape", str(ch))
passed("notifications: channels dict reports email + push booleans")


print("\nAll feature tests passed.")
