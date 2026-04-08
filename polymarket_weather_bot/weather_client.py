"""Weather forecast client — Open-Meteo GFS ensemble + NWS fallback."""

from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS_API_URL = "https://api.weather.gov"


@dataclass
class ForecastResult:
    """Forecast data for a specific location and date."""
    city: str
    icao: str
    target_date: datetime
    mean_temp_f: float
    std_temp_f: float
    min_temp_f: float
    max_temp_f: float
    source: str
    raw_ensemble: list


async def fetch_open_meteo_ensemble(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    target_date: datetime,
) -> Optional[ForecastResult]:
    """Fetch GFS ensemble forecast from Open-Meteo."""
    date_str = target_date.strftime("%Y-%m-%d")

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "start_date": date_str,
        "end_date": date_str,
        "models": "gfs_seamless",
    }

    try:
        async with session.get(OPEN_METEO_ENSEMBLE_URL, params=params) as resp:
            if resp.status != 200:
                logger.warning("Open-Meteo ensemble returned %d", resp.status)
                return None

            data = await resp.json()
            daily = data.get("daily", {})
            ensemble_temps = []

            for key, values in daily.items():
                if key.startswith("temperature_2m_max") and values:
                    for v in values:
                        if v is not None:
                            ensemble_temps.append(float(v))

            if not ensemble_temps:
                return None

            mean_temp = statistics.mean(ensemble_temps)
            std_temp = statistics.stdev(ensemble_temps) if len(ensemble_temps) > 1 else 3.0
            std_temp = max(std_temp, 2.0)

            return ForecastResult(
                city="", icao="", target_date=target_date,
                mean_temp_f=mean_temp, std_temp_f=std_temp,
                min_temp_f=min(ensemble_temps), max_temp_f=max(ensemble_temps),
                source="open-meteo-ensemble", raw_ensemble=ensemble_temps,
            )

    except Exception as e:
        logger.error("Open-Meteo ensemble error: %s", e)
        return None


async def fetch_open_meteo_deterministic(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    target_date: datetime,
) -> Optional[ForecastResult]:
    """Fallback: deterministic GFS forecast with assumed uncertainty."""
    date_str = target_date.strftime("%Y-%m-%d")

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "start_date": date_str,
        "end_date": date_str,
    }

    try:
        async with session.get(OPEN_METEO_URL, params=params) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            daily = data.get("daily", {})
            temps = daily.get("temperature_2m_max", [])

            if not temps or temps[0] is None:
                return None

            mean_temp = float(temps[0])
            hours_ahead = (target_date - datetime.now(timezone.utc)).total_seconds() / 3600
            base_std = 2.5
            std_temp = base_std + 0.03 * max(0, hours_ahead)
            std_temp = min(std_temp, 6.0)

            return ForecastResult(
                city="", icao="", target_date=target_date,
                mean_temp_f=mean_temp, std_temp_f=std_temp,
                min_temp_f=mean_temp - 2 * std_temp,
                max_temp_f=mean_temp + 2 * std_temp,
                source="open-meteo-deterministic", raw_ensemble=[mean_temp],
            )

    except Exception as e:
        logger.error("Open-Meteo deterministic error: %s", e)
        return None


async def fetch_nws_forecast(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    target_date: datetime,
) -> Optional[ForecastResult]:
    """Fetch NWS forecast for US locations via api.weather.gov."""
    try:
        contact_email = os.getenv("NWS_CONTACT_EMAIL", "contact@example.com")
        headers = {"User-Agent": f"PolymarketWeatherBot/1.0 ({contact_email})"}
        points_url = f"{NWS_API_URL}/points/{lat:.4f},{lon:.4f}"

        async with session.get(points_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            points_data = await resp.json()

        forecast_url = points_data.get("properties", {}).get("forecastHourly")
        if not forecast_url:
            return None

        async with session.get(forecast_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            forecast_data = await resp.json()

        periods = forecast_data.get("properties", {}).get("periods", [])
        if not periods:
            return None

        target_date_str = target_date.strftime("%Y-%m-%d")
        day_temps = []
        for period in periods:
            start = period.get("startTime", "")
            if target_date_str in start:
                temp = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if temp is not None:
                    temp_f = float(temp) if unit == "F" else float(temp) * 9 / 5 + 32
                    day_temps.append(temp_f)

        if not day_temps:
            return None

        max_temp = max(day_temps)
        std_temp = 3.0

        return ForecastResult(
            city="", icao="", target_date=target_date,
            mean_temp_f=max_temp, std_temp_f=std_temp,
            min_temp_f=max_temp - 2 * std_temp,
            max_temp_f=max_temp + 2 * std_temp,
            source="nws", raw_ensemble=[max_temp],
        )

    except Exception as e:
        logger.error("NWS forecast error: %s", e)
        return None


async def get_forecast(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    target_date: datetime,
    city: str = "",
    icao: str = "",
) -> Optional[ForecastResult]:
    """Get the best available forecast, trying ensemble first."""
    result = await fetch_open_meteo_ensemble(session, lat, lon, target_date)

    if result is None:
        result = await fetch_open_meteo_deterministic(session, lat, lon, target_date)

    if result is None and -130 < lon < -60 and 24 < lat < 50:
        result = await fetch_nws_forecast(session, lat, lon, target_date)

    if result:
        result.city = city
        result.icao = icao

    return result
