import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import db
import ingest_metaculus
import resolver


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _close_in(days):
    return _iso(datetime.now(timezone.utc) + timedelta(days=days))


def _post(qid=101, prob=0.62, close_days=5.0, **over):
    """Post-2024 rewrite shape: nested question object with aggregations."""
    base = {
        "id": qid,
        "title": f"Will thing {qid} happen?",
        "nr_forecasters": 57,
        "projects": {"category": [{"name": "Politics", "slug": "politics"}]},
        "question": {
            "id": qid,
            "type": "binary",
            "scheduled_close_time": _close_in(close_days),
            "scheduled_resolve_time": _close_in(close_days + 1),
            "resolution": None,
            "aggregations": {
                "recency_weighted": {
                    "latest": {
                        "centers": [prob],
                        "forecast_values": [round(1 - prob, 4), prob],
                        "means": [prob],
                        "forecaster_count": 57,
                    }
                }
            },
        },
    }
    base.update(over)
    return base


def _legacy(qid=201, prob=0.4, close_days=3.0, **over):
    """Legacy api2 shape: flat row, possibilities.type, community_prediction."""
    base = {
        "id": qid,
        "title": f"Legacy question {qid}?",
        "possibilities": {"type": "binary"},
        "close_time": _close_in(close_days),
        "resolve_time": _close_in(close_days + 2),
        "number_of_forecasters": 120,
        "community_prediction": {"full": {"q1": 0.3, "q2": prob, "q3": 0.5}},
        "categories": ["Economy & Business"],
    }
    base.update(over)
    return base


def _page(results, next_url=None, count=None):
    return {"count": count if count is not None else len(results), "next": next_url, "previous": None, "results": results}


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(ingest_metaculus.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(ingest_metaculus, "_PAGE_PAUSE", 0)
    monkeypatch.setattr(ingest_metaculus, "_RETRY_BACKOFF", 0)


def _run(coro):
    return asyncio.run(coro)


def test_normalizes_new_shape(monkeypatch):
    post = _post()
    close = post["question"]["scheduled_close_time"]

    def handler(request):
        return httpx.Response(200, json=_page([post]))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert len(rows) == 1
    row = rows[0]
    assert row["venue"] == "metaculus"
    assert row["venue_id"] == "101"
    assert row["event_ticker"] is None
    assert row["question"] == "Will thing 101 happen?"
    assert row["category"] == "politics"
    assert row["url"] == "https://www.metaculus.com/questions/101/"
    assert row["end_date"] == close
    assert row["yes_bid"] is None and row["yes_ask"] is None
    assert row["last_price"] == 0.62
    assert row["liquidity"] == 57.0
    assert row["volume_24h"] is None
    assert row["yes_token_id"] is None and row["no_token_id"] is None


def test_normalizes_legacy_shape(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_page([_legacy()]))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert len(rows) == 1
    row = rows[0]
    assert row["venue_id"] == "201"
    assert row["last_price"] == 0.4
    assert row["liquidity"] == 120.0
    assert row["category"] == "economics"
    assert row["url"] == "https://www.metaculus.com/questions/201/"


def test_prediction_extraction_fallback_chain():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=30)

    centers_only = _post(qid=1)
    centers_only["question"]["aggregations"]["recency_weighted"]["latest"] = {"centers": [0.71], "forecaster_count": 5}
    assert ingest_metaculus._normalize_question(centers_only, now, cutoff)["last_price"] == 0.71

    fv_only = _post(qid=2)
    fv_only["question"]["aggregations"]["recency_weighted"]["latest"] = {"centers": [], "forecast_values": [0.2, 0.8]}
    assert ingest_metaculus._normalize_question(fv_only, now, cutoff)["last_price"] == 0.8

    means_only = _post(qid=3)
    means_only["question"]["aggregations"]["recency_weighted"]["latest"] = {"means": [0.33]}
    assert ingest_metaculus._normalize_question(means_only, now, cutoff)["last_price"] == 0.33

    out_of_range = _post(qid=4)
    out_of_range["question"]["aggregations"]["recency_weighted"]["latest"] = {"centers": [1.7]}
    assert ingest_metaculus._normalize_question(out_of_range, now, cutoff) is None


def test_skips_without_community_prediction(monkeypatch):
    hidden = _post(qid=301)
    hidden["question"]["aggregations"]["recency_weighted"]["latest"] = {}
    no_aggs = _post(qid=302)
    no_aggs["question"]["aggregations"] = {}
    visible = _post(qid=303)

    def handler(request):
        return httpx.Response(200, json=_page([hidden, no_aggs, visible]))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert [r["venue_id"] for r in rows] == ["303"]


def test_horizon_filter(monkeypatch):
    inside = _post(qid=401, close_days=1.0)
    outside = _post(qid=402, close_days=10.0)
    already_closed = _post(qid=403, close_days=-1.0)

    def handler(request):
        return httpx.Response(200, json=_page([inside, outside, already_closed]))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(3))
    assert [r["venue_id"] for r in rows] == ["401"]


