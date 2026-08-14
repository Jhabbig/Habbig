import asyncio
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

import db
import models_fed
import models_weather


def _future_date(days=3):
    return (datetime.now(timezone.utc) + timedelta(days=days)).date()


def _mix_cdf(x, members, sigma):
    return sum(0.5 * (1.0 + math.erf((x - t) / (sigma * math.sqrt(2.0)))) for t in members) / len(members)


# ── weather question parsing ─────────────────────────────────────────────────


def test_parse_polymarket_over():
    p = models_weather.parse_weather_market("Will the highest temperature in NYC on August 15 be 95°F or higher?", "polymarket", "0xabc")
    assert p is not None
    assert p["city"] in ("nyc", "new york")
    assert p["icao"] == "KLGA"
    assert p["threshold"] == 95.0
    assert p["is_over"] is True
    assert p["tz"] == "America/New_York"
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


def test_parse_daily_low_markets_get_low_kind():
    p1 = models_weather.parse_weather_market("Will the lowest temperature in Miami be between 78-79°F on August 13?", "polymarket", "0x1")
    assert p1["kind"] == "low" and p1["temp_lower"] == 78.0 and p1["temp_upper"] == 79.0
    p2 = models_weather.parse_weather_market("Will the low temp in NYC be 66°F or lower on August 13?", "polymarket", "0x2")
    assert p2["kind"] == "low" and p2["threshold"] == 66.0 and p2["is_over"] is False
    p3 = models_weather.parse_weather_market("Will the highest temperature in Miami be between 94-95°F on August 13?", "polymarket", "0x3")
    assert p3["kind"] == "high"
    assert models_weather.parse_weather_market("Will the highest temperature in Miami be between 94-95°F on August 13?", "polymarket", "0x3") is not None


def test_parse_rejects_non_weather():
    assert models_weather.parse_weather_market("Will the LA Lakers beat the Bulls by over 10 points?", "polymarket", "0x1") is None
    assert models_weather.parse_weather_market("Will Bitcoin close above 100000 in August?", "polymarket", "0x2") is None
    assert models_weather.parse_weather_market("Will Chicago approve the transit bill by August 12?", "polymarket", "0x3") is None


# ── weather probability math ─────────────────────────────────────────────────


