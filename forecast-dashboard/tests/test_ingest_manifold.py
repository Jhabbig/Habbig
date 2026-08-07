import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import db
import ingest_manifold
import resolver


def _ms(days: float) -> int:
    return int((time.time() + days * 86400) * 1000)


def _mk(**over):
    base = {
        "id": "mkt-abc123",
        "question": "Will X happen by September?",
        "outcomeType": "BINARY",
        "isResolved": False,
        "closeTime": _ms(3),
        "probability": 0.42,
        "totalLiquidity": 850.0,
        "volume24Hours": 120.5,
        "url": "https://manifold.markets/alice/will-x-happen",
        "groupSlugs": ["politics"],
    }
    base.update(over)
    return base


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(ingest_manifold.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(ingest_manifold, "_PAGE_PAUSE", 0)
    monkeypatch.setattr(ingest_manifold, "_RETRY_BACKOFF", 0)


def _run(coro):
    return asyncio.run(coro)


# ── ingest: normalization and filters ────────────────────────────────────────


def test_normalizes_contract_fields(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[_mk()])

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert len(rows) == 1
    row = rows[0]
    assert row["venue"] == "manifold"
    assert row["venue_id"] == "mkt-abc123"
    assert row["event_ticker"] is None
    assert row["question"] == "Will X happen by September?"
    assert row["category"] == "politics"
    assert row["url"] == "https://manifold.markets/alice/will-x-happen"
    assert row["end_date"].endswith("Z")
    assert row["yes_bid"] is None and row["yes_ask"] is None
    assert row["last_price"] == 0.42
    assert row["liquidity"] == 850.0
    assert row["volume_24h"] == 120.5
    assert row["yes_token_id"] is None and row["no_token_id"] is None


def test_binary_and_resolved_filter(monkeypatch):
    def handler(request):
        markets = [
            _mk(id="keep-binary"),
            _mk(id="drop-mc", outcomeType="MULTIPLE_CHOICE"),
            _mk(id="drop-numeric", outcomeType="PSEUDO_NUMERIC"),
            _mk(id="drop-resolved", isResolved=True),
            _mk(id=None),
            {"question": "no id at all"},
        ]
        return httpx.Response(200, json=markets)

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["keep-binary"]


def test_horizon_filter(monkeypatch):
    def handler(request):
        markets = [
            _mk(id="keep-tomorrow", closeTime=_ms(1)),
            _mk(id="keep-day6", closeTime=_ms(6)),
            _mk(id="drop-past", closeTime=_ms(-1)),
            _mk(id="drop-day10", closeTime=_ms(10)),
            _mk(id="drop-no-close", closeTime=None),
        ]
        return httpx.Response(200, json=markets)

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["keep-tomorrow", "keep-day6"]


def test_category_mapping():
    cases = [
        (["us-politics"], "politics"),
        (["elections", "whatever-else"], "politics"),
        (["inflation"], "economics"),
        # fed/weather align with the kalshi series-prefix buckets, not economics/science
        (["federal-reserve"], "fed"),
        (["interest-rates", "economics"], "fed"),
        (["weather"], "weather"),
        (["bitcoin"], "finance"),
        (["nba"], "sports"),
        (["space"], "science"),
        (["ai"], "tech"),
        (["Technology"], "tech"),
        (["random-slug"], "other"),
        ([], "other"),
        (None, "other"),
        ("politics", "other"),
    ]
    for slugs, expected in cases:
        assert ingest_manifold._category({"groupSlugs": slugs}) == expected, slugs
    assert ingest_manifold._category({}) == "other"


def test_category_mapped_through_fetch(monkeypatch):
    def handler(request):
        markets = [
            _mk(id="m-pol", groupSlugs=["geopolitics"]),
            _mk(id="m-fin", groupSlugs=["crypto", "one-off-group"]),
            _mk(id="m-other", groupSlugs=["knitting"]),
            _mk(id="m-none", groupSlugs=None),
        ]
        return httpx.Response(200, json=markets)

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    by_id = {r["venue_id"]: r["category"] for r in rows}
    assert by_id == {"m-pol": "politics", "m-fin": "finance", "m-other": "other", "m-none": "other"}


def test_out_of_range_probability_becomes_none(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[_mk(id="m-junk", probability=1.7), _mk(id="m-missing", probability=None)])

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["m-junk", "m-missing"]
    assert rows[0]["last_price"] is None
    assert rows[1]["last_price"] is None


# ── ingest: pagination and caps ──────────────────────────────────────────────


def test_before_cursor_pagination_terminates(monkeypatch):
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if params.get("before") == "m-2":
            return httpx.Response(200, json=[_mk(id="m-3")])
        return httpx.Response(200, json=[_mk(id="m-1"), _mk(id="m-2")])

    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(ingest_manifold, "PAGE_LIMIT", 2)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["m-1", "m-2", "m-3"]
    assert len(calls) == 2
    assert "before" not in calls[0]
    assert calls[0]["limit"] == "2"
    assert calls[1]["before"] == "m-2"


def test_row_cap_truncates(monkeypatch):
    counter = iter(range(1000))

    def handler(request):
        return httpx.Response(200, json=[_mk(id=f"m-{next(counter)}"), _mk(id=f"m-{next(counter)}")])

    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(ingest_manifold, "PAGE_LIMIT", 2)
    monkeypatch.setattr(ingest_manifold, "MAX_ROWS", 3)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert len(rows) == 3


def test_page_cap_truncates(monkeypatch):
    counter = iter(range(1000))
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[_mk(id=f"m-{next(counter)}"), _mk(id=f"m-{next(counter)}")])

    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(ingest_manifold, "PAGE_LIMIT", 2)
    monkeypatch.setattr(ingest_manifold, "MAX_PAGES", 2)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert len(rows) == 4
    assert len(calls) == 2