def test_skips_non_binary_and_malformed(monkeypatch):
    numeric = _post(qid=501)
    numeric["question"]["type"] = "numeric"
    untyped = _post(qid=502)
    del untyped["question"]["type"]
    no_id = _post(qid=503)
    del no_id["id"]
    del no_id["question"]["id"]
    untitled = _post(qid=504, title="")
    untitled["question"].pop("title", None)
    good = _post(qid=505)

    def handler(request):
        return httpx.Response(200, json=_page([numeric, untyped, no_id, untitled, "not-a-dict", good]))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert [r["venue_id"] for r in rows] == ["505"]


def test_category_mapping():
    cases = [
        (["Politics"], "politics"),
        (["Geopolitics"], "politics"),
        (["Elections"], "politics"),
        (["Law"], "politics"),
        (["Economy & Business"], "economics"),
        # fed/weather align with the kalshi series-prefix buckets
        (["Fed"], "fed"),
        (["Federal Reserve"], "fed"),
        (["Interest Rates"], "fed"),
        (["Weather"], "weather"),
        (["Hurricane Season"], "weather"),
        (["Cryptocurrencies"], "finance"),
        (["AI"], "tech"),
        (["Artificial Intelligence"], "tech"),
        (["Computing and Math"], "tech"),
        (["Sports & Entertainment"], "sports"),
        (["Health & Pandemics"], "science"),
        (["Environment & Climate"], "science"),
        (["Natural Sciences"], "science"),
        (["Space"], "science"),
        (["Metaculus Meta"], "other"),
        ([], "other"),
    ]
    for labels, expected in cases:
        assert ingest_metaculus.map_category(labels) == expected, labels


def test_offset_pagination_and_dedup(monkeypatch):
    page1 = [_post(qid=i) for i in range(100)]
    page2 = [_post(qid=99), _post(qid=200)]
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if int(params["offset"]) == 0:
            return httpx.Response(200, json=_page(page1, next_url="https://www.metaculus.com/api2/questions/?offset=100", count=102))
        return httpx.Response(200, json=_page(page2, next_url=None, count=102))

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    ids = [r["venue_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 101
    assert "200" in ids
    assert [c["offset"] for c in calls] == ["0", "100"]
    assert calls[0]["status"] == "open"
    assert calls[0]["forecast_type"] == "binary"
    assert calls[0]["limit"] == "100"
    assert calls[0]["with_cp"] == "true"


def test_auth_gate_returns_empty_without_retry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="Permission Error: The API is only available to authenticated users.")

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert rows == []
    assert len(calls) == 1


def test_http_error_retries_once_then_returns_empty(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_metaculus.fetch_horizon(30))
    assert rows == []
    assert len(calls) == 2


def test_token_header_from_env(monkeypatch):
    seen_auth = []

    def handler(request):
        seen_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=_page([_post()]))

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("METACULUS_API_TOKEN", "sekrit")
    assert len(_run(ingest_metaculus.fetch_horizon(30))) == 1
    monkeypatch.delenv("METACULUS_API_TOKEN")
    assert len(_run(ingest_metaculus.fetch_horizon(30))) == 1
    assert seen_auth == ["Token sekrit", None]


# ── resolver branch ──────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeMetacClient:
    def __init__(self, by_qid):
        self.by_qid = by_qid
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, headers))
        qid = url.rstrip("/").rsplit("/", 1)[1]
        payload = self.by_qid.get(qid)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            return FakeResponse({"detail": "not found"}, status_code=404)
        return FakeResponse(payload)


def _market(venue_id, hours_past_close=12):
    return {
        "venue": "metaculus",
        "venue_id": venue_id,
        "question": f"metaculus {venue_id}?",
        "category": "science",
        "url": f"https://www.metaculus.com/questions/{venue_id}/",
        "end_date": _iso(datetime.now(timezone.utc) - timedelta(hours=hours_past_close)),
    }


def _add_calibration(conn, uid, displayed_at="2026-08-06", outcome=None):
    db.log_calibration_row(
        conn,
        {
            "market_uid": uid,
            "platform": "metaculus",
            "category": "science",
            "source": "market_baseline",
            "market_prob": 0.6,
            "outcome": outcome,
            "displayed_at": displayed_at,
        },
    )


def _install(monkeypatch, client):
    monkeypatch.setattr(resolver, "_make_client", lambda: client)
    return client


