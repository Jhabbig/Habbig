import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

import db
import ingest_options
import resolver


def _run(coro):
    return asyncio.run(coro)


def _utcnow():
    return datetime.now(timezone.utc)


def _epoch_midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _call(strike, iv=0.2, oi=500, volume=123, bid=2.0, ask=2.2):
    return {"strike": strike, "impliedVolatility": iv, "openInterest": oi, "volume": volume, "bid": bid, "ask": ask}


def _chain_result(symbol, spot, expirations, options_block):
    return {
        "optionChain": {
            "result": [
                {
                    "underlyingSymbol": symbol,
                    "expirationDates": expirations,
                    "quote": {"regularMarketPrice": spot},
                    "options": [options_block],
                }
            ],
            "error": None,
        }
    }


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def fake_client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(ingest_options.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(ingest_options, "_PAUSE", 0)
    monkeypatch.setattr(ingest_options, "_RETRY_BACKOFF", 0)
    ingest_options._throttled_until[0] = 0.0


# ── N(d2) math ───────────────────────────────────────────────────────────────


def test_prob_above_atm_hand_computed():
    # S=100, K=100, sigma=0.2, T=0.25, r=0.04:
    # d2 = (0 + (0.04 - 0.02)*0.25) / (0.2*0.5) = 0.05 -> N(0.05) = 0.5199
    p = ingest_options.prob_above(100.0, 100.0, 0.2, 0.25)
    assert abs(p - 0.5199) < 0.001


def test_prob_above_itm_otm_hand_computed():
    # K=90: d2 = (ln(10/9) + 0.005)/0.1 = 1.1036 -> N = 0.8651
    assert abs(ingest_options.prob_above(100.0, 90.0, 0.2, 0.25) - 0.8651) < 0.001
    # K=110: d2 = (ln(10/11) + 0.005)/0.1 = -0.9031 -> N = 0.1832
    assert abs(ingest_options.prob_above(100.0, 110.0, 0.2, 0.25) - 0.1832) < 0.001


def test_prob_above_deep_itm_otm_clamped():
    assert ingest_options.prob_above(100.0, 50.0, 0.2, 0.25) == 0.995
    assert ingest_options.prob_above(100.0, 200.0, 0.2, 0.25) == 0.005


def test_prob_above_invalid_inputs():
    assert ingest_options.prob_above(0.0, 100.0, 0.2, 0.25) is None
    assert ingest_options.prob_above(100.0, 100.0, 0.0, 0.25) is None
    assert ingest_options.prob_above(100.0, 100.0, 0.2, 0.0) is None


# ── venue_id round trip ──────────────────────────────────────────────────────


def test_venue_id_round_trip():
    d = date(2026, 8, 14)
    for symbol, strike in (("SPY", 630.0), ("SPY", 632.5), ("BRK-B", 450.0), ("AAPL", 227.5)):
        vid = ingest_options.make_venue_id(symbol, d, strike)
        assert ingest_options.parse_venue_id(vid) == (symbol, d, strike)
    assert ingest_options.make_venue_id("SPY", d, 630.0) == "SPY-20260814-C630"
    assert ingest_options.make_venue_id("SPY", d, 632.5) == "SPY-20260814-C632.5"


def test_parse_venue_id_rejects_malformed():
    for bad in ("", "junk", "SPY-20260814", "SPY-20260814-P630", "SPY-notadate-C630", "SPY-20260814-Cabc", "-20260814-C630"):
        assert ingest_options.parse_venue_id(bad) is None


# ── strike selection ─────────────────────────────────────────────────────────


def test_select_strikes_nearest_plus_round_numbers():
    strikes = [80.0, 85.0, 90.0, 92.5, 95.0, 97.5, 100.0, 102.5, 105.0, 107.5, 110.0, 120.0]
    chosen = ingest_options._select_strikes(strikes, 100.0)
    assert 100.0 == chosen[0]
    for k in (97.5, 102.5, 95.0, 105.0):  # 5 nearest
        assert k in chosen
    # round-number strikes (step 5) within +/-8% of spot
    assert 95.0 in chosen and 105.0 in chosen
    assert 120.0 not in chosen and 80.0 not in chosen
    assert len(chosen) <= ingest_options.MAX_STRIKES_PER_EXPIRY


def test_round_robin_cap_trims_evenly():
    buckets = [[f"a{i}" for i in range(10)], [f"b{i}" for i in range(10)], [f"c{i}" for i in range(2)]]
    out = ingest_options._round_robin(buckets, 8)
    assert len(out) == 8
    assert out[:3] == ["a0", "b0", "c0"]
    assert sum(1 for x in out if x.startswith("a")) == 3
    assert sum(1 for x in out if x.startswith("b")) == 3


# ── chain parsing (fixture payload) ──────────────────────────────────────────


def test_fetch_horizon_parses_fixture_chain(monkeypatch):
    expiry = (_utcnow() + timedelta(days=3)).date()
    epoch = _epoch_midnight(expiry)
    calls = [
        _call(90.0, iv=0.25, oi=100, volume=10, bid=10.0, ask=10.4),
        _call(95.0, iv=0.22),
        _call(100.0, iv=0.2, oi=500, volume=123, bid=2.0, ask=2.2),
        _call(105.0, iv=0.21),
        _call(110.0, iv=1e-5, oi=50, volume=None, bid=0, ask=0),  # missing IV -> nearest-strike fallback
    ]
    payload = _chain_result("TST", 100.0, [epoch], {"expirationDate": epoch, "calls": calls, "puts": []})

    def handler(request):
        assert "TST" in request.url.path
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_options.fetch_horizon(7))

    assert len(rows) == 6
    by_id = {r["venue_id"]: r for r in rows}
    expected_ids = {"TST-%s-C%d" % (expiry.strftime("%Y%m%d"), k) for k in (90, 95, 100, 105, 110)}
    expected_ids.add(f"TST-{expiry.strftime('%Y%m%d')}-D100")
    assert set(by_id) == expected_ids

    # No previousClose in the fixture quote -> the direction anchor falls
    # back to spot; the D row leads the bucket.
    direction = by_id[f"TST-{expiry.strftime('%Y%m%d')}-D100"]
    assert rows[0] is direction
    assert direction["question"].startswith(f"TST ends UP vs prev close ($100) on {expiry.isoformat()}")
    assert "expected move ±" in direction["question"]
    assert direction["volume_24h"] is None

    atm = by_id[f"TST-{expiry.strftime('%Y%m%d')}-C100"]
    assert atm["venue"] == "options"
    assert atm["event_ticker"] == f"TST-{expiry.strftime('%Y%m%d')}"
    assert atm["question"] == f"TST closes at or above $100 on {expiry.isoformat()} (options-implied)"
    assert atm["category"] == "finance"
    assert atm["url"] == "https://finance.yahoo.com/quote/TST/options"
    assert atm["end_date"] == f"{expiry.isoformat()}T21:00:00Z"
    assert atm["yes_bid"] is None and atm["yes_ask"] is None
    assert atm["yes_token_id"] is None and atm["no_token_id"] is None
    assert atm["liquidity"] == 500 * 100 * 2.1
    assert atm["volume_24h"] == 123.0

    end_dt = datetime(expiry.year, expiry.month, expiry.day, 21, tzinfo=timezone.utc)
    t_years = (end_dt - _utcnow()).total_seconds() / (365.0 * 86400.0)
    expected = ingest_options.prob_above(100.0, 100.0, 0.2, t_years)
    assert abs(atm["last_price"] - expected) < 0.005

    # probabilities decrease with strike; deep wings clamp
    probs = [by_id[f"TST-{expiry.strftime('%Y%m%d')}-C{k}"]["last_price"] for k in (90, 95, 100, 105, 110)]
    assert probs == sorted(probs, reverse=True)
    assert probs[0] == 0.995
    assert probs[-1] == 0.005  # present at all -> IV fallback worked

    # OI-only liquidity when no usable premium
    assert by_id[f"TST-{expiry.strftime('%Y%m%d')}-C110"]["liquidity"] == 50.0

    base = db.compute_baseline(atm["yes_bid"], atm["yes_ask"], atm["last_price"], atm["liquidity"])
    assert base["baseline"] == atm["last_price"]
    assert base["tier"] == "stale"


