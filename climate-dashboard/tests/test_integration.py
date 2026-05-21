"""HTTP-mocked end-to-end integration tests.

Verify that every /api/* endpoint returns sensible JSON when the upstream
HTTP fetchers are mocked with our test fixtures. Future URL drift surfaces
here in CI rather than silently in production — we don't actually hit
NOAA/NASA/NSIDC/Polymarket during the test run.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app import cache as cache_module

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, text: str = "", json_data=None, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fake_http_get(url: str, *, timeout=20, params=None):
    """Route by URL substring to the right fixture or stub."""
    if "GLB.Ts+dSST" in url:
        return FakeResponse(text=_load("gistemp_sample.csv"))
    if "co2_mm_mlo" in url:
        return FakeResponse(text=_load("co2_sample.csv"))
    if "ch4_mm_gl" in url:
        return FakeResponse(text=_load("methane_sample.csv"))
    if "n2o_mm_gl" in url:
        return FakeResponse(text=_load("n2o_sample.csv"))
    if "sf6_mm_gl" in url:
        return FakeResponse(text=_load("sf6_sample.csv"))
    if "N_seaice_extent" in url:
        return FakeResponse(text=_load("seaice_sample.csv"))
    if "S_seaice_extent" in url:
        return FakeResponse(text=_load("seaice_sample.csv"))
    if "climatereanalyzer" in url:
        # SST endpoint expects JSON
        return FakeResponse(json_data=[{"name": "2024", "data": [20.0, 20.1, None, None]},
                                       {"name": "1982-2011 mean", "data": [19.5, 19.6, 19.7, 19.8]}])
    if "oni.data" in url:
        return FakeResponse(text=_load("oni_sample.txt"))
    if "owid-co2-data.csv" in url:
        return FakeResponse(text=_load("owid_emissions_sample.csv"))
    if "gamma-api.polymarket.com" in url:
        # Polymarket events — empty list is a valid response shape
        return FakeResponse(json_data=[])
    return None


@pytest.fixture
def client():
    cache_module.clear()
    with patch("app.http.get", side_effect=_fake_http_get):
        from server import app
        with app.test_client() as c:
            yield c
    cache_module.clear()


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["service"] == "climate-dashboard"


def test_methodology_endpoint_lists_all_models(client):
    r = client.get("/api/methodology")
    assert r.status_code == 200
    body = r.get_json()
    ids = [m["id"] for m in body["models"]]
    # All the projection models, plus market scoring, kelly, and highlights
    for expected in (
        "temperature_year_end_projection",
        "co2_year_end_projection",
        "methane_year_end_projection",
        "n2o_year_end_projection",
        "arctic_min_projection",
        "antarctic_min_projection",
        "market_scoring",
        "highlights",
        "kelly_position_sizing",
    ):
        assert expected in ids, f"missing methodology entry: {expected}"


def test_temperature_endpoint_with_mocked_upstream(client):
    r = client.get("/api/temperature")
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"].startswith("NASA GISTEMP")
    assert len(body["monthly"]) > 0
    assert body["projection"] is not None


def test_co2_endpoint_with_mocked_upstream(client):
    r = client.get("/api/co2")
    assert r.status_code == 200
    body = r.get_json()
    assert body["units"] == "ppm"
    assert body["projection"]["projected_year_end_ppm"] > 0


def test_sf6_endpoint_with_mocked_upstream(client):
    r = client.get("/api/sf6")
    assert r.status_code == 200
    body = r.get_json()
    assert body["units"] == "ppt"
    assert body["projection"] is not None
    assert body["projection"]["residual_std_ppt"] >= 0.05


def test_emissions_endpoint_with_mocked_upstream(client):
    r = client.get("/api/emissions")
    assert r.status_code == 200
    body = r.get_json()
    assert body["latest_year"] == 2022
    assert body["top_emitters"][0]["iso"] == "CHN"
    assert body["global"]["global_co2_mt"] > 0


def test_forcing_endpoint_with_mocked_upstream(client):
    r = client.get("/api/forcing")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_wm2"] > 0
    assert body["effective_co2_ppm"] > 0
    assert "co2_wm2" in body


def test_summary_endpoint_with_mocked_upstream(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.get_json()
    for key in ("gistemp", "co2", "methane", "n2o", "sf6", "forcing", "sea_ice", "regime"):
        assert key in body, f"summary missing block: {key}"
    # Calibration block populated where backtest can run
    assert body["gistemp"].get("calibration") is not None


def test_highlights_endpoint_with_mocked_upstream(client):
    r = client.get("/api/highlights")
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body["items"], list)


def test_backtest_endpoint_with_mocked_upstream(client):
    r = client.get("/api/backtest")
    assert r.status_code == 200
    body = r.get_json()
    assert "calibration" in body
    # CO2 fixture only has 2024+; backtest needs prior years too, so this
    # may legitimately be empty. Just verify the shape is correct.
    assert isinstance(body["gistemp"], list)


def test_markets_endpoint_with_empty_polymarket(client):
    # The fake polymarket response is an empty list. The endpoint should
    # still return 200 with markets=[].
    r = client.get("/api/markets")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 0
    assert body["markets"] == []
    # Projections should still be populated from the other fetchers
    assert body["co2_projection"] is not None
    assert body["n2o_projection"] is not None


def test_methodology_page_renders(client):
    r = client.get("/methodology")
    assert r.status_code == 200
    # It's static HTML
    assert b"<title>Methodology" in r.data


def test_index_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<title>Climate Change" in r.data


def test_upstream_failure_yields_503(client):
    # Bypass the global patch and have all HTTP calls return None
    cache_module.clear()
    with patch("app.http.get", return_value=None):
        r = client.get("/api/temperature")
        assert r.status_code == 503
        assert "error" in r.get_json()