def test_metaculus_resolution_matrix():
    cases = [
        ({"question": {"resolution": "yes"}}, 1),
        ({"question": {"resolution": "no"}}, 0),
        ({"question": {"resolution": " Yes "}}, 1),
        ({"question": {"resolution": "ambiguous"}}, resolver.METACULUS_VOID),
        ({"question": {"resolution": "annulled"}}, resolver.METACULUS_VOID),
        ({"question": {"resolution": None}}, None),
        ({"resolution": 1.0}, 1),
        ({"resolution": 0.0}, 0),
        ({"resolution": "1.0"}, 1),
        ({"resolution": -1}, resolver.METACULUS_VOID),
        ({"resolution": -2.0}, resolver.METACULUS_VOID),
        ({"resolution": 0.5}, None),
        ({"resolution": "someday"}, None),
        ({"question": {"resolution": None}, "resolution": 1.0}, 1),
        ({}, None),
        ("not-a-dict", None),
    ]
    for payload, expected in cases:
        assert resolver._metaculus_resolution(payload) == expected, payload


def test_resolver_resolves_yes_and_no(conn, monkeypatch):
    uid_yes = db.upsert_market(conn, _market("301"))
    uid_no = db.upsert_market(conn, _market("302"))
    _add_calibration(conn, uid_yes)
    _add_calibration(conn, uid_no)

    client = _install(
        monkeypatch,
        FakeMetacClient(
            {
                "301": {"id": 301, "question": {"id": 301, "type": "binary", "resolution": "yes"}},
                "302": {"id": 302, "possibilities": {"type": "binary"}, "resolution": 0.0},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 2
    urls = [u for u, _ in client.calls]
    assert resolver.METACULUS_QUESTION_URL.format(qid="301") in urls

    for uid, outcome in ((uid_yes, 1), (uid_no, 0)):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 1
        assert m["active"] == 0
        assert m["outcome"] == outcome
        assert m["resolved_at"]
        row = conn.execute("SELECT outcome, resolved_at FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
        assert row["outcome"] == outcome
        assert row["resolved_at"]


def test_resolver_annulled_and_ambiguous_no_poison(conn, monkeypatch):
    uid_ann = db.upsert_market(conn, _market("303"))
    uid_amb = db.upsert_market(conn, _market("304"))
    _add_calibration(conn, uid_ann)
    _add_calibration(conn, uid_ann, displayed_at="2026-08-05", outcome=1)
    _add_calibration(conn, uid_amb)

    _install(
        monkeypatch,
        FakeMetacClient(
            {
                "303": {"id": 303, "question": {"id": 303, "type": "binary", "resolution": "annulled"}},
                "304": {"id": 304, "possibilities": {"type": "binary"}, "resolution": -1},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 2

    for uid in (uid_ann, uid_amb):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 1
        assert m["active"] == 0
        assert m["outcome"] is None
        assert m["resolved_at"]

    # unresolved calibration rows are deleted, already-scored rows survive
    rows = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid_ann,)).fetchall()
    assert [r["outcome"] for r in rows] == [1]
    assert conn.execute("SELECT COUNT(*) FROM calibration WHERE market_uid = ?", (uid_amb,)).fetchone()[0] == 0
    assert db.unresolved_past_close(conn, grace_hours=6) == []


def test_resolver_pending_question_left_untouched(conn, monkeypatch):
    uid = db.upsert_market(conn, _market("305"))
    _add_calibration(conn, uid)

    _install(monkeypatch, FakeMetacClient({"305": {"id": 305, "question": {"id": 305, "type": "binary", "resolution": None}}}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 0
    m = db.get_market(conn, uid)
    assert m["resolved"] == 0
    assert m["outcome"] is None
    row = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
    assert row["outcome"] is None


def test_resolver_error_skips_and_continues(conn, monkeypatch):
    bad_uid = db.upsert_market(conn, _market("306"))
    good_uid = db.upsert_market(conn, _market("307"))

    _install(
        monkeypatch,
        FakeMetacClient(
            {
                "306": RuntimeError("connection reset"),
                "307": {"id": 307, "question": {"id": 307, "type": "binary", "resolution": "yes"}},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    assert db.get_market(conn, bad_uid)["resolved"] == 0
    assert db.get_market(conn, good_uid)["outcome"] == 1


@pytest.mark.skipif(not os.environ.get("METACULUS_LIVE"), reason="live smoke; run with METACULUS_LIVE=1")
def test_live_smoke_30_days():
    rows = _run(ingest_metaculus.fetch_horizon(30))
    if not rows and not os.environ.get("METACULUS_API_TOKEN"):
        pytest.skip("Metaculus anonymous API access is disabled (403 verified live 2026-08-08) — set METACULUS_API_TOKEN to run the live smoke")
    report = {
        "binary_questions_closing_30d": len(rows),
        "sample": [
            {"venue_id": r["venue_id"], "question": r["question"], "community_prob": r["last_price"], "forecasters": r["liquidity"], "end_date": r["end_date"], "url": r["url"]} for r in rows[:3]
        ],
    }
    print("\nLIVE_SMOKE " + json.dumps(report, indent=2))
    assert rows
    for r in rows:
        assert r["venue"] == "metaculus"
        assert r["last_price"] is not None and 0.0 <= r["last_price"] <= 1.0
        assert r["end_date"]
