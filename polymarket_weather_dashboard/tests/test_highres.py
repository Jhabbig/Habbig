"""Tests for high-resolution model routing + block synthesis.

The fetch path itself hits Open-Meteo so we don't exercise it here;
those tests would be flaky and slow. The routing logic and the block
synthesizer are pure and important — those are what these cases pin
down.
"""

from weather_highres import (
    HIGHRES_MODELS,
    HighResModel,
    applicable_models,
    synthesize_highres_member,
)


# ─── Region routing ───────────────────────────────────────────────────────────

def test_hrrr_covers_us_cities():
    nyc = applicable_models(40.7772, -73.8726)
    assert any(m.id == "gfs_hrrr" for m in nyc)
    chicago = applicable_models(41.97, -87.91)
    assert any(m.id == "gfs_hrrr" for m in chicago)
    miami = applicable_models(25.79, -80.29)
    assert any(m.id == "gfs_hrrr" for m in miami)


def test_arome_covers_paris():
    paris = applicable_models(48.72, 2.38)
    assert any(m.id == "meteofrance_arome_france_hd" for m in paris)


def test_ukmo_covers_london():
    london = applicable_models(51.5, -0.05)
    assert any(m.id == "ukmo_uk_deterministic_2km" for m in london)


def test_icon_d2_covers_munich():
    munich = applicable_models(48.35, 11.78)
    assert any(m.id == "icon_d2" for m in munich)


def test_no_highres_for_sydney():
    sydney = applicable_models(-33.95, 151.18)
    assert sydney == []


def test_no_highres_for_tokyo():
    tokyo = applicable_models(35.55, 139.78)
    assert tokyo == []


def test_no_highres_for_buenos_aires():
    ba = applicable_models(-34.56, -58.42)
    assert ba == []


def test_overlapping_regions_returns_all_covering_models():
    # Brussels area: ICON-D2 covers it
    brussels = applicable_models(50.85, 4.35)
    assert any(m.id == "icon_d2" for m in brussels)


def test_model_resolution_present():
    for m in HIGHRES_MODELS:
        assert m.resolution_km < 5.0  # all are sub-5km
        assert isinstance(m, HighResModel)


# ─── Block synthesis ──────────────────────────────────────────────────────────

def _fake(model_id: str, mean: float) -> dict:
    return {"mean": mean, "std": 2.0, "min": mean - 3, "max": mean + 3,
            "ensemble": [mean], "source": model_id, "members": 1}


def test_synthesize_block_with_three_models():
    results = {
        "gfs_hrrr": _fake("gfs_hrrr", 75.0),
        "icon_d2": _fake("icon_d2", 76.0),
        "ukmo_uk_deterministic_2km": _fake("ukmo_uk_deterministic_2km", 74.0),
    }
    block = synthesize_highres_member(results)
    assert block is not None
    assert block["mean"] == 75.0
    assert block["members"] == 3
    assert block["std"] >= 1.0


def test_synthesize_block_single_model_pass_through():
    results = {"gfs_hrrr": _fake("gfs_hrrr", 80.0)}
    block = synthesize_highres_member(results)
    assert block is not None
    assert block["mean"] == 80.0
    assert block["members"] == 1


def test_synthesize_block_empty_returns_none():
    assert synthesize_highres_member({}) is None
    assert synthesize_highres_member({"gfs_hrrr": {"mean": None}}) is None