def test_fetch_horizon_multiple_expiries_and_date_param(monkeypatch):
    e1 = _epoch_midnight((_utcnow() + timedelta(days=2)).date())
    e2 = _epoch_midnight((_utcnow() + timedelta(days=5)).date())
    e3 = _epoch_midnight((_utcnow() + timedelta(days=30)).date())
    date_params = []

    def handler(request):
        d = request.url.params.get("date")
        if d is not None:
            date_params.append(int(d))
            return httpx.Response(200, json=_chain_result("TST", 100.0, [e1, e2, e3], {"expirationDate": int(d), "calls": [_call(100.0)], "puts": []}))
        return httpx.Response(200, json=_chain_result("TST", 100.0, [e1, e2, e3], {"expirationDate": e1, "calls": [_call(100.0)], "puts": []}))

    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_options.fetch_horizon(7))

    tickers = {r["event_ticker"] for r in rows}
    d1 = datetime.fromtimestamp(e1, tz=timezone.utc).strftime("%Y%m%d")
    d2 = datetime.fromtimestamp(e2, tz=timezone.utc).strftime("%Y%m%d")
    assert tickers == {f"TST-{d1}", f"TST-{d2}"}  # e3 beyond horizon excluded
    assert date_params == [e2]  # only the non-default expiry needed ?date=


