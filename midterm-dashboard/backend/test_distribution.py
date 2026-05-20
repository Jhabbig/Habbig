"""Standalone tests for distribution + API-tier surface.

Run with: python3 test_distribution.py

Covers:
  - API key generation (uniqueness, prefix shape, hash determinism)
  - API key lookup (revoked keys don't authenticate)
  - Webhook formatters (slack, discord, generic) — JSON shape checks
  - Webhook threshold dedup (should_fire logic)
  - Outbound webhook delivery success + failure paths (mocked aiohttp)
  - RSS feed structure (valid XML, includes recent moves, escapes HTML)
  - Daily digest subscriber CRUD
  - V1 API key auth: missing header, bad token, revoked key, free vs premium
"""
from __future__ import annotations

import asyncio
import sys
import sqlite3 as _sql


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


from database import Database, DB_PATH
import main as main_mod
import api_keys
import webhooks

_db = Database()
_db.connect()
main_mod.state.db = _db

# Clear per-test state
with _sql.connect(DB_PATH) as _c:
    for tbl in ("midterm_api_keys", "midterm_outbound_webhooks",
                "midterm_webhook_dedup", "midterm_digest_subscriptions"):
        _c.execute(f"DELETE FROM {tbl}")
    _c.commit()


# ---------------------------------------------------------------------------
# 1. API key generation
# ---------------------------------------------------------------------------

p1, prefix1, h1 = api_keys.generate()
p2, prefix2, h2 = api_keys.generate()

if not p1.startswith("mte_live_") or not p2.startswith("mte_live_"):
    fail("api_keys.generate: prefix is mte_live_", p1)
if p1 == p2 or h1 == h2:
    fail("api_keys.generate: keys are unique", f"{p1} vs {p2}")
if api_keys.hash_key(p1) != h1:
    fail("api_keys.hash_key: deterministic")
if len(prefix1) != len("mte_live_") + 8:
    fail("api_keys.generate: key_prefix length is brand + 8 chars", prefix1)
passed("api_keys: generate produces unique, prefixed, hashable keys")

if api_keys.rate_limit_for("free") != 60:
    fail("api_keys.rate_limit_for: free=60")
if api_keys.rate_limit_for("premium") != 600:
    fail("api_keys.rate_limit_for: premium=600")
if api_keys.rate_limit_for("unknown") != 60:
    fail("api_keys.rate_limit_for: unknown tier defaults to 60")
passed("api_keys: rate-limit table")


# ---------------------------------------------------------------------------
# 2. DB key store + lookup + revoke
# ---------------------------------------------------------------------------

kid = _db.store_api_key("u1", prefix1, h1, name="test", tier="premium", rate_limit_rpm=600)
found = _db.lookup_api_key(h1)
if not found or found["id"] != kid or found["tier"] != "premium":
    fail("db.lookup_api_key: round-trip", str(found))
if _db.lookup_api_key("nonexistent_hash") is not None:
    fail("db.lookup_api_key: unknown hash returns None")
passed("db: store + lookup API key round-trip")

if not _db.revoke_api_key(kid, "u1"):
    fail("db.revoke_api_key: returns True on hit")
if _db.lookup_api_key(h1) is not None:
    fail("db.lookup_api_key: revoked key not returned")
if _db.revoke_api_key(kid, "u1"):
    fail("db.revoke_api_key: idempotent — second revoke returns False")
passed("db: revoke makes the key unfindable")

# Foreign user can't revoke
kid2 = _db.store_api_key("u1", "mte_live_xxxxxxxx", "hash_xxx", tier="free")
if _db.revoke_api_key(kid2, "u2"):
    fail("db.revoke_api_key: foreign user gets False")
passed("db: revoke is owner-scoped")


# ---------------------------------------------------------------------------
# 3. Webhook formatters
# ---------------------------------------------------------------------------

args = dict(race_key="senate_GA", race_title="GA Senate 2026", source="polymarket",
            delta_pp=7.3, from_prob=0.55, to_prob=0.628)

g = webhooks.format_generic(**args)
if g["type"] != "midtermedge.movement":
    fail("format_generic: type")
if g["race_key"] != "senate_GA" or g["source"] != "polymarket":
    fail("format_generic: identifies race + source", str(g))
if g["delta_pp"] != 7.3 or g["direction"] != "up":
    fail("format_generic: delta + direction", str(g))
if not g["url"].endswith("/race/senate_GA"):
    fail("format_generic: includes deep link", g["url"])
passed("format_generic: canonical JSON shape")

s = webhooks.format_slack(**args)
if "attachments" not in s or "blocks" not in s["attachments"][0]:
    fail("format_slack: Slack block kit", str(s))
if "GA Senate 2026" not in s["text"] or "polymarket" not in s["text"]:
    fail("format_slack: top-level text", str(s))
