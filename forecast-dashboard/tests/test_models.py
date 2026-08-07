import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

import models_fed
import models_weather


def _future_date(days=3):
    return (datetime.now(timezone.utc) + timedelta(days=days)).date()


# ── weather question parsing ─────────────────────────────────────────────────


def test_parse_polymarket_over():
    p = models_weather.parse_weather_market("Will the highest temperature in NYC on August 15 be 95°F or higher?", "polymarket", "0xabc")
    assert p is not None
    assert p["city"] in ("nyc", "new york")
    assert p["icao"] == "KLGA"
    assert p["threshold"] == 95.0
    assert p["is_over"] is True
    assert p["target_date"] is not None
    assert (p["target_date"].month, p["target_date"].day) == (8, 15)


def test_parse_polymarket_between_bucket():
    p = models_weather.parse_weather_market("Highest temperature in Chicago on August 12: between 88 and 92°F?", "polymarket", "0xdef")
    assert p is not None
    assert p["city"] == "chicago"
    assert p["temp_lower"] == 88.0
    assert p["temp_upper"] == 92.0
    assert p["threshold"] is None


def test_parse_polymarket_under():
    p = models_weather.parse_weather_market("Will London's high temp be below 70°F on Aug 20?", "polymarket", "0x123")
    assert p is not None
    assert p["city"] == "london"
    assert p["icao"] == "EGLC"
    assert p["threshold"] == 70.0
    assert p["is_over"] is False


def test_parse_kalshi_b_is_range_bracket():
    # Live catalog: B95.5 = the "95° to 96°" range bracket, NOT "above 95.5".
    p = models_weather.parse_weather_market("Will the high temp in NYC be 95-96° on Aug 15, 2026? — 95° to 96°", "kalshi", "KXHIGHNY-26AUG15-B95.5")
    assert p is not None
    assert p["city"] == "new york"
    assert p["temp_lower"] == 95.0
    assert p["temp_upper"] == 96.0
    assert p["threshold"] is None
    assert p["target_date"].isoformat() == "2026-08-15"


def test_parse_kalshi_t_top_tail():
    # Live catalog: T is either tail; ">88°" ships as T88 with subtitle "89° or above".
    p = models_weather.parse_weather_market("Will the high temp in Chicago be >88° on Aug 12, 2026? — 89° or above", "kalshi", "KXHIGHCHI-26AUG12-T88")
    assert p is not None
    assert p["city"] == "chicago"
    assert p["threshold"] == 89.0
    assert p["is_over"] is True
    assert p["target_date"].isoformat() == "2026-08-12"


def test_parse_kalshi_t_bottom_tail():
    p = models_weather.parse_weather_market("Will the high temp in Chicago be <82° on Aug 12, 2026? — 81° or below", "kalshi", "KXHIGHCHI-26AUG12-T82")
    assert p is not None
    assert p["threshold"] == 81.0
    assert p["is_over"] is False


def test_parse_kalshi_subtitle_only_range():
    p = models_weather.parse_weather_market("Highest temperature in NYC today? — 95° to 96°", "kalshi", "KXHIGHNY-26AUG15-B95.5")
    assert p is not None
    assert p["temp_lower"] == 95.0
    assert p["temp_upper"] == 96.0
    assert p["threshold"] is None


def test_parse_kalshi_unparseable_text_skipped():
    # No bracket in the text → skip; the ticker suffix must never supply one.
    assert models_weather.parse_weather_market("Highest temperature in New York today?", "kalshi", "KXHIGHNY-26AUG15-B95.5") is None


def test_parse_rejects_non_weather():
    assert models_weather.parse_weather_market("Will the LA Lakers beat the Bulls by over 10 points?", "polymarket", "0x1") is None
    assert models_weather.parse_weather_market("Will Bitcoin close above 100000 in August?", "polymarket", "0x2") is None
    assert models_weather.parse_weather_market("Will Chicago approve the transit bill by August 12?", "polymarket", "0x3") is None


# ── weather probability math ─────────────────────────────────────────────────


