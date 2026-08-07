import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

import ingest_polymarket as ipm


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gamma_market(condition_id, end_dt, **overrides):
    m = {
        "id": "2961613",
        "question": f"Question {condition_id}?",
        "conditionId": condition_id,
        "slug": f"market-slug-{condition_id}",
        "endDate": _iso(end_dt),
        "liquidity": "10045.0957",
        "liquidityNum": 10045.0957,
        "volume24hr": 500,
        "bestBid": 0.45,
        "bestAsk": 0.47,
        "lastTradePrice": 0.46,
        "clobTokenIds": json.dumps([f"yes-{condition_id}", f"no-{condition_id}"]),
        "events": [
            {
                "id": "143568",
                "ticker": f"evt-{condition_id}",
                "slug": f"event-slug-{condition_id}",
                "title": f"Event {condition_id}",
                "tags": [{"label": "Politics"}],
            }
        ],
    }
    m.update(overrides)
    return m


class StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _install(monkeypatch, pages, books=None):
    calls = []

    async def fake_get(self, url, params=None, **kwargs):
        params = dict(params or {})
        calls.append((url, params))
        if url == ipm.GAMMA_MARKETS_URL:
            idx = int(params.get("offset", 0)) // ipm.PAGE_LIMIT
            return StubResponse(pages[idx] if idx < len(pages) else [])
        if url == ipm.CLOB_BOOK_URL:
            token = params.get("token_id")
            if books is not None and token in books:
                return StubResponse(books[token])
            return StubResponse({}, status_code=404)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return calls