def test_fetch_horizon_first_expiry_beyond_horizon_when_none_inside(monkeypatch):
    e_far = _epoch_midnight((_utcnow() + timedelta(days=20)).date())

    def handler(request):
        return httpx.Response(200, json=_chain_result("TST", 100.0, [e_far], {"expirationDate": e_far, "calls": [_call(100.0)], "puts": []}))

    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_options.fetch_horizon(7))
    assert len(rows) == 2  # ladder row + direction (D) row
    assert sum(1 for r in rows if ingest_options.is_direction(r["venue_id"])) == 1
    d = datetime.fromtimestamp(e_far, tz=timezone.utc).strftime("%Y%m%d")
    assert rows[0]["event_ticker"] == f"TST-{d}"


def test_failing_symbol_skipped_silently(monkeypatch):
    expiry = (_utcnow() + timedelta(days=3)).date()
    epoch = _epoch_midnight(expiry)

    def handler(request):
        if "BAD" in request.url.path:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_chain_result("TST", 100.0, [epoch], {"expirationDate": epoch, "calls": [_call(100.0)], "puts": []}))

    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "BAD,TST")
    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_options.fetch_horizon(7))
    assert {r["venue_id"].split("-")[0] for r in rows} == {"TST"}
    assert len(rows) == 2  # ladder row + direction (D) row


def test_crumb_dance_on_401(monkeypatch):
    expiry = (_utcnow() + timedelta(days=3)).date()
    epoch = _epoch_midnight(expiry)
    payload = _chain_result("TST", 100.0, [epoch], {"expirationDate": epoch, "calls": [_call(100.0)], "puts": []})

    def handler(request):
        host = request.url.host
        if host == "fc.yahoo.com":
            return httpx.Response(404, text="")
        if "getcrumb" in request.url.path:
            return httpx.Response(200, text="crumb123")
        if request.url.params.get("crumb") == "crumb123":
            return httpx.Response(200, json=payload)
        return httpx.Response(401, json={"error": "unauthorized"})

    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    _patch_transport(monkeypatch, handler)
    rows = _run(ingest_options.fetch_horizon(7))
    assert len(rows) == 2  # ladder row + direction (D) row
    assert sum(1 for r in rows if ingest_options.is_direction(r["venue_id"])) == 1
    assert rows[0]["venue_id"] == f"TST-{expiry.strftime('%Y%m%d')}-D100"
    assert rows[1]["venue_id"] == f"TST-{expiry.strftime('%Y%m%d')}-C100"


# ── resolver options branch ──────────────────────────────────────────────────


def _chart_payload(bars):
    """bars: list of (datetime UTC at session open, close|None)."""
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [int(dt.timestamp()) for dt, _ in bars],
                    "indicators": {"quote": [{"close": [c for _, c in bars]}]},
                }
            ],
            "error": None,
        }
    }


