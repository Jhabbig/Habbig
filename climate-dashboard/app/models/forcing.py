"""Radiative-forcing model — combines atmospheric concentrations into a single
W/m² number that's the climate-relevant equivalent of "how much extra energy
is being trapped vs. pre-industrial."

Uses the simplified IPCC AR5 formulas (Myhre et al. 1998 update). For CO₂
alone the well-known α·ln(C/C₀) holds; for CH₄ and N₂O there's a cross-term
because the absorption bands overlap.

Pre-industrial reference concentrations (year 1750):
  CO₂ = 278 ppm, CH₄ = 722 ppb, N₂O = 270 ppb, SF₆ = 0 ppt.

Outputs an "effective CO₂" — the CO₂ concentration that would produce the
same total radiative forcing on its own. Common framing in the literature
and easier for non-specialists to read than W/m².
"""
from __future__ import annotations

import math
from typing import Optional

PRE_INDUSTRIAL = {"co2_ppm": 278.0, "ch4_ppb": 722.0, "n2o_ppb": 270.0, "sf6_ppt": 0.0}

# IPCC AR5 / Myhre coefficients
_ALPHA_CO2 = 5.35  # W/m² per ln-ratio
_BETA_CH4 = 0.036  # W/m² per √ppb
_GAMMA_N2O = 0.12  # W/m² per √ppb
_DELTA_SF6 = 0.00052  # W/m² per ppt (long-lived halocarbon approximation)


def _ch4_n2o_overlap(m: float, n: float) -> float:
    """The overlap term f(M,N) from Myhre. ppb units."""
    return 0.47 * math.log(1 + 2.01e-5 * (m * n) ** 0.75 + 5.31e-15 * m * (m * n) ** 1.52)


def co2_forcing(ppm: float, ref: float = PRE_INDUSTRIAL["co2_ppm"]) -> float:
    return _ALPHA_CO2 * math.log(ppm / ref)


def ch4_forcing(ppb: float, n2o_ppb: float, ref_ch4: float = PRE_INDUSTRIAL["ch4_ppb"],
                ref_n2o: float = PRE_INDUSTRIAL["n2o_ppb"]) -> float:
    return _BETA_CH4 * (math.sqrt(ppb) - math.sqrt(ref_ch4)) \
           - (_ch4_n2o_overlap(ppb, ref_n2o) - _ch4_n2o_overlap(ref_ch4, ref_n2o))


def n2o_forcing(ppb: float, ch4_ppb: float, ref_n2o: float = PRE_INDUSTRIAL["n2o_ppb"],
                ref_ch4: float = PRE_INDUSTRIAL["ch4_ppb"]) -> float:
    return _GAMMA_N2O * (math.sqrt(ppb) - math.sqrt(ref_n2o)) \
           - (_ch4_n2o_overlap(ref_ch4, ppb) - _ch4_n2o_overlap(ref_ch4, ref_n2o))


def sf6_forcing(ppt: float, ref: float = PRE_INDUSTRIAL["sf6_ppt"]) -> float:
    return _DELTA_SF6 * (ppt - ref)


def effective_co2_ppm(total_forcing_wm2: float, ref: float = PRE_INDUSTRIAL["co2_ppm"]) -> float:
    """Invert the CO₂ formula: what CO₂ concentration alone would produce the
    same total forcing? Useful 'CO₂-equivalent' metric."""
    return ref * math.exp(total_forcing_wm2 / _ALPHA_CO2)


def compute(*, co2: Optional[dict] = None,
            methane: Optional[dict] = None,
            n2o: Optional[dict] = None,
            sf6: Optional[dict] = None) -> Optional[dict]:
    """Build the current breakdown of total anthropogenic GHG forcing.

    Returns a dict with per-gas forcing in W/m², the total, and the
    "effective CO₂ ppm" framing. Any missing input contributes 0 (we just
    flag it in the response so the frontend can warn).
    """
    if not co2 or not co2.get("latest"):
        return None  # CO₂ is the floor — without it the number is meaningless
    co2_ppm = co2["latest"]["ppm"]
    ch4_ppb = methane["latest"]["ppb"] if methane and methane.get("latest") else None
    n2o_ppb = n2o["latest"]["ppb"] if n2o and n2o.get("latest") else None
    sf6_ppt = sf6["latest"]["ppt"] if sf6 and sf6.get("latest") else None

    # For the overlap terms we need both CH4 and N2O. If only one is
    # available we approximate by using its pre-industrial value for the
    # other side of the overlap.
    eff_ch4_ref = ch4_ppb if ch4_ppb is not None else PRE_INDUSTRIAL["ch4_ppb"]
    eff_n2o_ref = n2o_ppb if n2o_ppb is not None else PRE_INDUSTRIAL["n2o_ppb"]

    parts = {"co2_wm2": round(co2_forcing(co2_ppm), 4)}
    if ch4_ppb is not None:
        parts["ch4_wm2"] = round(ch4_forcing(ch4_ppb, eff_n2o_ref), 4)
    if n2o_ppb is not None:
        parts["n2o_wm2"] = round(n2o_forcing(n2o_ppb, eff_ch4_ref), 4)
    if sf6_ppt is not None:
        parts["sf6_wm2"] = round(sf6_forcing(sf6_ppt), 4)

    total = sum(parts.values())
    return {
        **parts,
        "total_wm2": round(total, 4),
        "effective_co2_ppm": round(effective_co2_ppm(total), 2),
        "current_co2_ppm": round(co2_ppm, 2),
        "method": "Myhre et al. 1998 / IPCC AR5 simplified formulas; pre-industrial reference 1750.",
        "have_all_gases": all(g is not None for g in (ch4_ppb, n2o_ppb, sf6_ppt)),
    }
