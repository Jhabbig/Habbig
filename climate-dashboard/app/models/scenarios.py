"""IPCC AR6 SSP scenarios — published projections of CO₂ concentration and
global temperature anomaly out to 2100.

Source: IPCC AR6 WG1 Table SPM.1 (temperature) + SSP database (CO₂). All
temperature anomalies are vs. 1850-1900. GISTEMP — what the dashboard's
temperature card shows — uses a 1951-1980 baseline; the offset between the
two baselines is roughly +0.2°C (i.e. a 1.5°C IPCC anomaly ≈ 1.3°C GISTEMP).
We apply that offset when comparing dashboard readings to scenarios.

These numbers are anchor points; we linearly interpolate between them for
intermediate years. They're not meant to substitute for IPCC's full
projection — just to give the dashboard a "where does our current path sit
between scenarios?" framing.
"""
from __future__ import annotations

from typing import Optional

# Anchor temperature anomaly (°C vs 1850-1900) at the years IPCC reports.
# 2025 anchors are based on observed-plus-near-term-projection consensus.
SCENARIOS_TEMP: dict[str, dict[int, float]] = {
    "SSP1-2.6": {2020: 1.10, 2030: 1.35, 2050: 1.70, 2075: 1.80, 2100: 1.80},
    "SSP2-4.5": {2020: 1.10, 2030: 1.40, 2050: 2.00, 2075: 2.40, 2100: 2.70},
    "SSP3-7.0": {2020: 1.10, 2030: 1.45, 2050: 2.10, 2075: 2.90, 2100: 3.60},
    "SSP5-8.5": {2020: 1.10, 2030: 1.50, 2050: 2.40, 2075: 3.30, 2100: 4.40},
}

# Anchor CO₂ concentration (ppm) at the years IPCC reports.
SCENARIOS_CO2: dict[str, dict[int, float]] = {
    "SSP1-2.6": {2020: 412, 2030: 440, 2050: 445, 2075: 430, 2100: 420},
    "SSP2-4.5": {2020: 412, 2030: 455, 2050: 510, 2075: 555, 2100: 600},
    "SSP3-7.0": {2020: 412, 2030: 465, 2050: 570, 2075: 720, 2100: 870},
    "SSP5-8.5": {2020: 412, 2030: 475, 2050: 600, 2075: 850, 2100: 1135},
}

# Offset between baselines: GISTEMP (1951-1980) is ~0.2°C above 1850-1900.
GISTEMP_TO_PI_OFFSET_C = 0.2


def _interp(anchors: dict[int, float], year: float) -> Optional[float]:
    """Linear interpolation between the nearest anchor years. Returns None
    if ``year`` is outside the anchored range."""
    years = sorted(anchors.keys())
    if not years or year < years[0] or year > years[-1]:
        return None
    for i in range(len(years) - 1):
        y0, y1 = years[i], years[i + 1]
        if y0 <= year <= y1:
            v0, v1 = anchors[y0], anchors[y1]
            t = (year - y0) / (y1 - y0)
            return v0 + (v1 - v0) * t
    return None


def temp_for(scenario: str, year: float) -> Optional[float]:
    if scenario not in SCENARIOS_TEMP:
        return None
    return _interp(SCENARIOS_TEMP[scenario], year)


def co2_for(scenario: str, year: float) -> Optional[float]:
    if scenario not in SCENARIOS_CO2:
        return None
    return _interp(SCENARIOS_CO2[scenario], year)


def closest_temp_scenario(gistemp_anomaly_c: Optional[float], year: int) -> Optional[dict]:
    """Which SSP scenario does the current GISTEMP anomaly most closely match?
    Converts to the 1850-1900 baseline before comparing.

    Returns {scenario, distance_c, scenario_value_c, observed_value_c} or None.
    """
    if gistemp_anomaly_c is None:
        return None
    pi_anomaly = gistemp_anomaly_c + GISTEMP_TO_PI_OFFSET_C
    best = None
    for name in SCENARIOS_TEMP:
        v = temp_for(name, year)
        if v is None:
            continue
        d = abs(pi_anomaly - v)
        if best is None or d < best["distance_c"]:
            best = {
                "scenario": name,
                "distance_c": round(d, 3),
                "scenario_value_c": round(v, 3),
                "observed_value_c": round(pi_anomaly, 3),
            }
    return best


def closest_co2_scenario(current_ppm: Optional[float], year: int) -> Optional[dict]:
    """Which SSP scenario does the current CO₂ concentration most closely
    match?"""
    if current_ppm is None:
        return None
    best = None
    for name in SCENARIOS_CO2:
        v = co2_for(name, year)
        if v is None:
            continue
        d = abs(current_ppm - v)
        if best is None or d < best["distance_ppm"]:
            best = {
                "scenario": name,
                "distance_ppm": round(d, 2),
                "scenario_value_ppm": round(v, 2),
                "observed_value_ppm": round(current_ppm, 2),
            }
    return best


def all_trajectories(metric: str = "temp") -> dict[str, list[dict]]:
    """Year-by-year list of {year, value} for each scenario, suitable for
    plotting. ``metric`` is "temp" (returns °C vs 1850-1900) or "co2"
    (returns ppm)."""
    source = SCENARIOS_TEMP if metric == "temp" else SCENARIOS_CO2
    if metric not in ("temp", "co2"):
        raise ValueError(f"unknown metric: {metric!r}")
    out: dict[str, list[dict]] = {}
    for name, anchors in source.items():
        years = sorted(anchors.keys())
        trajectory = []
        for y in range(years[0], years[-1] + 1, 5):  # every 5 years
            v = _interp(anchors, y)
            if v is not None:
                trajectory.append({"year": y, "value": round(v, 3)})
        out[name] = trajectory
    return out