class FakeChartClient:
    def __init__(self, payload_by_symbol):
        self.payload_by_symbol = payload_by_symbol
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        symbol = url.rsplit("/", 1)[1]
        payload = self.payload_by_symbol.get(symbol)
        if payload is None:
            return _Resp({"error": "not found"}, 404)
        return _Resp(payload)


class _Resp:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _options_market(venue_id, end_date):
    return {
        "venue": "options",
        "venue_id": venue_id,
        "question": f"{venue_id} (options-implied)",
        "category": "finance",
        "url": "https://finance.yahoo.com/quote/TST/options",
        "end_date": end_date,
    }


def _past_expiry(days_ago=7):
    return (_utcnow() - timedelta(days=days_ago)).date()


def test_resolver_resolves_yes_no_and_boundary(conn, monkeypatch):
    expiry = _past_expiry()
    ds = expiry.strftime("%Y%m%d")
    end = f"{expiry.isoformat()}T21:00:00Z"
    uid_yes = db.upsert_market(conn, _options_market(f"TSTA-{ds}-C100", end))
    uid_no = db.upsert_market(conn, _options_market(f"TSTB-{ds}-C100", end))
    uid_eq = db.upsert_market(conn, _options_market(f"TSTC-{ds}-C100", end))

    open_dt = datetime(expiry.year, expiry.month, expiry.day, 13, 30, tzinfo=timezone.utc)
    prev = open_dt - timedelta(days=1)
    client = FakeChartClient(
        {
            "TSTA": _chart_payload([(prev, 99.0), (open_dt, 105.0)]),
            "TSTB": _chart_payload([(prev, 101.0), (open_dt, 95.0)]),
            "TSTC": _chart_payload([(open_dt, 100.0)]),
        }
    )
    monkeypatch.setattr(resolver, "_make_client", lambda: client)

    n = _run(resolver.resolve_pending(conn))
    assert n == 3
    assert db.get_market(conn, uid_yes)["outcome"] == 1
    assert db.get_market(conn, uid_no)["outcome"] == 0
    assert db.get_market(conn, uid_eq)["outcome"] == 1  # close == strike resolves yes

    url, params = client.calls[0]
    assert url == resolver.YAHOO_CHART_URL.format(symbol="TSTA")
    assert params["interval"] == "1d"
    assert params["period1"] <= _epoch_midnight(expiry) <= params["period2"]


def test_resolver_leaves_pending_when_close_missing(conn, monkeypatch):
    expiry = _past_expiry()
    ds = expiry.strftime("%Y%m%d")
    end = f"{expiry.isoformat()}T21:00:00Z"
    uid_gap = db.upsert_market(conn, _options_market(f"TSTA-{ds}-C100", end))
    uid_null = db.upsert_market(conn, _options_market(f"TSTB-{ds}-C100", end))
    uid_404 = db.upsert_market(conn, _options_market(f"TSTX-{ds}-C100", end))

    open_dt = datetime(expiry.year, expiry.month, expiry.day, 13, 30, tzinfo=timezone.utc)
    client = FakeChartClient(
        {
            "TSTA": _chart_payload([(open_dt - timedelta(days=1), 99.0)]),  # no bar for expiry date
            "TSTB": _chart_payload([(open_dt, None)]),  # bar exists, close null
        }
    )
    monkeypatch.setattr(resolver, "_make_client", lambda: client)

    n = _run(resolver.resolve_pending(conn))
    assert n == 0
    for uid in (uid_gap, uid_null, uid_404):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 0
        assert m["outcome"] is None


def test_resolver_options_grace_guard_no_fetch(monkeypatch):
    today = _utcnow().date()
    client = FakeChartClient({})
    out = _run(resolver._options_outcome(client, f"TST-{today.strftime('%Y%m%d')}-C100"))
    assert out is None
    assert client.calls == []


def test_resolver_options_malformed_venue_id():
    client = FakeChartClient({})
    assert _run(resolver._options_outcome(client, "not-a-real-id")) is None
    assert client.calls == []


# ── live smoke (opt-in) ──────────────────────────────────────────────────────