passed("format_slack: produces valid Slack block payload")

d = webhooks.format_discord(**args)
if "embeds" not in d or len(d["embeds"]) != 1:
    fail("format_discord: embed array", str(d))
if "polymarket" not in d["embeds"][0]["description"]:
    fail("format_discord: embed description", str(d))
if d["embeds"][0]["color"] != 0x10B981:
    fail("format_discord: up move = green", str(d))
# Down move = red
d_down = webhooks.format_discord(**{**args, "delta_pp": -7.3})
if d_down["embeds"][0]["color"] != 0xEF4444:
    fail("format_discord: down move = red", str(d_down))
passed("format_discord: embed with correct color per direction")


# ---------------------------------------------------------------------------
# 4. should_fire dedup logic
# ---------------------------------------------------------------------------

# No prior fire + over threshold → fire
if not webhooks.should_fire(wm=None, delta_pp=6.0, threshold_pp=5.0):
    fail("should_fire: no prior, over threshold → True")
# No prior + under threshold → no fire
if webhooks.should_fire(wm=None, delta_pp=3.0, threshold_pp=5.0):
    fail("should_fire: under threshold → False")
# Prior fire at +6, current +6.5 — diff 0.5 < threshold → no fire
if webhooks.should_fire(wm={"last_delta_pp": 6.0}, delta_pp=6.5, threshold_pp=5.0):
    fail("should_fire: small drift doesn't re-fire")
# Prior +6, current +12 — diff 6 ≥ threshold → fire
if not webhooks.should_fire(wm={"last_delta_pp": 6.0}, delta_pp=12.0, threshold_pp=5.0):
    fail("should_fire: another threshold of growth re-fires")
# Prior +6, current -1 — diff 7 ≥ threshold → fire (reversal)
if not webhooks.should_fire(wm={"last_delta_pp": 6.0}, delta_pp=-1.0, threshold_pp=5.0):
    fail("should_fire: direction reversal re-fires when delta diff crosses threshold")
passed("should_fire: dedup respects threshold-crossing semantics")


# ---------------------------------------------------------------------------
# 5. Outbound webhook delivery (mocked)
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False
    async def text(self): return self._body


class FakeSession:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body
        self.last_url = None
        self.last_payload = None
    def post(self, url, json=None, timeout=None, headers=None):
        self.last_url = url
        self.last_payload = json
        return FakeResp(self.status, self._body)


async def _delivery_tests():
    sess = FakeSession(200, "ok")
    ok, info = await webhooks.deliver(sess, "https://example.com/hook", {"hello": 1})
    if not ok or info != "200":
        fail("deliver: 200 OK", info)
    if sess.last_payload != {"hello": 1}:
        fail("deliver: payload forwarded verbatim")
    passed("deliver: 200 → ok, payload forwarded")

    sess = FakeSession(500, "server boom")
    ok, info = await webhooks.deliver(sess, "https://example.com/hook", {})
    if ok or "500" not in info:
        fail("deliver: 5xx → not ok", info)
    passed("deliver: 5xx returns not-ok with status in info")

    # ClientError path
    class RaisingSession:
        def post(self, *a, **kw):
            import aiohttp
            class R:
                async def __aenter__(s): raise aiohttp.ClientConnectionError("nope")
                async def __aexit__(s, *e): return False
            return R()
    ok, info = await webhooks.deliver(RaisingSession(), "https://x", {})
    if ok or "ClientConnectionError" not in info:
        fail("deliver: ClientError surfaced", info)
    passed("deliver: transport errors return not-ok with type in info")


asyncio.run(_delivery_tests())


# ---------------------------------------------------------------------------
# 6. RSS feed endpoint
# ---------------------------------------------------------------------------

# Seed a fake race with a big movement
import datetime as _dt
now = _dt.datetime.now(_dt.timezone.utc)
with _sql.connect(DB_PATH) as _c:
    # An active market so the feed picks up the race title
    _c.execute(
        """INSERT INTO midterm_markets (source, source_id, title, event_title, race_type, state,
                                         outcomes, volume, active, closed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, source_id) DO UPDATE SET event_title=excluded.event_title""",
        ("polymarket", "feed_test_1", "Test", "Race that <moved>", "senate", "ZZ",
         '[{"name":"Yes","probability":0.6}]', 100, 1, 0),
    )
    # Two divergence snapshots with an 8pp jump
    for offset, prob in [(2 * 3600, 0.50), (60, 0.58)]:
        ts = (now - _dt.timedelta(seconds=offset)).isoformat()
        _c.execute(
            """INSERT INTO midterm_divergence_snapshots
                (race_key, state, race_type, polymarket_prob, snapshot_time)
               VALUES (?, ?, ?, ?, ?)""",
            ("senate_ZZ", "ZZ", "senate", prob, ts),
        )
    _c.commit()


