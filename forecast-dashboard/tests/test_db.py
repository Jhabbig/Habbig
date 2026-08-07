from datetime import datetime, timedelta, timezone

import pytest

import db


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(venue="polymarket", venue_id="m1", **kw):
    end = datetime.now(timezone.utc) + timedelta(days=2)
    base = {
        "venue": venue,
        "venue_id": venue_id,
        "event_ticker": "EVT",
        "question": "Will it rain?",
        "category": "weather",
        "url": "https://example.com/m",
        "end_date": _iso(end),
        "yes_token_id": "tok-yes",
        "no_token_id": "tok-no",
    }
    base.update(kw)
    return base


def test_ddl_creates_all_tables(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"markets", "snapshots", "model_probs", "venue_links", "calibration"} <= tables


def test_upsert_market_idempotent(conn):
    uid = db.upsert_market(conn, _market())
    assert uid == "polymarket:m1"
    first = db.get_market(conn, uid)

    uid2 = db.upsert_market(conn, _market(question="Will it rain tomorrow?"))
    assert uid2 == uid
    rows = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    assert rows == 1

    second = db.get_market(conn, uid)
    assert second["question"] == "Will it rain tomorrow?"
    assert second["first_seen"] == first["first_seen"]


def test_compute_baseline_tiers():
    r = db.compute_baseline(0.49, 0.51, 0.50, 5000)
    assert r == {"baseline": 0.5, "spread": 0.02, "tier": "high"}

    r = db.compute_baseline(0.48, 0.52, None, 100)
    assert r["tier"] == "medium"
    assert r["baseline"] == 0.5
    assert r["spread"] == pytest.approx(0.04)

    r = db.compute_baseline(0.40, 0.60, None, 50)
    assert r["tier"] == "low"

    r = db.compute_baseline(0.45, None, 0.5, 1000)
    assert r == {"baseline": 0.45, "spread": None, "tier": "thin"}

    r = db.compute_baseline(None, 0.55, 0.5, 1000)
    assert r == {"baseline": 0.55, "spread": None, "tier": "thin"}

    r = db.compute_baseline(None, None, 0.62, 1000)
    assert r == {"baseline": 0.62, "spread": None, "tier": "stale"}


def test_latest_snapshots_returns_latest_with_model_probs(conn):
    uid = db.upsert_market(conn, _market())
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
    db.insert_snapshot(conn, {"ts": old, "market_uid": uid, "baseline_prob": 0.40})
    db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": 0.55, "spread": 0.02, "liquidity": 2000})
    db.insert_model_prob(conn, {"market_uid": uid, "source": "weather_model", "model_prob": 0.7, "prob_method": "gaussian"})

    rows = db.latest_snapshots(conn, horizon_days=7)
    assert len(rows) == 1
    assert rows[0]["baseline_prob"] == 0.55
    assert rows[0]["model_probs"]["weather_model"]["model_prob"] == 0.7

    assert db.latest_snapshots(conn, horizon_days=1) == []


def test_latest_snapshot_single_uid(conn):
    uid = db.upsert_market(conn, _market())
    assert db.latest_snapshot(conn, uid) is None
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    db.insert_snapshot(conn, {"ts": old, "market_uid": uid, "baseline_prob": 0.40})
    db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": 0.55, "spread": 0.02})
    snap = db.latest_snapshot(conn, uid)
    assert snap["baseline_prob"] == 0.55
    assert snap["spread"] == 0.02


def test_resolution_flow(conn):
    uid = db.upsert_market(conn, _market(end_date=_iso(datetime.now(timezone.utc) - timedelta(hours=12))))
    pending = db.unresolved_past_close(conn, grace_hours=6)
    assert [m["uid"] for m in pending] == [uid]
    assert db.unresolved_past_close(conn, grace_hours=24) == []

    db.log_calibration_row(
        conn,
        {
            "market_uid": uid,
            "platform": "polymarket",
            "category": "weather",
            "source": "market_baseline",
            "market_prob": 0.6,
            "displayed_at": "2026-08-06",
        },
    )
    db.mark_resolved(conn, uid, outcome=1)
    n = db.backfill_calibration_outcomes(conn, uid, outcome=1)
    assert n == 1

    m = db.get_market(conn, uid)
    assert m["resolved"] == 1 and m["outcome"] == 1 and m["resolved_at"]
    assert db.unresolved_past_close(conn, grace_hours=6) == []


