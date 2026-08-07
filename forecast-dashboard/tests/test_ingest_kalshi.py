import asyncio
import json
import os

import httpx
import pytest

import db
import ingest_kalshi


def _market(**over):
    base = {
        "ticker": "KXHIGHNY-26AUG08-B85",
        "event_ticker": "KXHIGHNY-26AUG08",
        "title": "Highest temperature in NYC on Aug 8?",
        "yes_sub_title": "84 to 85",
        "close_time": "2026-08-08T14:00:00Z",
        "yes_bid_dollars": "0.5600",
        "yes_ask_dollars": "0.5800",
        "last_price_dollars": "0.5700",
        "liquidity_dollars": "2500.0000",
        "volume_24h_fp": "1200.00",
        "volume_fp": "5400.00",
        "open_interest_fp": "900.00",
    }
    base.update(over)
    return base


def _legacy_market(**over):
    base = {
        "ticker": "LEGACY-1",
        "event_ticker": "LEGACY",
        "title": "Legacy cents payload?",
        "close_time": "2026-08-08T14:00:00Z",
        "yes_bid": 56,
        "yes_ask": 58,
        "last_price": 57,
        "liquidity": 250000,
        "volume_24h": 1200,
        "volume": 5400,
        "open_interest": 900,
    }
    base.update(over)
    return base


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(ingest_kalshi.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(ingest_kalshi, "_PAGE_PAUSE", 0)
    monkeypatch.setattr(ingest_kalshi, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(ingest_kalshi, "SUPPLEMENT_SERIES", ())


def _run(coro):
    return asyncio.run(coro)


def test_normalize_price_cents_and_floats():
    assert ingest_kalshi._normalize_price(56) == 0.56
    assert ingest_kalshi._normalize_price(0.56) == 0.56
    assert ingest_kalshi._normalize_price("56") == 0.56
    assert ingest_kalshi._normalize_price(99) == 0.99
    assert ingest_kalshi._normalize_price(0) == 0.0
    assert ingest_kalshi._normalize_price(None) is None
    assert ingest_kalshi._normalize_price("n/a") is None
    assert ingest_kalshi._normalize_price(150) is None
    assert ingest_kalshi._normalize_price(-3) is None


def test_fetch_normalizes_dollar_strings(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"markets": [_market()], "cursor": ""})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert len(rows) == 1
    row = rows[0]
    assert row["venue"] == "kalshi"
    assert row["venue_id"] == "KXHIGHNY-26AUG08-B85"
    assert row["yes_bid"] == 0.56
    assert row["yes_ask"] == 0.58
    assert row["last_price"] == 0.57
    assert row["liquidity"] == 2500.0
    assert row["volume_24h"] == 1200.0
    assert row["url"] == "https://kalshi.com/markets/kxhighny-26aug08"
    assert row["question"] == "Highest temperature in NYC on Aug 8? — 84 to 85"
    assert row["end_date"] == "2026-08-08T14:00:00Z"
    assert row["yes_token_id"] is None and row["no_token_id"] is None


def test_fetch_normalizes_legacy_cents(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"markets": [_legacy_market()], "cursor": ""})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert len(rows) == 1
    row = rows[0]
    assert row["yes_bid"] == 0.56
    assert row["yes_ask"] == 0.58
    assert row["last_price"] == 0.57
    assert row["liquidity"] == 2500.0
    assert row["volume_24h"] == 1200.0


def test_cursor_pagination_terminates(monkeypatch):
    pages = [
        {"markets": [_market(ticker="T-A"), _market(ticker="T-B")], "cursor": "page2"},
        {"markets": [_market(ticker="T-C")], "cursor": ""},
    ]
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=pages[1] if cursor == "page2" else pages[0])

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["T-A", "T-B", "T-C"]
    assert len(calls) == 2
    assert "cursor" not in calls[0]
    assert calls[1]["cursor"] == "page2"
    assert calls[0]["status"] == "open"
    assert calls[0]["limit"] == "1000"
    assert int(calls[0]["max_close_ts"]) - int(calls[0]["min_close_ts"]) == 7 * 86400