@pytest.mark.skipif(not os.environ.get("OPTIONS_LIVE"), reason="live smoke; run with OPTIONS_LIVE=1")
def test_live_smoke_spy(monkeypatch):
    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "SPY")
    rows = _run(ingest_options.fetch_horizon(7))
    assert 0 < len(rows) <= ingest_options.MAX_ROWS
    for r in rows:
        assert 0.005 <= r["last_price"] <= 0.995
        assert ingest_options.parse_venue_id(r["venue_id"]) is not None
    report = {
        "row_count": len(rows),
        "sample": [{"venue_id": r["venue_id"], "prob": r["last_price"], "question": r["question"]} for r in rows[:3]],
    }
    print("\nLIVE_SMOKE " + json.dumps(report, indent=2))


def test_direction_row_anchored_to_previous_close(monkeypatch):
    expiry = (_utcnow() + timedelta(days=3)).date()
    epoch = _epoch_midnight(expiry)
    calls = [_call(100.0, iv=0.2, oi=500, volume=9, bid=2.0, ask=2.2), _call(105.0, iv=0.21)]
    payload = _chain_result("TST", 101.37, [epoch], {"expirationDate": epoch, "calls": calls, "puts": []})
    # Anchor comes from previousClose, NOT the live spot — a moving spot must
    # not mint a new market id every ingest cycle.
    payload["optionChain"]["result"][0]["quote"]["regularMarketPreviousClose"] = 100.84
    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    rows = _run(ingest_options.fetch_horizon(7))

    vid = f"TST-{expiry.strftime('%Y%m%d')}-D100.84"
    direction = next(r for r in rows if r["venue_id"] == vid)
    assert direction["question"].startswith(f"TST ends UP vs prev close ($100.84) on {expiry.isoformat()}")
    assert ingest_options.is_direction(vid)
    assert ingest_options.parse_venue_id(vid) == ("TST", expiry, 100.84)
    # P(S_T > prev close) with spot slightly above the anchor: a bit over 0.5
    assert 0.48 < direction["last_price"] < 0.62
    # ladder rows still present alongside, and none are direction-kind
    assert any(r["venue_id"].endswith("-C100") for r in rows)
    assert sum(1 for r in rows if ingest_options.is_direction(r["venue_id"])) == 1


def test_direction_row_resolves_strictly_greater(conn, monkeypatch):
    expiry = (_utcnow() - timedelta(days=2)).date()
    vid = f"TST-{expiry.strftime('%Y%m%d')}-D100.84"

    def chart(close):
        ts = int(datetime(expiry.year, expiry.month, expiry.day, 14, tzinfo=timezone.utc).timestamp())
        return {"chart": {"result": [{"timestamp": [ts], "indicators": {"quote": [{"close": [close]}]}}]}}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, close):
            self._close = close
        async def get(self, *a, **kw):
            return FakeResp(chart(self._close))

    # flat close == anchor -> NOT up (strict); a cent above -> up
    assert _run(resolver._options_outcome(FakeClient(100.84), vid)) == 0
    assert _run(resolver._options_outcome(FakeClient(100.85), vid)) == 1
    # ladder kind keeps >= semantics at the same boundary
    cvid = f"TST-{expiry.strftime('%Y%m%d')}-C100.84"
    assert _run(resolver._options_outcome(FakeClient(100.84), cvid)) == 1


# ── CBOE fallback ────────────────────────────────────────────────────────────


def test_parse_occ():
    assert ingest_options.parse_occ("AAPL260814C00110000", "AAPL") == (date(2026, 8, 14), "C", 110.0)
    assert ingest_options.parse_occ("SPY260813P00082500", "SPY") == (date(2026, 8, 13), "P", 82.5)
    assert ingest_options.parse_occ("AAPL1260814C00110000", "AAPL") is None  # adjusted root
    assert ingest_options.parse_occ("AAPL260814C00110000", "SPY") is None  # wrong symbol
    assert ingest_options.parse_occ("AAPL260814X00110000", "AAPL") is None  # bad kind
    assert ingest_options.parse_occ("AAPL26081C00110000", "AAPL") is None  # short date
    assert ingest_options.parse_occ("", "AAPL") is None


def _cboe_option(symbol, expiry, kind, strike, iv=0.2, oi=500, volume=100, bid=2.0, ask=2.2):
    occ = f"{symbol}{expiry.strftime('%y%m%d')}{kind}{int(round(strike * 1000)):08d}"
    return {"option": occ, "iv": iv, "open_interest": oi, "volume": volume, "bid": bid, "ask": ask}