def test_calibration_insert_or_ignore_dedup(conn):
    row = {
        "market_uid": "polymarket:m1",
        "platform": "polymarket",
        "category": "weather",
        "source": "market_baseline",
        "market_prob": 0.6,
        "displayed_at": "2026-08-07",
    }
    db.log_calibration_row(conn, row)
    db.log_calibration_row(conn, {**row, "market_prob": 0.9})
    rows = conn.execute("SELECT market_prob FROM calibration").fetchall()
    assert len(rows) == 1
    assert rows[0]["market_prob"] == 0.6

    db.log_calibration_row(conn, {**row, "displayed_at": "2026-08-08"})
    db.log_calibration_row(conn, {**row, "source": "weather_model", "model_prob": 0.7})
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 3


def test_venue_links_manual_status_survives_rematch(conn):
    db.upsert_link(conn, "polymarket:a", "kalshi:b", "fuzzy", 0.8, "auto")
    db.set_link_status(conn, "polymarket:a", "kalshi:b", "confirmed")
    db.upsert_link(conn, "polymarket:a", "kalshi:b", "fuzzy", 0.9, "auto")

    lk = db.links(conn)[0]
    assert lk["status"] == "confirmed"
    assert lk["score"] == 0.9

    assert not db.set_link_status(conn, "polymarket:x", "kalshi:y", "rejected")


def test_get_brier_score_known_values(conn):
    def cal(uid, source, model_prob, market_prob, outcome, platform="polymarket", category="weather"):
        db.log_calibration_row(
            conn,
            {
                "market_uid": uid,
                "platform": platform,
                "category": category,
                "source": source,
                "model_prob": model_prob,
                "market_prob": market_prob,
                "outcome": outcome,
                "displayed_at": "2026-08-01",
            },
        )

    cal("p:1", "weather_model", 0.8, 0.7, 1)
    cal("p:2", "weather_model", 0.4, 0.5, 0)
    cal("k:3", "market_baseline", None, 0.6, 1, platform="kalshi", category="fed")
    cal("p:4", "market_baseline", None, 0.9, None)  # unresolved — excluded

    s = db.get_brier_score(conn)
    assert s["n"] == 3
    assert s["n_model"] == 2
    # model: ((0.8-1)^2 + (0.4-0)^2) / 2 = (0.04 + 0.16) / 2 = 0.10
    assert s["brier_model"] == pytest.approx(0.10)
    # market head-to-head: ((0.7-1)^2 + (0.5-0)^2) / 2 = (0.09 + 0.25) / 2 = 0.17
    assert s["brier_market"] == pytest.approx(0.17)
    assert s["edge_vs_market"] == pytest.approx(0.07)

    assert s["by_source"]["weather_model"]["n"] == 2
    assert s["by_source"]["weather_model"]["brier"] == pytest.approx(0.10)
    # market_baseline scored on market_prob: (0.6-1)^2 = 0.16
    assert s["by_source"]["market_baseline"]["brier"] == pytest.approx(0.16)
    assert s["by_platform"]["kalshi"]["n"] == 1
    assert s["by_category"]["fed"]["brier"] == pytest.approx(0.16)

    assert s["buckets"]["80-90%"] == {"count": 1, "avg_prob": 0.8, "hit_rate": 1.0}
    assert s["buckets"]["40-50%"] == {"count": 1, "avg_prob": 0.4, "hit_rate": 0.0}
    assert s["buckets"]["60-70%"] == {"count": 1, "avg_prob": 0.6, "hit_rate": 1.0}


def test_get_brier_score_empty(conn):
    s = db.get_brier_score(conn)
    assert s["n"] == 0
    assert s["brier_model"] is None
    assert s["buckets"] == {}