def test_missing_price_rows_still_returned(monkeypatch):
    def handler(request):
        markets = [
            _market(ticker="T-NOPRICES", yes_bid_dollars=None, yes_ask_dollars=None, last_price_dollars="0.1200"),
            _market(ticker="T-JUNK", yes_bid_dollars="junk", yes_ask_dollars="0.0000", last_price_dollars=None),
            {"title": "no ticker, dropped"},
        ]
        return httpx.Response(200, json={"markets": markets, "cursor": ""})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["T-NOPRICES", "T-JUNK"]
    assert rows[0]["yes_bid"] is None
    assert rows[0]["yes_ask"] is None
    assert rows[0]["last_price"] == 0.12
    assert rows[1]["yes_bid"] is None
    assert rows[1]["yes_ask"] == 0.0
    assert rows[1]["last_price"] is None
    base = db.compute_baseline(rows[0]["yes_bid"], rows[0]["yes_ask"], rows[0]["last_price"], rows[0]["liquidity"])
    assert base["baseline"] == 0.12
    assert base["tier"] == "stale"


def test_http_error_retries_once_then_returns_empty(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert rows == []
    assert len(calls) == 2


def test_supplement_series_fetched_and_deduped(monkeypatch):
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        series = params.get("series_ticker")
        if series == "KXHIGHNY":
            markets = [
                _market(ticker="KXHIGHNY-26AUG08-B85"),
                _market(ticker="KXHIGHNY-26AUG08-B87"),
            ]
        elif series:
            markets = []
        else:
            markets = [_market(ticker="KXHIGHNY-26AUG08-B85"), _market(ticker="T-MAIN")]
        return httpx.Response(200, json={"markets": markets, "cursor": ""})

    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(ingest_kalshi, "SUPPLEMENT_SERIES", ("KXHIGHNY", "KXFEDDECISION"))
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert [r["venue_id"] for r in rows] == ["KXHIGHNY-26AUG08-B85", "T-MAIN", "KXHIGHNY-26AUG08-B87"]
    series_calls = [c["series_ticker"] for c in calls if "series_ticker" in c]
    assert series_calls == ["KXHIGHNY", "KXFEDDECISION"]
    for c in calls:
        assert c["status"] == "open"


def test_uninformative_subtitle_not_appended(monkeypatch):
    def handler(request):
        markets = [
            _market(ticker="T-1", title="Will X happen?", yes_sub_title="Yes"),
            _market(ticker="T-2", title="Will X happen?", yes_sub_title=""),
            _market(ticker="T-3", title="Fed cuts 25 bps in Sept", yes_sub_title="fed cuts 25 bps in sept"),
        ]
        return httpx.Response(200, json={"markets": markets, "cursor": ""})

    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert rows[0]["question"] == "Will X happen?"
    assert rows[1]["question"] == "Will X happen?"
    assert rows[2]["question"] == "Fed cuts 25 bps in Sept"


@pytest.mark.skipif(not os.environ.get("KALSHI_LIVE"), reason="live smoke; run with KALSHI_LIVE=1")
def test_live_smoke_7_days():
    rows = _run(ingest_kalshi.fetch_horizon(7))
    assert len(rows) > 0
    for row in rows:
        for k in ("yes_bid", "yes_ask", "last_price"):
            assert row[k] is None or 0.0 <= row[k] <= 1.0
    quoted = [r for r in rows if r["yes_bid"] and r["yes_ask"] and (r["volume_24h"] or 0) > 0]
    quoted.sort(key=lambda r: r["volume_24h"] or 0, reverse=True)
    sample = quoted[:3]
    report = {"market_count_7d": len(rows), "sample": []}
    for r in sample:
        base = db.compute_baseline(r["yes_bid"], r["yes_ask"], r["last_price"], r["liquidity"])
        report["sample"].append(
            {
                "ticker": r["venue_id"],
                "question": r["question"],
                "yes_bid": r["yes_bid"],
                "yes_ask": r["yes_ask"],
                "last_price": r["last_price"],
                "baseline": base["baseline"],
                "tier": base["tier"],
                "url": r["url"],
            }
        )
    print("\nLIVE_SMOKE " + json.dumps(report, indent=2))
    assert len(sample) == 3