def test_gaussian_matches_erf():
    parsed = {"threshold": 95.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    got = models_weather.gaussian_probability(90.0, 3.0, parsed)
    expected = 1.0 - 0.5 * (1.0 + math.erf((95.0 - 90.0) / (3.0 * math.sqrt(2.0))))
    assert abs(got - expected) < 1e-9
    assert abs(got - 0.047790352272814696) < 1e-9

    parsed_under = {"threshold": 70.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    got_under = models_weather.gaussian_probability(74.0, 2.5, parsed_under)
    assert abs(got_under - 0.054799291699557995) < 1e-9


def test_gaussian_clamp():
    parsed = {"threshold": 200.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    assert models_weather.gaussian_probability(70.0, 2.0, parsed) == 0.01
    parsed["is_over"] = False
    assert models_weather.gaussian_probability(70.0, 2.0, parsed) == 0.99


def test_laplace_7_of_30():
    members = [96.0] * 7 + [80.0] * 23
    parsed = {"threshold": 95.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    assert models_weather.laplace_probability(members, parsed) == 8 / 32


def test_laplace_bucket_edges():
    members = [87.5, 88.0, 90.0, 92.5, 93.0, 95.0]
    parsed = {"threshold": None, "is_over": None, "temp_lower": 88.0, "temp_upper": 92.0}
    # [87.5, 92.5] inclusive → 4 of 6 members inside → (4+1)/(6+2)
    assert models_weather.laplace_probability(members, parsed) == 5 / 8


# ── weather compute (offline, monkeypatched fetch) ───────────────────────────


def test_weather_compute_offline(monkeypatch):
    calls = []

    async def fake_fetch(client, lat, lon, target_date):
        calls.append((lat, lon, target_date.isoformat()))
        return {"members": [96.0] * 7 + [80.0] * 23, "mean": 83.7, "std": 6.9, "source": "test"}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)

    d = _future_date(3)
    ticker = f"KXHIGHNY-{d.strftime('%y%b%d').upper()}-T94"
    kalshi_q = f"Will the high temp in NYC be >94° on {d.strftime('%b')} {d.day}, {d.year}? — 95° or above"
    poly_title = f"Will the highest temperature in NYC on {d.strftime('%B')} {d.day} be 95°F or higher?"
    markets = [
        {"uid": f"kalshi:{ticker}", "venue": "kalshi", "venue_id": ticker, "question": kalshi_q, "end_date": f"{d.isoformat()}T23:59:00Z"},
        {"venue": "polymarket", "venue_id": "0xw1", "question": poly_title, "end_date": f"{d.isoformat()}T23:59:00Z"},
        {"venue": "polymarket", "venue_id": "0xr1", "question": "Will Bitcoin close above 100000?", "end_date": f"{d.isoformat()}T23:59:00Z"},
    ]
    rows = asyncio.run(models_weather.compute(markets))

    assert len(rows) == 2
    assert len(calls) == 1  # same (station, date) → cached, one fetch
    by_uid = {r["market_uid"]: r for r in rows}
    assert f"kalshi:{ticker}" in by_uid
    assert "polymarket:0xw1" in by_uid
    for r in rows:
        assert r["source"] == "weather"
        assert r["prob_method"] == "ensemble"
        detail = json.loads(r["detail"])
        assert detail["station"] == "KLGA"
        assert detail["date"] == d.isoformat()
        assert detail["members"] == 30
    # kalshi ">94°" = "95° or above" → threshold 95: 7 members at 96 above → 8/32
    assert by_uid[f"kalshi:{ticker}"]["model_prob"] == 0.25
    # poly threshold 95: same count
    assert by_uid["polymarket:0xw1"]["model_prob"] == 0.25


def test_weather_compute_gaussian_fallback(monkeypatch):
    async def fake_fetch(client, lat, lon, target_date):
        return {"members": [90.0], "mean": 90.0, "std": 3.0, "source": "test-deterministic"}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)

    d = _future_date(2)
    markets = [
        {"venue": "polymarket", "venue_id": "0xg1", "question": f"Will the highest temperature in Miami on {d.strftime('%B')} {d.day} be 95°F or higher?", "end_date": f"{d.isoformat()}T23:59:00Z"}
    ]
    rows = asyncio.run(models_weather.compute(markets))
    assert len(rows) == 1
    assert rows[0]["prob_method"] == "gaussian"
    expected = 1.0 - 0.5 * (1.0 + math.erf((95.0 - 90.0) / (3.0 * math.sqrt(2.0))))
    assert abs(rows[0]["model_prob"] - round(max(0.01, expected), 4)) < 1e-9


# ── fed bucket classification ────────────────────────────────────────────────


def test_fed_classify_titles():
    cases = [
        ("Will the Fed cut rates by 25 bps in September 2026?", 4.50, "cut25"),
        ("Fed rate decision: 50 bp hike in June?", 4.50, "hike50"),
        ("Will the FOMC hold rates steady in May?", 4.50, "hold"),
        ("Federal Reserve raises rates by 25 basis points", 4.50, "hike25"),
        ("Will the Federal Reserve Cut rates by 25bps at their June 2026 meeting?", 4.50, "cut25"),
        ("Will the Federal Reserve Hike rates by 0bps at their June 2026 meeting?", 4.50, "hold"),
        ("Fed funds target rate at 4.25%-4.50% after April 2026 meeting", 4.50, "cut25"),
        ("Will the Federal Reserve Cut rates by >25bps at their June 2026 meeting?", 4.50, None),
        ("Bitcoin to $200k by year-end", None, None),
        ("Trump approval rating above 45%?", 4.50, None),
    ]
    for text, rate, expected in cases:
        assert models_fed.classify(text, rate) == expected, text


def test_fed_derive_probs():
    probs = models_fed.derive_probs(-0.15)
    assert probs == {"hold": 0.4, "cut25": 0.6}
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert models_fed.derive_probs(-0.25) == {"cut25": 1.0}
    assert models_fed.derive_probs(0.0) == {"hold": 1.0}
    probs_hike = models_fed.derive_probs(0.30)
    assert probs_hike == {"hike25": 0.8, "hike50": 0.2}
    assert abs(sum(probs_hike.values()) - 1.0) < 1e-9


def test_fed_contract_symbol():
    assert models_fed.ff_contract_symbol(2026, 10) == "ZQV26.CBT"
    assert models_fed.ff_contract_symbol(2026, 5) == "ZQK26.CBT"


# ── fed compute (offline, monkeypatched fetches) ─────────────────────────────


def test_fed_compute_buckets_sum(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setenv("FORECAST_DFF", "4.50")
    monkeypatch.setattr(models_fed, "_next_fomc", lambda today=None: {"decision_date": "2026-09-16", "label": "FOMC"})

    async def fake_yahoo(client, symbol):
        assert symbol == "ZQV26.CBT"
        return 95.65

    monkeypatch.setattr(models_fed, "_fetch_yahoo_close", fake_yahoo)

    markets = [
        {"venue": "polymarket", "venue_id": "0xhold", "question": "Will the FOMC hold rates steady at the September 2026 meeting?", "end_date": "2026-09-16T18:00:00Z"},
        {"venue": "polymarket", "venue_id": "0xcut25", "question": "Will the Fed cut rates by 25 bps in September 2026?", "end_date": "2026-09-16T18:00:00Z"},
        {"venue": "kalshi", "venue_id": "KXFED-26SEP-C50", "question": "Will the Federal Reserve Cut rates by 50bps at their September 2026 meeting?", "end_date": "2026-09-16T18:00:00Z"},
        {"venue": "kalshi", "venue_id": "KXFED-26SEP-H25", "question": "Will the Federal Reserve Hike rates by 25bps at their September 2026 meeting?", "end_date": "2026-09-16T18:00:00Z"},
        # December market must be skipped — ZQ path prices only the next meeting
        {"venue": "polymarket", "venue_id": "0xdec", "question": "Will the Fed cut rates by 25 bps in December 2026?", "end_date": "2026-12-09T18:00:00Z"},
        {"venue": "polymarket", "venue_id": "0xbtc", "question": "Will Bitcoin close above 100000 in September?", "end_date": "2026-09-16T18:00:00Z"},
    ]
    rows = asyncio.run(models_fed.compute(markets))

    by_uid = {r["market_uid"]: r for r in rows}
    assert set(by_uid) == {"polymarket:0xhold", "polymarket:0xcut25", "kalshi:KXFED-26SEP-C50", "kalshi:KXFED-26SEP-H25"}
    assert by_uid["polymarket:0xhold"]["model_prob"] == 0.4
    assert by_uid["polymarket:0xcut25"]["model_prob"] == 0.6
    assert by_uid["kalshi:KXFED-26SEP-C50"]["model_prob"] == 0.0
    assert by_uid["kalshi:KXFED-26SEP-H25"]["model_prob"] == 0.0
    assert abs(sum(r["model_prob"] for r in rows) - 1.0) < 1e-6
    for r in rows:
        assert r["source"] == "fed_implied"
        assert r["prob_method"] == "zq_interp"
        detail = json.loads(r["detail"])
        assert detail["contract"] == "ZQV26.CBT"
        assert detail["meeting_date"] == "2026-09-16"
        assert abs(detail["implied_post_rate"] - 4.35) < 1e-6
        assert abs(sum(detail["probabilities"].values()) - 1.0) < 1e-9


def test_fed_compute_no_dff_skips(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("FORECAST_DFF", raising=False)
    monkeypatch.setattr(models_fed, "_next_fomc", lambda today=None: {"decision_date": "2026-09-16", "label": "FOMC"})

    async def fake_yahoo(client, symbol):
        raise AssertionError("must not reach Yahoo without a DFF rate")

    monkeypatch.setattr(models_fed, "_fetch_yahoo_close", fake_yahoo)
    markets = [{"venue": "polymarket", "venue_id": "0x1", "question": "Will the Fed cut rates by 25 bps in September 2026?", "end_date": "2026-09-16T18:00:00Z"}]
    assert asyncio.run(models_fed.compute(markets)) == []


def test_fed_compute_empty():
    assert asyncio.run(models_fed.compute([])) == []