def test_http_error_retries_once_then_returns_empty(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_manifold.fetch_horizon(7))
    assert rows == []
    assert len(calls) == 2


# ── resolver: manifold settlement ────────────────────────────────────────────


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_market(venue_id, hours_past_close=12):
    return {
        "venue": "manifold",
        "venue_id": venue_id,
        "question": f"manifold {venue_id}?",
        "category": "other",
        "url": f"https://manifold.markets/x/{venue_id}",
        "end_date": _iso(datetime.now(timezone.utc) - timedelta(hours=hours_past_close)),
    }


def _add_calibration(conn, uid):
    db.log_calibration_row(
        conn,
        {
            "market_uid": uid,
            "platform": "manifold",
            "category": "other",
            "source": "market_baseline",
            "market_prob": 0.6,
            "displayed_at": "2026-08-06",
        },
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeManifoldClient:
    def __init__(self, by_id):
        self.by_id = by_id
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append(url)
        market_id = url.rsplit("/", 1)[1]
        payload = self.by_id.get(market_id)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            return FakeResponse({"error": "not found"}, status_code=404)
        return FakeResponse(payload)


def _install(monkeypatch, client):
    monkeypatch.setattr(resolver, "_make_client", lambda: client)
    return client


def test_resolves_yes_and_no(conn, monkeypatch):
    yes_uid = db.upsert_market(conn, _past_market("m-yes"))
    no_uid = db.upsert_market(conn, _past_market("m-no"))
    _add_calibration(conn, yes_uid)
    _add_calibration(conn, no_uid)

    client = _install(
        monkeypatch,
        FakeManifoldClient(
            {
                "m-yes": {"isResolved": True, "resolution": "YES"},
                "m-no": {"isResolved": True, "resolution": "NO"},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 2
    assert len(client.calls) == 2
    for uid, outcome in ((yes_uid, 1), (no_uid, 0)):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 1
        assert m["active"] == 0
        assert m["outcome"] == outcome
        rows = conn.execute("SELECT outcome, resolved_at FROM calibration WHERE market_uid = ?", (uid,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["outcome"] == outcome
        assert rows[0]["resolved_at"]
    assert db.unresolved_past_close(conn, grace_hours=6) == []


def test_unresolved_market_left_pending(conn, monkeypatch):
    uid = db.upsert_market(conn, _past_market("m-open"))
    _add_calibration(conn, uid)
    _install(monkeypatch, FakeManifoldClient({"m-open": {"isResolved": False, "probability": 0.5}}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 0
    m = db.get_market(conn, uid)
    assert m["resolved"] == 0
    assert m["outcome"] is None
    row = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
    assert row["outcome"] is None


def test_mkt_resolution_rounds_probability(conn, monkeypatch):
    hi_uid = db.upsert_market(conn, _past_market("m-mkt-hi"))
    lo_uid = db.upsert_market(conn, _past_market("m-mkt-lo"))
    _add_calibration(conn, hi_uid)
    _add_calibration(conn, lo_uid)

    _install(
        monkeypatch,
        FakeManifoldClient(
            {
                "m-mkt-hi": {"isResolved": True, "resolution": "MKT", "resolutionProbability": 0.87},
                "m-mkt-lo": {"isResolved": True, "resolution": "MKT", "resolutionProbability": 0.12},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 2
    for uid, outcome in ((hi_uid, 1), (lo_uid, 0)):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 1
        assert m["outcome"] == outcome
        row = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
        assert row["outcome"] == outcome


def test_mkt_without_probability_deactivated_unscored(conn, monkeypatch):
    uid = db.upsert_market(conn, _past_market("m-mkt-norp"))
    _add_calibration(conn, uid)
    _install(monkeypatch, FakeManifoldClient({"m-mkt-norp": {"isResolved": True, "resolution": "MKT"}}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    m = db.get_market(conn, uid)
    assert m["resolved"] == 1
    assert m["active"] == 0
    assert m["outcome"] is None
    # never scored: the calibration row keeps outcome NULL, excluded from Brier
    row = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
    assert row["outcome"] is None
    assert db.unresolved_past_close(conn, grace_hours=6) == []


def test_cancel_leaves_no_calibration_outcome_rows(conn, monkeypatch):
    uid = db.upsert_market(conn, _past_market("m-cancel"))
    _add_calibration(conn, uid)
    client = _install(monkeypatch, FakeManifoldClient({"m-cancel": {"isResolved": True, "resolution": "CANCEL"}}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    m = db.get_market(conn, uid)
    assert m["resolved"] == 1
    assert m["active"] == 0
    assert m["outcome"] is None
    scored = conn.execute("SELECT COUNT(*) FROM calibration WHERE market_uid = ? AND outcome IS NOT NULL", (uid,)).fetchone()[0]
    assert scored == 0
    remaining = conn.execute("SELECT COUNT(*) FROM calibration WHERE market_uid = ?", (uid,)).fetchone()[0]
    assert remaining == 0
    assert db.get_brier_score(conn)["n"] == 0

    calls_before = len(client.calls)
    assert asyncio.run(resolver.resolve_pending(conn)) == 0
    assert len(client.calls) == calls_before


def test_cancel_with_probability_still_no_calibration_backfill(conn, monkeypatch):
    uid = db.upsert_market(conn, _past_market("m-cancel-rp"))
    _add_calibration(conn, uid)
    _install(monkeypatch, FakeManifoldClient({"m-cancel-rp": {"isResolved": True, "resolution": "CANCEL", "resolutionProbability": 0.9}}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    m = db.get_market(conn, uid)
    assert m["resolved"] == 1
    assert m["outcome"] == 1
    remaining = conn.execute("SELECT COUNT(*) FROM calibration WHERE market_uid = ?", (uid,)).fetchone()[0]
    assert remaining == 0


def test_settlement_error_leaves_market_pending(conn, monkeypatch):
    bad_uid = db.upsert_market(conn, _past_market("m-err"))
    good_uid = db.upsert_market(conn, _past_market("m-good"))
    _install(
        monkeypatch,
        FakeManifoldClient(
            {
                "m-err": RuntimeError("connection reset"),
                "m-good": {"isResolved": True, "resolution": "YES"},
            }
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    assert db.get_market(conn, bad_uid)["resolved"] == 0
    assert db.get_market(conn, good_uid)["outcome"] == 1


# ── live smoke ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(not os.environ.get("MANIFOLD_LIVE"), reason="live smoke; run with MANIFOLD_LIVE=1")
def test_live_smoke_30_days():
    rows = _run(ingest_manifold.fetch_horizon(30))
    assert len(rows) > 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    categories: dict[str, int] = {}
    for row in rows:
        assert row["venue"] == "manifold"
        assert row["venue_id"]
        assert row["last_price"] is None or 0.0 <= row["last_price"] <= 1.0
        assert row["end_date"] > now_iso
        categories[row["category"]] = categories.get(row["category"], 0) + 1
    top = sorted(rows, key=lambda r: r["liquidity"] or 0, reverse=True)[:3]
    report = {
        "market_count_30d": len(rows),
        "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "sample": [
            {
                "id": r["venue_id"],
                "question": r["question"],
                "probability": r["last_price"],
                "liquidity": r["liquidity"],
                "url": r["url"],
            }
            for r in top
        ],
    }
    print("\nLIVE_SMOKE " + json.dumps(report, indent=2))