def test_gaussian_matches_erf():
    # Inclusive thresholds share half-degree edges with the brackets:
    # >=95 evaluates at 94.5, <=70 at 70.5 (same convention as dressing).
    parsed = {"threshold": 95.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    got = models_weather.gaussian_probability(90.0, 3.0, parsed)
    expected = 1.0 - 0.5 * (1.0 + math.erf((94.5 - 90.0) / (3.0 * math.sqrt(2.0))))
    assert abs(got - expected) < 1e-9
    assert abs(got - 0.06680720126885809) < 1e-9

    parsed_under = {"threshold": 70.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    got_under = models_weather.gaussian_probability(74.0, 2.5, parsed_under)
    assert abs(got_under - 0.08075665923377107) < 1e-9


def test_gaussian_ladder_sums_to_one():
    # <=85 | 86-87 | 88-89 | >=90 partitions the line at 85.5/87.5/89.5.
    mean, std = 88.0, 2.0
    ladder = [
        {"threshold": 85.0, "is_over": False, "temp_lower": None, "temp_upper": None},
        {"threshold": None, "is_over": None, "temp_lower": 86.0, "temp_upper": 87.0},
        {"threshold": None, "is_over": None, "temp_lower": 88.0, "temp_upper": 89.0},
        {"threshold": 90.0, "is_over": True, "temp_lower": None, "temp_upper": None},
    ]
    total = sum(models_weather.gaussian_probability(mean, std, p) for p in ladder)
    assert abs(total - 1.0) < 1e-9


def test_gaussian_runmax_constraint():
    below = {"threshold": None, "is_over": None, "temp_lower": 89.0, "temp_upper": 90.0}
    assert models_weather.gaussian_probability(88.0, 1.0, below, runmax=91.0) == 0.01

    containing = {"threshold": None, "is_over": None, "temp_lower": 90.0, "temp_upper": 91.0}
    with_run = models_weather.gaussian_probability(88.0, 2.0, containing, runmax=90.0)
    without = models_weather.gaussian_probability(88.0, 2.0, containing)
    # mass below the running max collapses onto it → bracket absorbs CDF(89.5)
    assert abs(with_run - 0.9599408431361829) < 1e-9
    assert with_run > without

    over = {"threshold": 94.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    assert models_weather.gaussian_probability(88.0, 2.5, over, runmax=95.0) == 0.99
    under = {"threshold": 94.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    assert models_weather.gaussian_probability(88.0, 2.5, under, runmax=95.0) == 0.01


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


# ── kernel dressing ──────────────────────────────────────────────────────────


def test_dressed_bracket_hand_computed():
    # members [88, 90], sigma 1.0, bracket 89–90 → edges 88.5 / 90.5:
    # 0.5*[(Φ(2.5)-Φ(0.5)) + (Φ(0.5)-Φ(-1.5))]
    parsed = {"threshold": None, "is_over": None, "temp_lower": 89.0, "temp_upper": 90.0}
    got = models_weather.dressed_probability([88.0, 90.0], parsed, 1.0)
    expected = _mix_cdf(90.5, [88.0, 90.0], 1.0) - _mix_cdf(88.5, [88.0, 90.0], 1.0)
    assert abs(got - expected) < 1e-9
    assert abs(got - 0.4634915667026829) < 1e-9


def test_dressed_threshold_tails_hand_computed():
    members = [88.0, 90.0]
    over = {"threshold": 90.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    got_over = models_weather.dressed_probability(members, over, 1.0)
    assert abs(got_over - (1.0 - _mix_cdf(89.5, members, 1.0))) < 1e-9
    under = {"threshold": 89.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    got_under = models_weather.dressed_probability(members, under, 1.0)
    assert abs(got_under - _mix_cdf(89.5, members, 1.0)) < 1e-9
    # ">=90" and "<=89" partition the line at 89.5 → complementary
    assert abs(got_over + got_under - 1.0) < 1e-9


def test_dressed_bias_shifts_members():
    parsed = {"threshold": None, "is_over": None, "temp_lower": 89.0, "temp_upper": 90.0}
    biased = models_weather.dressed_probability([88.0, 90.0], parsed, 1.0, bias=1.0)
    shifted = models_weather.dressed_probability([89.0, 91.0], parsed, 1.0)
    assert abs(biased - shifted) < 1e-12


def test_dressed_runmax_constraint():
    members = [88.0, 90.0]
    below = {"threshold": None, "is_over": None, "temp_lower": 89.0, "temp_upper": 90.0}
    assert models_weather.dressed_probability(members, below, 1.0, runmax=91.0) == 0.01

    containing = {"threshold": None, "is_over": None, "temp_lower": 90.0, "temp_upper": 91.0}
    with_run = models_weather.dressed_probability(members, containing, 1.0, runmax=90.0)
    without = models_weather.dressed_probability(members, containing, 1.0)
    # mass below the running max collapses onto it → bracket absorbs CDF(89.5)
    assert abs(with_run - _mix_cdf(91.5, members, 1.0)) < 1e-9
    assert with_run > without

    over = {"threshold": 85.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    assert models_weather.dressed_probability(members, over, 1.0, runmax=90.0) == 0.99
    under = {"threshold": 85.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    assert models_weather.dressed_probability(members, under, 1.0, runmax=90.0) == 0.01


def test_sigma_from_residual_clamps():
    assert models_weather._sigma_from_residual(None) == 1.8
    assert models_weather._sigma_from_residual(0.5) == 1.2
    assert models_weather._sigma_from_residual(5.0) == 3.0
    assert models_weather._sigma_from_residual(1.269) == 1.269


# ── ledger bias recovery ─────────────────────────────────────────────────────


def _seed_resolution(conn, uid, station, day, mean, lower=None, upper=None, threshold=None, is_over=None, outcome=1, displayed_at=None, kind=None):
    detail = {"station": station, "city": "x", "date": day, "threshold": threshold, "temp_lower": lower, "temp_upper": upper, "is_over": is_over, "mean": mean, "std": 2.0, "members": 31}
    if kind is not None:
        detail["kind"] = kind
    detail = json.dumps(detail)
    db.insert_model_prob(conn, {"market_uid": uid, "source": "weather", "model_prob": 0.5, "prob_method": "ensemble_dressed", "detail": detail})
    db.log_calibration_row(conn, {"market_uid": uid, "source": "weather", "outcome": outcome, "displayed_at": displayed_at or day})


def test_learn_station_bias(conn):
    _seed_resolution(conn, "kalshi:MIA1", "KMIA", "2026-08-07", 88.0, lower=89.0, upper=90.0)
    _seed_resolution(conn, "kalshi:MIA1", "KMIA", "2026-08-07", 88.0, lower=89.0, upper=90.0, displayed_at="2026-08-08")  # duplicate scoring → one day
    _seed_resolution(conn, "kalshi:MIA2", "KMIA", "2026-08-07", 88.0, lower=91.0, upper=92.0, outcome=0)  # losing bracket ignored
    _seed_resolution(conn, "kalshi:MIA3", "KMIA", "2026-08-07", 88.0, threshold=79.0, is_over=True)  # tail ignored
    _seed_resolution(conn, "kalshi:ORD1", "KORD", "2026-08-07", 87.0, lower=86.0, upper=87.0)
    _seed_resolution(conn, "kalshi:ORD2", "KORD", "2026-08-08", 85.5, lower=84.0, upper=85.0)
    _seed_resolution(conn, "kalshi:BIG1", "KBIG", "2026-08-07", 59.5, lower=99.0, upper=100.0)  # err +40 → cap
    _seed_resolution(conn, "kalshi:BAD1", "KBAD", "2026-08-07", 80.0, lower=81.0, upper=82.0)  # contradictory winners
    _seed_resolution(conn, "kalshi:BAD2", "KBAD", "2026-08-07", 80.0, lower=84.0, upper=85.0)

    out = models_weather.learn_station_bias(conn)
    assert out["days"] == 4
    assert abs(out["bias"][("KMIA", "high")] - 1.5 * (1 / 6)) < 1e-9
    assert abs(out["bias"][("KORD", "high")] - (-0.75) * (2 / 7)) < 1e-9
    assert out["bias"][("KBIG", "high")] == 4.0  # 40 * 1/6 capped at +4
    assert ("KBAD", "high") not in out["bias"]
    assert abs(out["residual_std"] - statistics.stdev([1.5, -0.5, -1.0, 40.0])) < 1e-9


def test_learn_station_bias_empty(conn):
    out = models_weather.learn_station_bias(conn)
    assert out == {"bias": {}, "residual_std": None, "days": 0}


def test_learn_station_bias_overlapping_winners_intersect(conn):
    # Kalshi "83-84" and Polymarket "84-85" both pay when the high is 84 —
    # the intersection pins the high to 84, not a contradiction.
    _seed_resolution(conn, "kalshi:ORDA", "KORD", "2026-08-08", 85.0, lower=83.0, upper=84.0)
    _seed_resolution(conn, "polymarket:ORDB", "KORD", "2026-08-08", 85.0, lower=84.0, upper=85.0)
    out = models_weather.learn_station_bias(conn)
    assert out["days"] == 1
    assert abs(out["bias"][("KORD", "high")] - (84.0 - 85.0) * (1 / 6)) < 1e-9


def test_learn_station_bias_skips_legacy_low_rows_learns_new_ones(conn):
    # Legacy low rows (detail carries no kind → 'high') mismatch their
    # question's kind and are skipped; new low rows with kind='low' in the
    # detail feed a separate (station, 'low') bias.
    db.upsert_market(conn, {"venue": "polymarket", "venue_id": "MIAHI", "question": "Will the highest temperature in Miami be between 94-95°F on August 13?"})
    db.upsert_market(conn, {"venue": "polymarket", "venue_id": "MIALO", "question": "Will the lowest temperature in Miami be between 78-79°F on August 13?"})
    _seed_resolution(conn, "polymarket:MIAHI", "KMIA", "2026-08-13", 93.0, lower=94.0, upper=95.0)
    _seed_resolution(conn, "polymarket:MIALO", "KMIA", "2026-08-13", 93.0, lower=78.0, upper=79.0)
    out = models_weather.learn_station_bias(conn)
    assert out["days"] == 1
    assert abs(out["bias"][("KMIA", "high")] - 1.5 * (1 / 6)) < 1e-9
    assert ("KMIA", "low") not in out["bias"]

    db.upsert_market(conn, {"venue": "polymarket", "venue_id": "MIALO2", "question": "Will the lowest temperature in Miami be between 76-77°F on August 14?"})
    _seed_resolution(conn, "polymarket:MIALO2", "KMIA", "2026-08-14", 78.0, lower=76.0, upper=77.0, kind="low")
    out2 = models_weather.learn_station_bias(conn)
    assert out2["days"] == 2
    assert abs(out2["bias"][("KMIA", "low")] - (-1.5) * (1 / 6)) < 1e-9


# ── HTTP contracts (mocked transport) ────────────────────────────────────────


def test_open_meteo_ensemble_request_and_member_parsing():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "daily_units": {"time": "iso8601"},
                "daily": {
                    "time": ["2026-08-20"],
                    "temperature_2m_max_ncep_gefs_seamless": [90.0],  # control: no memberNN
                    "temperature_2m_max_member01_ncep_gefs_seamless": [91.0],
                    "temperature_2m_max_ecmwf_ifs025_ensemble": [89.0],
                    "temperature_2m_max_member01_ecmwf_ifs025_ensemble": [92.0],
                    "temperature_2m_min_ncep_gefs_seamless": [72.0],
                    "temperature_2m_min_member01_ncep_gefs_seamless": [73.5],
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await models_weather._fetch_forecast(client, 25.7959, -80.287, datetime(2026, 8, 20).date())

    bundle = asyncio.run(run())
    params = seen[0].url.params
    assert params["timezone"] == "auto"
    assert params["daily"] == "temperature_2m_max,temperature_2m_min"
    assert params["models"] == "gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global"
    assert bundle["high"]["source"] == "open-meteo-ensemble"
    assert sorted(bundle["high"]["members"]) == [89.0, 90.0, 91.0, 92.0]
    assert bundle["high"]["models"] == {"ncep_gefs_seamless": 2, "ecmwf_ifs025_ensemble": 2}
    assert sorted(bundle["low"]["members"]) == [72.0, 73.5]
    assert bundle["low"]["models"] == {"ncep_gefs_seamless": 2}


def test_open_meteo_deterministic_fallback_keeps_timezone():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "ensemble-api.open-meteo.com":
            return httpx.Response(500)
        return httpx.Response(200, json={"daily": {"temperature_2m_max": [90.0], "temperature_2m_min": [71.0]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await models_weather._fetch_forecast(client, 25.7959, -80.287, _future_date(2))

    bundle = asyncio.run(run())
    assert bundle["high"]["source"] == "open-meteo-deterministic"
    assert bundle["high"]["members"] == [90.0]
    assert bundle["low"]["members"] == [71.0]
    fallback = seen[1].url.params
    assert fallback["timezone"] == "auto"
    assert "models" not in fallback


def test_metar_extremes_c_to_f_and_local_day():
    tz = "America/New_York"
    target = datetime(2026, 8, 14).date()

    def _epoch(hour, day=14):
        return int(datetime(2026, 8, day, hour, 53, tzinfo=ZoneInfo(tz)).timestamp())

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"icaoId": "KMIA", "temp": 31.1, "obsTime": _epoch(13)},
                {"icaoId": "KMIA", "temp": 30.0, "obsTime": _epoch(12)},
                {"icaoId": "KMIA", "temp": 35.0, "obsTime": _epoch(23, day=13)},  # previous local day
                {"icaoId": "KMIA", "temp": None, "obsTime": _epoch(11)},
                {"icaoId": "KLGA", "temp": 25.0, "obsTime": _epoch(13)},
                {"icaoId": "KORD", "temp": 40.0, "obsTime": _epoch(13)},  # not requested
            ],
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await models_weather._fetch_metar_extremes(client, {"KMIA": (tz, target), "KLGA": (tz, target)})

    out = asyncio.run(run())
    assert out["KMIA"]["high"] == pytest.approx(31.1 * 9 / 5 + 32)  # 87.98
    assert out["KMIA"]["low"] == pytest.approx(86.0)  # 30.0 C
    assert out["KLGA"] == {"high": pytest.approx(77.0), "low": pytest.approx(77.0)}
    assert "KORD" not in out
    assert seen[0].url.params["ids"] == "KLGA,KMIA"


# ── weather compute (offline, monkeypatched fetch) ───────────────────────────


def test_weather_compute_offline(monkeypatch, tmp_path):
    calls = []
    members = [96.0] * 7 + [80.0] * 23

    async def fake_fetch(client, lat, lon, target_date):
        calls.append((lat, lon, target_date.isoformat()))
        return {"high": {"members": list(members), "mean": 83.7, "std": 6.9, "models": {"ncep_gefs_seamless": 30}, "source": "test"}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)
    real_get_conn = db.get_conn
    monkeypatch.setattr(db, "get_conn", lambda: real_get_conn(tmp_path / "ledger.sqlite"))

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
    # empty ledger → bias 0, sigma_k 1.8; threshold 95 "or above" → 1 - CDF(94.5)
    expected = round(1.0 - _mix_cdf(94.5, members, 1.8), 4)
    for r in rows:
        assert r["source"] == "weather"
        assert r["prob_method"] == "ensemble_dressed"
        assert r["model_prob"] == expected
        detail = json.loads(r["detail"])
        assert detail["station"] == "KLGA"
        assert detail["date"] == d.isoformat()
        assert detail["members"] == 30
        assert detail["bias"] == 0.0
        assert detail["sigma_k"] == 1.8
        assert detail["runmax"] is None
        assert detail["models"] == {"ncep_gefs_seamless": 30}
        assert detail["tz"] == "America/New_York"


def test_weather_compute_intraday_runmax(monkeypatch, conn):
    async def fake_fetch(client, lat, lon, target_date):
        return {"high": {"members": [90.0] * 12, "mean": 90.0, "std": 0.5, "models": {"ncep_gefs_seamless": 12}, "source": "test"}}

    async def fake_metar(client, stations):
        assert stations == {"KLGA": ("America/New_York", d)}
        return {"KLGA": {"high": 96.5, "low": 74.0}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)
    monkeypatch.setattr(models_weather, "_fetch_metar_extremes", fake_metar)

    d = datetime.now(ZoneInfo("America/New_York")).date()
    ticker = f"KXHIGHNY-{d.strftime('%y%b%d').upper()}-B90.5"
    q = f"Will the high temp in NYC be 90-91° on {d.strftime('%b')} {d.day}, {d.year}? — 90° to 91°"
    markets = [{"venue": "kalshi", "venue_id": ticker, "question": q, "end_date": f"{d.isoformat()}T23:59:00Z"}]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))

    assert len(rows) == 1
    assert rows[0]["prob_method"] == "ensemble_intraday"
    assert rows[0]["model_prob"] == 0.01  # bracket 90–91 entirely below runmax 96.5
    detail = json.loads(rows[0]["detail"])
    assert detail["runmax"] == 96.5


def test_weather_compute_gaussian_fallback_keeps_runmax(monkeypatch, conn):
    # Deterministic fallback (ensemble fetch degraded) must still honour the
    # observed intraday running max: high already locked in → ~1, not ~0.
    async def fake_fetch(client, lat, lon, target_date):
        return {"high": {"members": [88.0], "mean": 88.0, "std": 2.5, "models": {}, "source": "test-deterministic"}}

    async def fake_metar(client, stations):
        return {"KLGA": {"high": 95.0, "low": 73.0}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)
    monkeypatch.setattr(models_weather, "_fetch_metar_extremes", fake_metar)

    d = datetime.now(ZoneInfo("America/New_York")).date()
    q = f"Will the highest temperature in New York City be 94°F or higher on {d.strftime('%B')} {d.day}?"
    markets = [{"venue": "polymarket", "venue_id": "0xg2", "question": q, "end_date": f"{d.isoformat()}T23:59:00Z"}]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))

    assert len(rows) == 1
    assert rows[0]["prob_method"] == "gaussian_intraday"
    assert rows[0]["model_prob"] == 0.99  # runmax 95 ≥ threshold 94
    assert json.loads(rows[0]["detail"])["runmax"] == 95.0


def test_weather_compute_applies_ledger_bias(monkeypatch, conn):
    _seed_resolution(conn, "kalshi:OLD1", "KLGA", "2026-08-07", 89.0, lower=91.5, upper=92.5)  # err +3 → shrunk +0.5

    async def fake_fetch(client, lat, lon, target_date):
        return {"high": {"members": [90.0] * 12, "mean": 90.0, "std": 0.5, "models": {}, "source": "test"}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)

    d = _future_date(2)
    markets = [
        {"venue": "polymarket", "venue_id": "0xb1", "question": f"Will the highest temperature in New York on {d.strftime('%B')} {d.day} be 92°F or higher?", "end_date": f"{d.isoformat()}T23:59:00Z"}
    ]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["bias"] == 0.5
    assert detail["sigma_k"] == 1.8  # single resolved day → no residual_std yet
    expected = round(1.0 - _mix_cdf(91.5, [90.5] * 12, 1.8), 4)
    assert rows[0]["model_prob"] == expected


def test_weather_compute_gaussian_fallback(monkeypatch, conn):
    async def fake_fetch(client, lat, lon, target_date):
        return {"high": {"members": [90.0], "mean": 90.0, "std": 3.0, "models": {}, "source": "test-deterministic"}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)

    d = _future_date(2)
    markets = [
        {"venue": "polymarket", "venue_id": "0xg1", "question": f"Will the highest temperature in Miami on {d.strftime('%B')} {d.day} be 95°F or higher?", "end_date": f"{d.isoformat()}T23:59:00Z"}
    ]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))
    assert len(rows) == 1
    assert rows[0]["prob_method"] == "gaussian"
    # inclusive threshold → half-degree edge at 94.5
    expected = 1.0 - 0.5 * (1.0 + math.erf((94.5 - 90.0) / (3.0 * math.sqrt(2.0))))
    assert abs(rows[0]["model_prob"] - round(max(0.01, expected), 4)) < 1e-9


def test_dressed_runmin_constraint():
    members = [70.0] * 10
    above = {"threshold": None, "is_over": None, "temp_lower": 74.0, "temp_upper": 75.0}
    # bracket entirely above the running min → impossible for a daily low
    assert models_weather.dressed_probability(members, above, 1.0, runmin=72.0) == 0.01
    containing = {"threshold": None, "is_over": None, "temp_lower": 71.0, "temp_upper": 72.0}
    # mass above the running min collapses onto it → bracket absorbs 1 - CDF(70.5)
    with_run = models_weather.dressed_probability(members, containing, 1.0, runmin=72.0)
    without = models_weather.dressed_probability(members, containing, 1.0)
    assert with_run > without
    under = {"threshold": 73.0, "is_over": False, "temp_lower": None, "temp_upper": None}
    assert models_weather.dressed_probability(members, under, 1.0, runmin=72.0) == 0.99
    over = {"threshold": 73.0, "is_over": True, "temp_lower": None, "temp_upper": None}
    assert models_weather.dressed_probability(members, over, 1.0, runmin=72.0) == 0.01


def test_weather_compute_low_market_uses_min_ensemble(monkeypatch, conn):
    async def fake_fetch(client, lat, lon, target_date):
        return {
            "high": {"members": [95.0] * 12, "mean": 95.0, "std": 0.5, "models": {}, "source": "test"},
            "low": {"members": [76.0] * 12, "mean": 76.0, "std": 0.5, "models": {"ncep_gefs_seamless": 12}, "source": "test"},
        }

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)

    d = _future_date(2)
    markets = [
        {"venue": "polymarket", "venue_id": "0xlo1", "question": f"Will the lowest temperature in Miami on {d.strftime('%B')} {d.day} be between 76-77°F?", "end_date": f"{d.isoformat()}T23:59:00Z"}
    ]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))
    assert len(rows) == 1
    assert rows[0]["prob_method"] == "ensemble_dressed"
    detail = json.loads(rows[0]["detail"])
    assert detail["kind"] == "low"
    assert detail["mean"] == 76.0
    expected = round(_mix_cdf(77.5, [76.0] * 12, 1.8) - _mix_cdf(75.5, [76.0] * 12, 1.8), 4)
    assert rows[0]["model_prob"] == expected


def test_weather_compute_low_market_intraday_runmin(monkeypatch, conn):
    async def fake_fetch(client, lat, lon, target_date):
        return {"low": {"members": [76.0] * 12, "mean": 76.0, "std": 0.5, "models": {}, "source": "test"}}

    async def fake_metar(client, stations):
        return {"KMIA": {"high": 90.0, "low": 74.5}}

    monkeypatch.setattr(models_weather, "_fetch_forecast", fake_fetch)
    monkeypatch.setattr(models_weather, "_fetch_metar_extremes", fake_metar)

    d = datetime.now(ZoneInfo("America/New_York")).date()
    markets = [
        {"venue": "polymarket", "venue_id": "0xlo2", "question": f"Will the lowest temperature in Miami on {d.strftime('%B')} {d.day} be between 78-79°F?", "end_date": f"{d.isoformat()}T23:59:00Z"}
    ]
    rows = asyncio.run(models_weather.compute(markets, conn=conn))
    assert len(rows) == 1
    assert rows[0]["prob_method"] == "ensemble_intraday"
    # bracket 78-79 entirely above the observed running min 74.5 → impossible
    assert rows[0]["model_prob"] == 0.01
    detail = json.loads(rows[0]["detail"])
    assert detail["runmin"] == 74.5
    assert detail["runmax"] is None


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