def test_search_markets_term_and_case_insensitive(conn):
    u_rain = db.upsert_market(conn, _market(venue_id="m1", question="Will it rain in Berlin on Friday?"))
    u_summit = db.upsert_market(conn, _market(venue_id="m2", question="Will Berlin host the summit?"))
    u_oslo = db.upsert_market(conn, _market(venue_id="m3", question="Will it snow in Oslo?"))
    for uid, liq in ((u_rain, 100), (u_summit, 5000), (u_oslo, 50)):
        db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": 0.5, "liquidity": liq})
    db.insert_model_prob(conn, {"market_uid": u_rain, "source": "llm", "model_prob": 0.7, "prob_method": "llm_forecast"})

    rows = db.search_markets(conn, "will BERLIN")
    assert [r["uid"] for r in rows] == [u_summit, u_rain]  # liquidity desc

    rows = db.search_markets(conn, "Berlin RAIN")
    assert [r["uid"] for r in rows] == [u_rain]
    assert rows[0]["baseline_prob"] == 0.5
    assert rows[0]["model_probs"]["llm"]["model_prob"] == 0.7

    assert db.search_markets(conn, "berlin oslo") == []
    assert db.search_markets(conn, "   ") == []


def test_search_markets_ignores_edge_punctuation(conn):
    uid = db.upsert_market(conn, _market(venue_id="m1", question="Will the Fed cut interest rates at the September FOMC meeting?"))
    db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": 0.6, "liquidity": 100})

    # Natural questions end in '?'; the trailing term must still match.
    assert [r["uid"] for r in db.search_markets(conn, "Will the Fed cut interest rates?")] == [uid]
    assert [r["uid"] for r in db.search_markets(conn, '"fed" rates,')] == [uid]
    assert db.search_terms("?? !!") == []
    assert db.search_markets(conn, "?? !!") == []


def test_search_markets_excludes_resolved_and_snapshotless(conn):
    u1 = db.upsert_market(conn, _market(venue_id="m1", question="Will it rain in Berlin?"))
    u2 = db.upsert_market(conn, _market(venue_id="m2", question="Will it rain in Berlin twice?"))
    db.upsert_market(conn, _market(venue_id="m3", question="Will it rain in Berlin thrice?"))  # no snapshot
    db.insert_snapshot(conn, {"market_uid": u1, "baseline_prob": 0.4, "liquidity": 10})
    db.insert_snapshot(conn, {"market_uid": u2, "baseline_prob": 0.6, "liquidity": 20})

    assert [r["uid"] for r in db.search_markets(conn, "berlin rain")] == [u2, u1]

    db.mark_resolved(conn, u2, outcome=1)
    assert [r["uid"] for r in db.search_markets(conn, "berlin rain")] == [u1]


def test_create_custom_market_upsert(conn):
    uid = db.create_custom_market(conn, "Will X happen by Friday?", "2026-08-14T00:00:00Z")
    assert uid.startswith("custom:")
    m = db.get_market(conn, uid)
    assert m["venue"] == "custom"
    assert m["category"] == "custom"
    assert m["active"] == 1
    assert m["end_date"] == "2026-08-14T00:00:00Z"

    # same question (any case) -> same uid; missing end_date doesn't clear the stored one
    uid2 = db.create_custom_market(conn, "WILL X HAPPEN BY FRIDAY?", None)
    assert uid2 == uid
    assert conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1
    assert db.get_market(conn, uid)["end_date"] == "2026-08-14T00:00:00Z"

    uid3 = db.create_custom_market(conn, "Will Y happen by Friday?")
    assert uid3 != uid
    assert db.get_market(conn, uid3)["end_date"] is None


def test_status_counts(conn):
    uid = db.upsert_market(conn, _market())
    db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": 0.5})
    c = db.status_counts(conn)
    assert c["markets"] == 1
    assert c["markets_active"] == 1
    assert c["snapshots"] == 1
    assert c["calibration"] == 0
