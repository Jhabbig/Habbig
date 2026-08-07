import asyncio
import json
from datetime import datetime, timedelta, timezone

import db
import resolver


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(venue, venue_id, hours_past_close=12):
    return {
        "venue": venue,
        "venue_id": venue_id,
        "question": f"{venue} {venue_id}?",
        "category": "weather",
        "url": "https://example.com/m",
        "end_date": _iso(datetime.now(timezone.utc) - timedelta(hours=hours_past_close)),
    }


def _gamma_payload(prices, outcomes=("Yes", "No"), closed=True):
    return [
        {
            "closed": closed,
            "outcomePrices": json.dumps([str(p) for p in prices]),
            "outcomes": json.dumps(list(outcomes)),
        }
    ]


def _kalshi_payload(status, result):
    return {"market": {"status": status, "result": result}}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, gamma_by_cid=None, kalshi_by_ticker=None):
        self.gamma_by_cid = gamma_by_cid or {}
        self.kalshi_by_ticker = kalshi_by_ticker or {}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if url == resolver.GAMMA_MARKETS_URL:
            payload = self.gamma_by_cid.get(params["condition_ids"])
            if isinstance(payload, Exception):
                raise payload
            return FakeResponse(payload if payload is not None else [])
        ticker = url.rsplit("/", 1)[1]
        payload = self.kalshi_by_ticker.get(ticker)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            return FakeResponse({"error": "not found"}, status_code=404)
        return FakeResponse(payload)


def _install(monkeypatch, client):
    monkeypatch.setattr(resolver, "_make_client", lambda: client)
    return client


def _add_calibration(conn, uid):
    db.log_calibration_row(
        conn,
        {
            "market_uid": uid,
            "platform": uid.split(":")[0],
            "category": "weather",
            "source": "market_baseline",
            "market_prob": 0.6,
            "displayed_at": "2026-08-06",
        },
    )


def test_resolves_settled_yes_and_no_on_both_venues(conn, monkeypatch):
    uids = {
        "py": db.upsert_market(conn, _market("polymarket", "cid-yes")),
        "pn": db.upsert_market(conn, _market("polymarket", "cid-no")),
        "ky": db.upsert_market(conn, _market("kalshi", "KX-YES")),
        "kn": db.upsert_market(conn, _market("kalshi", "KX-NO")),
    }
    for uid in uids.values():
        _add_calibration(conn, uid)

    client = _install(
        monkeypatch,
        FakeClient(
            gamma_by_cid={
                "cid-yes": _gamma_payload([1, 0]),
                "cid-no": _gamma_payload([0, 1]),
            },
            kalshi_by_ticker={
                "KX-YES": _kalshi_payload("settled", "yes"),
                "KX-NO": _kalshi_payload("finalized", "no"),
            },
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 4
    assert len(client.calls) == 4

    expected = {uids["py"]: 1, uids["pn"]: 0, uids["ky"]: 1, uids["kn"]: 0}
    for uid, outcome in expected.items():
        m = db.get_market(conn, uid)
        assert m["resolved"] == 1
        assert m["active"] == 0
        assert m["outcome"] == outcome
        assert m["resolved_at"]

    for uid, outcome in expected.items():
        rows = conn.execute("SELECT outcome, resolved_at FROM calibration WHERE market_uid = ?", (uid,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["outcome"] == outcome
        assert rows[0]["resolved_at"]

    assert db.unresolved_past_close(conn, grace_hours=6) == []


def test_open_markets_left_untouched(conn, monkeypatch):
    poly_uid = db.upsert_market(conn, _market("polymarket", "cid-open"))
    kalshi_uid = db.upsert_market(conn, _market("kalshi", "KX-OPEN"))
    unsettled_uid = db.upsert_market(conn, _market("polymarket", "cid-halfway"))
    _add_calibration(conn, poly_uid)

    _install(
        monkeypatch,
        FakeClient(
            gamma_by_cid={
                "cid-open": [],
                "cid-halfway": _gamma_payload([0.5, 0.5]),
            },
            kalshi_by_ticker={"KX-OPEN": _kalshi_payload("active", "")},
        ),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 0
    for uid in (poly_uid, kalshi_uid, unsettled_uid):
        m = db.get_market(conn, uid)
        assert m["resolved"] == 0
        assert m["outcome"] is None
    row = conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (poly_uid,)).fetchone()
    assert row["outcome"] is None


def test_grace_period_respected(conn, monkeypatch):
    fresh_end = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    uid = db.upsert_market(conn, _market("polymarket", "cid-fresh") | {"end_date": fresh_end})

    client = _install(monkeypatch, FakeClient(gamma_by_cid={"cid-fresh": _gamma_payload([1, 0])}))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 0
    assert client.calls == []
    assert db.get_market(conn, uid)["resolved"] == 0


def test_one_bad_market_never_stops_the_pass(conn, monkeypatch):
    bad_uid = db.upsert_market(conn, _market("polymarket", "cid-bad"))
    err_uid = db.upsert_market(conn, _market("polymarket", "cid-500"))
    good_uid = db.upsert_market(conn, _market("kalshi", "KX-GOOD"))

    fake = FakeClient(
        gamma_by_cid={"cid-bad": RuntimeError("connection reset")},
        kalshi_by_ticker={"KX-GOOD": _kalshi_payload("settled", "yes")},
    )
    fake.gamma_by_cid["cid-500"] = None

    async def _get(url, params=None, _orig=fake.get):
        if params and params.get("condition_ids") == "cid-500":
            fake.calls.append((url, params))
            return FakeResponse({"error": "boom"}, status_code=500)
        return await _orig(url, params=params)

    fake.get = _get
    _install(monkeypatch, fake)

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    assert db.get_market(conn, bad_uid)["resolved"] == 0
    assert db.get_market(conn, err_uid)["resolved"] == 0
    good = db.get_market(conn, good_uid)
    assert good["resolved"] == 1 and good["outcome"] == 1


def test_batch_capped_at_100_per_pass(conn, monkeypatch):
    gamma = {}
    for i in range(105):
        db.upsert_market(conn, _market("polymarket", f"cid-{i:03d}"))
        gamma[f"cid-{i:03d}"] = _gamma_payload([1, 0])

    client = _install(monkeypatch, FakeClient(gamma_by_cid=gamma))

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 100
    assert len(client.calls) == 100
    remaining = conn.execute("SELECT COUNT(*) FROM markets WHERE resolved = 0").fetchone()[0]
    assert remaining == 5


def test_yes_index_follows_outcomes_order(conn, monkeypatch):
    uid = db.upsert_market(conn, _market("polymarket", "cid-flipped"))
    _install(
        monkeypatch,
        FakeClient(gamma_by_cid={"cid-flipped": _gamma_payload([1, 0], outcomes=("No", "Yes"))}),
    )

    n = asyncio.run(resolver.resolve_pending(conn))
    assert n == 1
    assert db.get_market(conn, uid)["outcome"] == 0