def _cboe_payload(symbol, spot, prev_close, options):
    return {"data": {"symbol": symbol, "current_price": spot, "prev_day_close": prev_close, "options": options}}


def test_cboe_fallback_when_yahoo_throttled(monkeypatch):
    expiry = (_utcnow() + timedelta(days=3)).date()
    far = (_utcnow() + timedelta(days=200)).date()
    opts = [
        _cboe_option("TST", expiry, "C", 100.0),
        _cboe_option("TST", expiry, "C", 105.0),
        _cboe_option("TST", expiry, "P", 95.0),  # puts ignored
        _cboe_option("TST", far, "C", 100.0),  # beyond horizon
    ]

    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "cdn.cboe.com" in str(request.url):
            return httpx.Response(200, json=_cboe_payload("TST", 102.0, 101.5, opts))
        return httpx.Response(429)

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    import time as _time

    ingest_options._throttled_until[0] = _time.time() + 3600  # Yahoo banned

    rows = _run(ingest_options.fetch_horizon(7))
    assert rows, "CBOE fallback produced no rows"
    assert all("cdn.cboe.com" in u for u in calls), "Yahoo was contacted while throttled"
    assert ingest_options.is_direction(rows[0]["venue_id"])  # direction row leads
    assert rows[0]["venue_id"] == f"TST-{expiry.strftime('%Y%m%d')}-D101.5"  # anchored at prev close
    ladder_ids = {r["venue_id"] for r in rows[1:]}
    assert f"TST-{expiry.strftime('%Y%m%d')}-C100" in ladder_ids
    assert f"TST-{expiry.strftime('%Y%m%d')}-C105" in ladder_ids
    assert not any(far.strftime("%Y%m%d") in vid for vid in ladder_ids)  # horizon respected
    for r in rows:
        assert 0.0 < r["last_price"] < 1.0
    ingest_options._throttled_until[0] = 0.0


def test_cboe_fallback_http_error_skips_symbol(monkeypatch):
    def handler(request):
        return httpx.Response(503)

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FORECAST_EQUITY_WATCHLIST", "TST")
    import time as _time

    ingest_options._throttled_until[0] = _time.time() + 3600
    assert _run(ingest_options.fetch_horizon(7)) == []
    ingest_options._throttled_until[0] = 0.0


class FakeCboeResolverClient:
    """Yahoo chart 429s; CBOE serves the underlying quote."""

    def __init__(self, close, last_trade_time):
        self.close = close
        self.last_trade_time = last_trade_time
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append(url)
        if "cboe.com" in url:
            return _Resp({"data": {"close": self.close, "last_trade_time": self.last_trade_time}})
        return _Resp({}, 429)


def test_resolver_falls_back_to_cboe_close(conn, monkeypatch):
    expiry = _past_expiry(days_ago=1)
    ds = expiry.strftime("%Y%m%d")
    end = f"{expiry.isoformat()}T21:00:00Z"
    uid = db.upsert_market(conn, _options_market(f"TSTA-{ds}-C100", end))

    client = FakeCboeResolverClient(close=105.0, last_trade_time=f"{expiry.isoformat()}T16:00:00")
    monkeypatch.setattr(resolver, "_make_client", lambda: client)
    n = _run(resolver.resolve_pending(conn))
    assert n == 1
    assert db.get_market(conn, uid)["outcome"] == 1


def test_resolver_cboe_stale_trade_date_stays_pending(conn, monkeypatch):
    expiry = _past_expiry(days_ago=3)
    ds = expiry.strftime("%Y%m%d")
    end = f"{expiry.isoformat()}T21:00:00Z"
    uid = db.upsert_market(conn, _options_market(f"TSTA-{ds}-C100", end))

    # CBOE already shows a newer session -> its close is the wrong day's.
    newer = (_utcnow() - timedelta(days=1)).date()
    client = FakeCboeResolverClient(close=105.0, last_trade_time=f"{newer.isoformat()}T16:00:00")
    monkeypatch.setattr(resolver, "_make_client", lambda: client)
    n = _run(resolver.resolve_pending(conn))
    assert n == 0
    assert db.get_market(conn, uid)["resolved"] == 0
