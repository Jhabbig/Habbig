"""Offline tests for the empirical recalibration model — tmp DB, no network."""

import asyncio
import json

import pytest

import db
import models_calibrated


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Same path the conftest `conn` fixture opens, so seeded rows are visible
    # to compute()'s own connection.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.delenv("FORECAST_CALIB_K", raising=False)
    monkeypatch.delenv("FORECAST_CALIB_MIN_N", raising=False)


def _seed(conn, platform, prob, hits, misses, start=0):
    """hits rows with outcome 1 and misses rows with outcome 0, all at market_prob=prob."""
    for i in range(hits + misses):
        db.log_calibration_row(
            conn,
            {
                "market_uid": f"{platform}:cal{start + i}",
                "platform": platform,
                "market_prob": prob,
                "outcome": 1 if i < hits else 0,
                "displayed_at": "2026-08-01",
            },
        )
    return start + hits + misses


def _market(venue="polymarket", venue_id="m1", baseline=0.9):
    return {"uid": f"{venue}:{venue_id}", "venue": venue, "venue_id": venue_id, "question": "Will X?", "baseline_prob": baseline}


def _run(markets):
    return asyncio.run(models_calibrated.compute(markets))


# ── bias correction ──────────────────────────────────────────────────────────


def test_known_bias_corrected_downward(conn):
    # 300 resolved pairs priced 0.9 that hit only 75% of the time.
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    rows = _run([_market(baseline=0.9)])
    assert len(rows) == 1
    r = rows[0]
    assert r["market_uid"] == "polymarket:m1"
    assert r["source"] == "calibrated"
    assert r["prob_method"] == "empirical_recalibration"
    w = 300 / (300 + 50)
    assert r["model_prob"] == pytest.approx(0.9 + w * (0.75 - 0.9), abs=1e-3)
    assert r["model_prob"] < 0.9


def test_detail_json_parses(conn):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    (r,) = _run([_market(baseline=0.9)])
    d = json.loads(r["detail"])
    assert d["bin"] == 9
    assert d["n_bin"] == 300
    assert d["venue_n"] == 300
    assert d["bin_hit_rate"] == pytest.approx(0.75)
    assert d["bin_mean_prob"] == pytest.approx(0.9)
    assert d["weight"] == pytest.approx(300 / 350, abs=1e-4)


def test_correction_is_per_venue(conn):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    rows = _run([_market(baseline=0.9), _market(venue="kalshi", venue_id="k1", baseline=0.9)])
    assert [r["market_uid"] for r in rows] == ["polymarket:m1"]


# ── gates / identity ─────────────────────────────────────────────────────────


def test_identity_below_min_n(conn):
    _seed(conn, "polymarket", 0.9, hits=112, misses=38)  # 150 < 200
    assert _run([_market(baseline=0.9)]) == []


def test_min_n_env_override(conn, monkeypatch):
    _seed(conn, "polymarket", 0.9, hits=112, misses=38)
    monkeypatch.setenv("FORECAST_CALIB_MIN_N", "100")
    assert len(_run([_market(baseline=0.9)])) == 1


def test_k_env_override(conn, monkeypatch):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    monkeypatch.setenv("FORECAST_CALIB_K", "0")  # w=1: full bias correction
    (r,) = _run([_market(baseline=0.9)])
    assert r["model_prob"] == pytest.approx(0.75, abs=1e-3)


def test_skips_custom_venue(conn):
    _seed(conn, "custom", 0.9, hits=225, misses=75)
    assert _run([_market(venue="custom", baseline=0.9)]) == []


def test_skips_none_baseline(conn):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    assert _run([_market(baseline=None)]) == []


def test_empty_bin_stays_identity(conn):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    assert _run([_market(baseline=0.3)]) == []  # no evidence in the 0.3 bucket


def test_near_identity_rows_suppressed(conn):
    _seed(conn, "polymarket", 0.5, hits=150, misses=150)  # perfectly calibrated bucket
    assert _run([_market(baseline=0.5)]) == []


# ── clamping ─────────────────────────────────────────────────────────────────


def test_clamps_low(conn):
    _seed(conn, "polymarket", 0.05, hits=0, misses=300)  # h=0 drags low baselines below 0
    (r,) = _run([_market(baseline=0.02)])
    assert r["model_prob"] == 0.01


def test_clamps_high(conn):
    _seed(conn, "polymarket", 0.95, hits=300, misses=0)  # h=1 pushes high baselines past 1
    (r,) = _run([_market(baseline=0.98)])
    assert r["model_prob"] == 0.99


# ── monotonicity (PAV) ───────────────────────────────────────────────────────


def test_pav_pools_violating_buckets():
    stats = [None] * 10
    stats[3] = (0.35, 0.9, 150)
    stats[4] = (0.45, 0.2, 150)
    out = models_calibrated._pav(stats)
    assert out[3] == (0.35, pytest.approx(0.55), 150)
    assert out[4] == (0.45, pytest.approx(0.55), 150)
    assert out[:3] == [None] * 3 and out[5:] == [None] * 5


def test_pav_weights_by_n():
    stats = [None] * 10
    stats[6] = (0.65, 0.9, 300)
    stats[7] = (0.75, 0.3, 100)
    out = models_calibrated._pav(stats)
    pooled = (0.9 * 300 + 0.3 * 100) / 400
    assert out[6][1] == pytest.approx(pooled)
    assert out[7][1] == pytest.approx(pooled)


def test_pav_leaves_monotone_input_alone():
    stats = [None] * 10
    stats[2] = (0.25, 0.2, 100)
    stats[5] = (0.55, 0.5, 100)
    stats[8] = (0.85, 0.9, 100)
    assert models_calibrated._pav(stats) == stats


def test_pav_cascades_across_blocks():
    stats = [None] * 10
    stats[1] = (0.15, 0.6, 100)
    stats[2] = (0.25, 0.5, 100)
    stats[3] = (0.35, 0.1, 100)
    out = models_calibrated._pav(stats)
    pooled = (0.6 + 0.5 + 0.1) / 3
    assert [out[i][1] for i in (1, 2, 3)] == [pytest.approx(pooled)] * 3


def test_non_monotone_buckets_do_not_invert_corrections(conn):
    # Fabricated inversion: the 0.3-bucket hits 90%, the 0.4-bucket only 20%.
    start = _seed(conn, "polymarket", 0.35, hits=135, misses=15)
    _seed(conn, "polymarket", 0.45, hits=30, misses=120, start=start)
    rows = _run([_market(venue_id="lo", baseline=0.35), _market(venue_id="hi", baseline=0.45)])
    by_uid = {r["market_uid"]: r for r in rows}
    lo, hi = by_uid["polymarket:lo"], by_uid["polymarket:hi"]
    assert lo["model_prob"] <= hi["model_prob"]
    # both buckets carry the pooled hit rate
    assert json.loads(lo["detail"])["bin_hit_rate"] == json.loads(hi["detail"])["bin_hit_rate"] == pytest.approx(0.55)


def test_one_calibrated_row_per_market_per_day(conn):
    _seed(conn, "polymarket", 0.9, hits=225, misses=75)
    markets = [_market(baseline=0.9)]
    first = _run(markets)
    assert len(first) == 1
    db.insert_model_prob(conn, first[0])
    second = _run(markets)
    assert second == []