async def _feed_test():
    resp = await main_mod.feed_movements()
    body = resp.body.decode() if hasattr(resp, "body") else str(resp)
    if "<feed" not in body or "</feed>" not in body:
        fail("feed_movements: well-formed Atom feed", body[:200])
    if "senate_ZZ" not in body:
        fail("feed_movements: includes moved race", body[:300])
    # HTML in title got escaped
    if "<moved>" in body:
        fail("feed_movements: dangerous HTML escaped", "raw '<moved>' present")
    if "&lt;moved&gt;" not in body:
        fail("feed_movements: HTML escapes applied", "expected &lt;moved&gt;")
    passed("feed_movements: Atom feed with moved races + HTML-safe titles")


asyncio.run(_feed_test())

# Clean up
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_markets WHERE source_id='feed_test_1'")
    _c.execute("DELETE FROM midterm_divergence_snapshots WHERE race_key='senate_ZZ'")
    _c.commit()


# ---------------------------------------------------------------------------
# 7. Digest subscriber CRUD
# ---------------------------------------------------------------------------

_db.upsert_digest_subscription("u1", "alice@example.com", enabled=True)
subs = _db.get_digest_subscribers()
if not any(s["user_id"] == "u1" for s in subs):
    fail("digest: subscribed user appears")
passed("digest: subscribe inserts row")

_db.upsert_digest_subscription("u1", "alice@example.com", enabled=False)
subs = _db.get_digest_subscribers()
if any(s["user_id"] == "u1" for s in subs):
    fail("digest: disabling drops user from active list")
passed("digest: disable removes from active subscribers")


# ---------------------------------------------------------------------------
# 8. V1 API auth (require_api_key)
# ---------------------------------------------------------------------------

from fastapi import HTTPException as _HTTPException


class _FakeRequest:
    def __init__(self, auth: str | None = None):
        self.headers = {"authorization": auth} if auth else {}


async def _auth_tests():
    # No header
    try:
        await main_mod._require_api_key(_FakeRequest())
    except _HTTPException as e:
        if e.status_code != 401:
            fail("_require_api_key: missing header → 401", str(e))
    else:
        fail("_require_api_key: missing header should raise")
    passed("_require_api_key: missing Authorization → 401")

    # Bad scheme
    try:
        await main_mod._require_api_key(_FakeRequest("Basic xyz"))
    except _HTTPException as e:
        if e.status_code != 401:
            fail("_require_api_key: non-Bearer scheme → 401", str(e))
    else:
        fail("_require_api_key: non-Bearer should raise")
    passed("_require_api_key: non-Bearer scheme → 401")

    # Unknown bearer token
    try:
        await main_mod._require_api_key(_FakeRequest("Bearer nonexistent_token_xyz"))
    except _HTTPException as e:
        if e.status_code != 401:
            fail("_require_api_key: unknown token → 401", str(e))
    else:
        fail("_require_api_key: unknown token should raise")
    passed("_require_api_key: unknown token → 401")

    # Valid premium token
    plain, prefix, h = api_keys.generate()
    _db.store_api_key("u-auth", prefix, h, tier="premium", rate_limit_rpm=600)
    record = await main_mod._require_api_key(_FakeRequest(f"Bearer {plain}"))
    if record.get("tier") != "premium":
        fail("_require_api_key: valid premium returns the record", str(record))
    passed("_require_api_key: valid premium token authenticates")

    # Revoked token
    _db.revoke_api_key(record["id"], "u-auth")
    try:
        await main_mod._require_api_key(_FakeRequest(f"Bearer {plain}"))
    except _HTTPException as e:
        if e.status_code != 401:
            fail("_require_api_key: revoked token → 401", str(e))
    else:
        fail("_require_api_key: revoked token should raise")
    passed("_require_api_key: revoked token → 401")


asyncio.run(_auth_tests())


# ---------------------------------------------------------------------------
# 9. Routes registered
# ---------------------------------------------------------------------------

paths = {r.path for r in main_mod.app.routes if hasattr(r, "path")}
required = {
    "/feed/movements.xml",
    "/premium/webhooks", "/premium/webhooks/{webhook_id}",
    "/premium/digest",
    "/premium/api-keys", "/premium/api-keys/{key_id}",
    "/v1/api/races", "/v1/api/race/{race_key}",
    "/v1/api/race/{race_key}/history", "/v1/api/race/{race_key}/history.csv",
    "/v1/api/accuracy", "/v1/api/movements",
}
missing = required - paths
if missing:
    fail("routes: all new distribution + API endpoints registered", f"missing={missing}")
passed(f"routes: all {len(required)} new endpoints registered")


print("\nAll distribution tests passed.")
