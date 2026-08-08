"""Offline tests for headline ingestion — httpx is monkeypatched, no network."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import db
import news


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _end(days=2):
    return _iso(datetime.now(timezone.utc) + timedelta(days=days))


def _market(venue_id="0xa", question="Will the Senate pass the bill this week?", **kw):
    m = {
        "uid": f"polymarket:{venue_id}",
        "venue": "polymarket",
        "venue_id": venue_id,
        "question": question,
        "end_date": _end(2),
        "liquidity": 5000.0,
    }
    m.update(kw)
    return m


def _rss(items):
    body = "".join(
        f'<item><title>{title}</title><link>{link}</link><pubDate>Fri, 07 Aug 2026 10:00:00 GMT</pubDate><source url="https://src.example">{source}</source></item>' for title, link, source in items
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>results</title>{body}</channel></rss>'


SIX_ITEMS = _rss(
    [
        ("Senate schedules final vote on the bill", "https://example.com/a1", "The Paper"),
        ("Bill clears &lt;b&gt;procedural&lt;/b&gt; hurdle", "https://example.com/a2", "The Wire"),
        ("Whip count tightens ahead of vote", "https://example.com/a3", "The Post"),
        ("Amendment fight stalls floor schedule", "https://example.com/a4", "The Times"),
        ("Leadership eyes weekend session", "https://example.com/a5", "The Herald"),
        ("Sixth headline beyond the per-event cap", "https://example.com/a6", "The Extra"),
    ]
)


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_http(monkeypatch, responder):
    """Patch news.httpx.AsyncClient; responder(url) -> _Resp or raises.

    Returns the list of requested URLs.
    """
    urls = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            urls.append(url)
            return responder(url)

    monkeypatch.setattr(news.httpx, "AsyncClient", _Client)
    return urls


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(news, "PAUSE_SECONDS", 0)
    monkeypatch.delenv("FORECAST_NEWS_MAX_EVENTS", raising=False)


# ── parsing + storage ────────────────────────────────────────────────────────


def test_rss_fixture_parses_to_stored_rows(conn, monkeypatch):
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    n = asyncio.run(news.refresh(conn, [_market()]))
    assert n == 5
    assert len(urls) == 1
    # Query is the question's content words, urlencoded.
    assert "Senate" in urls[0] and "pas" in urls[0]
    assert "hl=en-US" in urls[0] and "ceid=US%3Aen" in urls[0]
    rows = db.news_for(conn, "polymarket:0xa", limit=10)
    assert len(rows) == 5
    by_url = {r["url"]: r for r in rows}
    assert "https://example.com/a6" not in by_url
    first = by_url["https://example.com/a1"]
    assert first["title"] == "Senate schedules final vote on the bill"
    assert first["source"] == "The Paper"
    assert first["published"] == "Fri, 07 Aug 2026 10:00:00 GMT"
    # Markup inside titles is stripped — stored as plain text.
    assert by_url["https://example.com/a2"]["title"] == "Bill clears procedural hurdle"


def test_dedupe_on_second_pass(conn, monkeypatch):
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    assert asyncio.run(news.refresh(conn, [_market()])) == 5
    # Age the stored rows past the 12h freshness skip so the second pass fetches again.
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=13))
    conn.execute("UPDATE news SET ts = ?", (stale,))
    conn.commit()
    assert asyncio.run(news.refresh(conn, [_market()])) == 0
    assert len(urls) == 2
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 5


@pytest.mark.parametrize(
    "payload",
    [
        '<?xml version="1.0"?><!DOCTYPE rss [<!ELEMENT rss ANY>]><rss><channel><item><title>t</title><link>https://example.com/x</link></item></channel></rss>',
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title><link>https://example.com/x</link></item></channel></rss>',
    ],
)
def test_doctype_and_entity_payloads_rejected(conn, monkeypatch, payload):
    _install_http(monkeypatch, lambda url: _Resp(payload))
    assert asyncio.run(news.refresh(conn, [_market()])) == 0
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0


def test_malformed_xml_skipped_without_raising(conn, monkeypatch):
    def responder(url):
        return _Resp("not xml <<<") if "Senate" in url else _Resp(SIX_ITEMS)

    _install_http(monkeypatch, responder)
    markets = [_market(), _market("0xb", question="Will the tropical storm make landfall by Sunday?")]
    assert asyncio.run(news.refresh(conn, markets)) == 5
    assert db.news_for(conn, "polymarket:0xa") == []
    assert len(db.news_for(conn, "polymarket:0xb")) == 5


def test_fetch_errors_are_retried_once_then_skipped(conn, monkeypatch):
    def responder(url):
        raise RuntimeError("connection reset")

    urls = _install_http(monkeypatch, responder)
    assert asyncio.run(news.refresh(conn, [_market()])) == 0
    assert len(urls) == 2


# ── selection ────────────────────────────────────────────────────────────────


def test_micro_options_ladder_and_far_events_excluded(conn, monkeypatch):
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    markets = [
        _market("0xmicro", question="Bitcoin up or down at 3:00 PM ET?"),
        {
            "uid": "options:SPY-20260814-C630",
            "venue": "options",
            "venue_id": "SPY-20260814-C630",
            "question": "SPY closes at or above $630 on 2026-08-14 (options-implied)",
            "end_date": _end(2),
            "liquidity": 99999.0,
        },
        _market("0xfar", question="Will Z happen within two weeks?", end_date=_end(10)),
        _market("0xok"),
    ]
    assert asyncio.run(news.refresh(conn, markets)) == 5
    assert len(urls) == 1
    assert {r["market_uid"] for r in conn.execute("SELECT market_uid FROM news").fetchall()} == {"polymarket:0xok"}


def test_direction_row_query_uses_bare_symbol(conn, monkeypatch):
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    markets = [
        {
            "uid": "options:SPY-20260814-D630",
            "venue": "options",
            "venue_id": "SPY-20260814-D630",
            "question": "SPY ends UP vs prev close ($630) on 2026-08-14 (options-implied; expected move ±1.2%)",
            "end_date": _end(2),
            "liquidity": 99999.0,
        }
    ]
    assert asyncio.run(news.refresh(conn, markets)) == 5
    assert len(urls) == 1
    assert "q=SPY+stock" in urls[0]
    assert "close" not in urls[0]
    assert len(db.news_for(conn, "options:SPY-20260814-D630")) == 5


def test_12h_skip_honored(conn, monkeypatch):
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    db.insert_news(conn, {"market_uid": "polymarket:0xa", "title": "already fetched", "source": "s", "url": "https://example.com/old"})
    assert asyncio.run(news.refresh(conn, [_market()])) == 0
    assert urls == []


def test_cap_prefers_liquidity(conn, monkeypatch):
    monkeypatch.setenv("FORECAST_NEWS_MAX_EVENTS", "1")
    urls = _install_http(monkeypatch, lambda url: _Resp(SIX_ITEMS))
    markets = [
        _market("0xsmall", question="Will A happen this week?", liquidity=10.0),
        _market("0xbig", question="Will B happen this week?", liquidity=99999.0),
    ]
    assert asyncio.run(news.refresh(conn, markets)) == 5
    assert len(urls) == 1
    assert {r["market_uid"] for r in conn.execute("SELECT market_uid FROM news").fetchall()} == {"polymarket:0xbig"}


def test_sports_category_excluded(conn, monkeypatch):
    end = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    picks = news._select(
        conn,
        [
            {"uid": "polymarket:s1", "venue": "polymarket", "question": "Will Arsenal win the league?", "category": "sports", "end_date": end, "liquidity": 9e9},
            {"uid": "polymarket:p1", "venue": "polymarket", "question": "Will the treaty be ratified?", "category": "politics", "end_date": end, "liquidity": 10.0},
        ],
    )
    assert [uid for uid, _q in picks] == ["polymarket:p1"]