def test_horizon_filtering_and_pagination_stop(monkeypatch):
    now = datetime.now(timezone.utc)
    page = [
        _gamma_market("0xa", now + timedelta(days=1)),
        _gamma_market("0xstale", now - timedelta(hours=2)),
        _gamma_market("0xb", now + timedelta(days=3)),
        _gamma_market("0xbeyond", now + timedelta(days=10)),
        _gamma_market("0xnever", now + timedelta(days=20)),
    ]
    calls = _install(monkeypatch, [page, [_gamma_market("0xnextpage", now + timedelta(days=30))]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert [r["venue_id"] for r in rows] == ["0xa", "0xb"]
    gamma_calls = [c for c in calls if c[0] == ipm.GAMMA_MARKETS_URL]
    assert len(gamma_calls) == 1
    for r in rows:
        assert r["venue"] == "polymarket"
        assert r["url"] == f"https://polymarket.com/event/event-slug-{r['venue_id']}"
        assert r["event_ticker"] == f"evt-{r['venue_id']}"
        assert r["category"] == "politics"
        end = datetime.fromisoformat(r["end_date"].replace("Z", "+00:00"))
        assert now <= end <= now + timedelta(days=7)


def test_pagination_advances_offset(monkeypatch):
    monkeypatch.setattr(ipm, "PAGE_LIMIT", 2)
    now = datetime.now(timezone.utc)
    pages = [
        [_gamma_market("0x1", now + timedelta(days=1)), _gamma_market("0x2", now + timedelta(days=2))],
        [_gamma_market("0x3", now + timedelta(days=3))],
    ]
    calls = _install(monkeypatch, pages)

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert [r["venue_id"] for r in rows] == ["0x1", "0x2", "0x3"]
    gamma_calls = [c for c in calls if c[0] == ipm.GAMMA_MARKETS_URL]
    assert [c[1]["offset"] for c in gamma_calls] == [0, 2]


def test_string_prices_coerced_to_floats(monkeypatch):
    now = datetime.now(timezone.utc)
    m = _gamma_market(
        "0xstr",
        now + timedelta(days=2),
        bestBid="0.45",
        bestAsk="0.47",
        lastTradePrice="0.46",
        liquidityNum=None,
        liquidity="1234.5",
        volume24hr="99.5",
    )
    _install(monkeypatch, [[m]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert len(rows) == 1
    r = rows[0]
    assert r["yes_bid"] == 0.45
    assert r["yes_ask"] == 0.47
    assert r["last_price"] == 0.46
    assert r["liquidity"] == 1234.5
    assert r["volume_24h"] == 99.5


def test_missing_bid_ask_and_garbage_values(monkeypatch):
    now = datetime.now(timezone.utc)
    m = _gamma_market("0xmissing", now + timedelta(days=2), lastTradePrice="not-a-number")
    del m["bestBid"]
    del m["bestAsk"]
    _install(monkeypatch, [[m]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert len(rows) == 1
    r = rows[0]
    assert r["yes_bid"] is None
    assert r["yes_ask"] is None
    assert r["last_price"] is None
    assert r["question"] == "Question 0xmissing?"


def test_clob_token_ids_parsing(monkeypatch):
    now = datetime.now(timezone.utc)
    good = _gamma_market("0xg", now + timedelta(days=1))
    malformed = _gamma_market("0xbadjson", now + timedelta(days=2), clobTokenIds="not json [")
    absent = _gamma_market("0xnone", now + timedelta(days=3))
    del absent["clobTokenIds"]
    _install(monkeypatch, [[good, malformed, absent]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    by_id = {r["venue_id"]: r for r in rows}
    assert by_id["0xg"]["yes_token_id"] == "yes-0xg"
    assert by_id["0xg"]["no_token_id"] == "no-0xg"
    assert by_id["0xbadjson"]["yes_token_id"] is None
    assert by_id["0xbadjson"]["no_token_id"] is None
    assert by_id["0xnone"]["yes_token_id"] is None
    assert by_id["0xnone"]["no_token_id"] is None


def test_clob_book_refines_top_liquidity_bid_ask(monkeypatch):
    monkeypatch.setattr(ipm, "CLOB_REFINE_TOP_N", 1)
    now = datetime.now(timezone.utc)
    rich = _gamma_market("0xrich", now + timedelta(days=1), liquidityNum=50000.0)
    poor = _gamma_market("0xpoor", now + timedelta(days=2), liquidityNum=10.0)
    books = {
        "yes-0xrich": {
            "bids": [{"price": "0.44", "size": "100"}, {"price": "0.46", "size": "50"}],
            "asks": [{"price": "0.50", "size": "80"}, {"price": "0.48", "size": "20"}],
        }
    }
    _install(monkeypatch, [[rich, poor]], books=books)

    rows = asyncio.run(ipm.fetch_horizon(7))

    by_id = {r["venue_id"]: r for r in rows}
    assert by_id["0xrich"]["yes_bid"] == 0.46
    assert by_id["0xrich"]["yes_ask"] == 0.48
    assert by_id["0xpoor"]["yes_bid"] == 0.45
    assert by_id["0xpoor"]["yes_ask"] == 0.47


def test_clob_book_failure_keeps_gamma_prices(monkeypatch):
    now = datetime.now(timezone.utc)
    m = _gamma_market("0xnobook", now + timedelta(days=1))
    _install(monkeypatch, [[m]], books={})

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert rows[0]["yes_bid"] == 0.45
    assert rows[0]["yes_ask"] == 0.47


def test_network_failure_never_raises_and_retries_once(monkeypatch):
    calls = []

    async def fake_get(self, url, params=None, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert rows == []
    assert calls == [ipm.GAMMA_MARKETS_URL, ipm.GAMMA_MARKETS_URL]


def test_offset_cap_advances_end_date_cursor(monkeypatch):
    monkeypatch.setattr(ipm, "PAGE_LIMIT", 2)
    now = datetime.now(timezone.utc)
    d2 = now + timedelta(days=2)
    m1 = _gamma_market("0x1", now + timedelta(days=1))
    m2 = _gamma_market("0x2", d2)
    m3 = _gamma_market("0x3", now + timedelta(days=3))
    calls = []

    async def fake_get(self, url, params=None, **kwargs):
        params = dict(params or {})
        calls.append((url, params))
        if url == ipm.CLOB_BOOK_URL:
            return StubResponse({}, status_code=404)
        offset = int(params["offset"])
        if params["end_date_min"] == _iso(d2):
            return StubResponse([m2, m3] if offset == 0 else [])
        if offset == 0:
            return StubResponse([m1, m2])
        return StubResponse({"error": "offset too large"}, status_code=422)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert [r["venue_id"] for r in rows] == ["0x1", "0x2", "0x3"]
    gamma = [p for u, p in calls if u == ipm.GAMMA_MARKETS_URL]
    assert len(gamma) == 4
    assert gamma[2]["end_date_min"] == _iso(d2)
    assert gamma[2]["offset"] == 0


def test_liquid_supplement_after_page_budget_exhausted(monkeypatch):
    monkeypatch.setattr(ipm, "PAGE_LIMIT", 2)
    monkeypatch.setattr(ipm, "MAX_PAGES", 1)
    now = datetime.now(timezone.utc)
    d2 = now + timedelta(days=2)
    m1 = _gamma_market("0x1", now + timedelta(days=1))
    m2 = _gamma_market("0x2", d2)
    m3 = _gamma_market("0x3", now + timedelta(days=5), liquidityNum=50000.0)
    calls = []

    async def fake_get(self, url, params=None, **kwargs):
        params = dict(params or {})
        calls.append((url, params))
        if url == ipm.CLOB_BOOK_URL:
            return StubResponse({}, status_code=404)
        if "liquidity_num_min" in params:
            assert params["end_date_min"] == _iso(d2)
            return StubResponse([m3])
        return StubResponse([m1, m2])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert [r["venue_id"] for r in rows] == ["0x1", "0x2", "0x3"]
    gamma = [p for u, p in calls if u == ipm.GAMMA_MARKETS_URL]
    assert sum(1 for p in gamma if "liquidity_num_min" in p) == 1


def test_category_canonical_mapping(monkeypatch):
    now = datetime.now(timezone.utc)
    fed = _gamma_market("0xfed", now + timedelta(days=1), tags=[{"label": "fomc"}, {"label": "Economic Policy"}, {"label": "Politics"}])
    weird = _gamma_market("0xweird", now + timedelta(days=2), tags=[{"label": "Recurring"}, {"label": "Strait of Hormuz"}])
    bare = _gamma_market("0xbare", now + timedelta(days=3))
    catfield = _gamma_market("0xcat", now + timedelta(days=4), category="Weather")
    for m in (fed, weird, bare, catfield):
        m["events"][0]["tags"] = []
    _install(monkeypatch, [[fed, weird, bare, catfield]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    by = {r["venue_id"]: r["category"] for r in rows}
    assert by["0xfed"] == "fed"
    assert by["0xweird"] == "strait of hormuz"
    assert by["0xbare"] is None
    assert by["0xcat"] == "weather"


def test_category_shared_vocabulary_across_venues(monkeypatch):
    """Same concepts land on the same buckets the kalshi/manifold/metaculus
    maps emit: economy->economics, crypto->finance — via the category field
    and via tags alike."""
    now = datetime.now(timezone.utc)
    econ_field = _gamma_market("0xecon", now + timedelta(days=1), category="Economy")
    crypto_field = _gamma_market("0xbtc", now + timedelta(days=2), category="Crypto")
    econ_tags = _gamma_market("0xtags", now + timedelta(days=3), tags=[{"label": "Macro Indicators"}])
    crypto_tags = _gamma_market("0xeth", now + timedelta(days=4), tags=[{"label": "Ethereum"}])
    for m in (econ_field, crypto_field, econ_tags, crypto_tags):
        m["events"][0]["tags"] = []
    _install(monkeypatch, [[econ_field, crypto_field, econ_tags, crypto_tags]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    by = {r["venue_id"]: r["category"] for r in rows}
    assert by["0xecon"] == "economics"
    assert by["0xbtc"] == "finance"
    assert by["0xtags"] == "economics"
    assert by["0xeth"] == "finance"


def test_missing_condition_id_row_skipped(monkeypatch):
    now = datetime.now(timezone.utc)
    m = _gamma_market("0xok", now + timedelta(days=1))
    broken = _gamma_market("", now + timedelta(days=1))
    _install(monkeypatch, [[broken, m, "not-a-dict"]])

    rows = asyncio.run(ipm.fetch_horizon(7))

    assert [r["venue_id"] for r in rows] == ["0xok"]
