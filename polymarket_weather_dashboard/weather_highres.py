"""High-resolution NWP model routing.

The 8-ensemble path in `fetch_multi_model_forecast` covers the global NWP
landscape but caps out at ~13 km horizontal resolution. For markets that
resolve at a specific airport in 6–24 hours, the real alpha lives in
sub-5 km models that resolve thunderstorms, urban heat islands, sea-breeze
fronts, and lake/coastal effects.

Open-Meteo's regular forecast API exposes several of these by name —
`gfs_hrrr` (3 km, North America), `meteofrance_arome_france_hd` (1.3 km,
France), `ukmo_uk_deterministic_2km` (2 km, UK & Ireland),
`icon_d2` (2 km, Germany/Alps/Benelux), `dmi_harmonie_dini_seamless`
(2.5 km, Nordics + DK). We fan out to whichever is applicable for the
station's location.

This module is *only* the routing + fetch layer. Probability scoring,
bias correction, and consensus weighting live elsewhere.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class HighResModel:
    """One high-resolution model with its bounding box.

    Bounding boxes are intentionally a touch loose — Open-Meteo silently
    falls back to a coarser model when a request is just outside, so we
    let it. Better to over-include and let the fetch fail soft than to
    miss a station that's nominally in-region.
    """
    id: str
    name: str
    resolution_km: float
    description: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def covers(self, lat: float, lon: float) -> bool:
        return (self.lat_min <= lat <= self.lat_max
                and self.lon_min <= lon <= self.lon_max)


# Catalog ordered roughly by usefulness — when a station is covered by
# multiple high-res models we fetch them all and treat the union as
# additional consensus members.
HIGHRES_MODELS = (
    HighResModel(
        id="gfs_hrrr", name="HRRR", resolution_km=3.0,
        description="High-Resolution Rapid Refresh (NOAA), CONUS + Alaska + Canada border",
        lat_min=21.0, lat_max=53.0, lon_min=-135.0, lon_max=-60.0,
    ),
    HighResModel(
        id="meteofrance_arome_france_hd", name="AROME-FR-HD", resolution_km=1.3,
        description="Météo-France AROME France HD, 1.3 km hourly",
        lat_min=41.0, lat_max=51.5, lon_min=-6.0, lon_max=10.0,
    ),
    HighResModel(
        id="ukmo_uk_deterministic_2km", name="UKMO 2km", resolution_km=2.0,
        description="UK Met Office UKV 2 km deterministic",
        lat_min=49.0, lat_max=61.0, lon_min=-11.0, lon_max=2.0,
    ),
    HighResModel(
        id="icon_d2", name="ICON-D2", resolution_km=2.2,
        description="DWD ICON-D2 nest, Germany/Alps/Benelux",
        lat_min=43.0, lat_max=58.0, lon_min=-3.5, lon_max=20.0,
    ),
    HighResModel(
        id="dmi_harmonie_dini_seamless", name="HARMONIE-DINI", resolution_km=2.5,
        description="DMI HARMONIE Dini, Nordics + Denmark",
        lat_min=53.0, lat_max=71.0, lon_min=-5.0, lon_max=35.0,
    ),
)


def applicable_models(lat: float, lon: float) -> list[HighResModel]:
    """Return the high-res models whose footprint covers this point."""
    return [m for m in HIGHRES_MODELS if m.covers(lat, lon)]


def fetch_highres_forecast(lat: float, lon: float, date_str: str,
                           model: HighResModel,
                           timeout: int = 12) -> Optional[dict]:
    """Pull the daily max temperature for one high-res model.

    Returns a dict matching the shape produced by `_fetch_ensemble_model`
    in server.py — `mean`, `std`, `min`, `max`, `ensemble`, `source`,
    `members` — so the caller can drop it straight into the existing
    consensus pipeline. Deterministic models have a single member, so
    `std` is set to a sensible default reflecting the model's known MAE.
    """
    try:
        resp = requests.get(OPEN_METEO_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "start_date": date_str, "end_date": date_str,
            "models": model.id,
        }, timeout=timeout, headers={"User-Agent": "narve-weather/1.0"})
        if resp.status_code != 200:
            logger.debug("highres %s status %d for (%s,%s) %s",
                         model.id, resp.status_code, lat, lon, date_str)
            return None
        daily = resp.json().get("daily", {})
        # Open-Meteo encodes the value either under the bare key or with the
        # model suffix appended ("temperature_2m_max_gfs_hrrr"). Look for both.
        temp = None
        for key, vals in daily.items():
            if key.startswith("temperature_2m_max") and vals:
                v = vals[0]
                if v is not None:
                    temp = float(v)
                    break
        if temp is None:
            return None
        # Resolution-aware default sigma: 1km models have ~1.8°F MAE for
        # daily max at 24h; 3km HRRR around 2.2°F. Tracks the "members=1"
        # NWS handler in server.py.
        default_std = 1.8 if model.resolution_km <= 1.5 else 2.2
        return {
            "mean": round(temp, 1),
            "std": default_std,
            "min": round(temp - default_std * 1.5, 1),
            "max": round(temp + default_std * 1.5, 1),
            "ensemble": [temp],
            "source": model.id,
            "model_name": model.name,
            "org": model.description,
            "members": 1,
            "is_resolution_model": False,
            "resolution_km": model.resolution_km,
            "is_highres": True,
        }
    except requests.RequestException as e:
        logger.debug("highres fetch %s failed: %s", model.id, e)
        return None


def fetch_all_highres_forecasts(lat: float, lon: float, date_str: str,
                                models: Optional[list[HighResModel]] = None,
                                stagger_seconds: float = 0.0) -> dict:
    """Fetch every high-res model that covers (lat, lon).

    Returns ``{model_id: forecast_dict}`` for the models that returned data.
    Concurrent fetching happens upstream — this helper stays sequential to
    keep the implementation simple and thread-safe; the caller can
    parallelize if it cares.
    """
    out: dict = {}
    targets = models if models is not None else applicable_models(lat, lon)
    for m in targets:
        fc = fetch_highres_forecast(lat, lon, date_str, m)
        if fc:
            out[m.id] = fc
        if stagger_seconds:
            time.sleep(stagger_seconds)
    return out


def synthesize_highres_member(highres_results: dict) -> Optional[dict]:
    """Collapse a {model_id: forecast} dict into a single pseudo-member
    for the consensus pipeline.

    The consensus in server.py weights by ensemble member count; treating
    each high-res model as a single-member contributor would under-weight
    them next to a 51-member ECMWF EPS. Instead we synthesize a *block*
    member with weight equal to the count of high-res models that
    returned data, mean equal to their average, and std equal to the
    spread between them (with a floor) — this gives high-res a fair seat
    at the table without dominating.
    """
    if not highres_results:
        return None
    means = [v["mean"] for v in highres_results.values() if v.get("mean") is not None]
    if not means:
        return None
    if len(means) == 1:
        v = next(iter(highres_results.values()))
        return {
            "mean": v["mean"],
            "std": v["std"],
            "min": v["min"],
            "max": v["max"],
            "ensemble": list(v["ensemble"]),
            "source": "highres_block",
            "model_name": "Hi-Res block",
            "org": "Aggregated high-res",
            "members": 1,
            "is_resolution_model": False,
            "is_highres": True,
        }
    spread = statistics.stdev(means) if len(means) > 1 else 1.5
    return {
        "mean": round(statistics.mean(means), 1),
        "std": round(max(1.0, spread), 1),
        "min": round(min(means), 1),
        "max": round(max(means), 1),
        "ensemble": list(means),
        "source": "highres_block",
        "model_name": "Hi-Res block",
        "org": f"{len(means)} high-res models averaged",
        "members": len(means),
        "is_resolution_model": False,
        "is_highres": True,
    }
