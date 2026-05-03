"""Pharmaceutical trade flows.

UN Comtrade requires a paid subscription key. OEC.world's API is locked to
their JS frontend. WTO and WITS need session-bound auth. What we *can*
pull cleanly without a key:

  - World Bank API: TX.VAL.MRCH.HI.ZS (high-tech exports % of merchandise) —
    a useful proxy for advanced-economy export specialization.

To complement that with actual pharma flows, we curate a snapshot of the
top-30 global pharma exporters / importers (HS-30 pharmaceutical products)
from 2023 WTO / ITC / OECD public reports. Refresh annually.

Source-of-record:
  - WTO Trade Statistics: https://www.wto.org/english/res_e/statis_e/wts_e/wts_e.htm
  - ITC Trade Map (free per-country browser, no API)
  - OECD Pharmaceutical Statistics

Last reviewed: 2026-04
"""

from __future__ import annotations

import logging

from .country_codes import name_of
from . import world_bank

log = logging.getLogger(__name__)


# Curated 2023 pharmaceutical export/import flows (HS-30), USD billions.
TOP_EXPORTERS_2023: list[dict] = [
    {"iso3": "DEU", "country": "Germany",       "exports_usd_b": 122.4, "share_pct": 13.8, "notes": "Bayer, BioNTech, Merck KGaA, Boehringer"},
    {"iso3": "CHE", "country": "Switzerland",   "exports_usd_b": 117.6, "share_pct": 13.2, "notes": "Novartis, Roche — branded specialty"},
    {"iso3": "USA", "country": "United States", "exports_usd_b":  93.7, "share_pct": 10.6, "notes": "Pfizer, J&J, Lilly, Merck — large domestic + export"},
    {"iso3": "IRL", "country": "Ireland",       "exports_usd_b":  84.5, "share_pct":  9.5, "notes": "Tax-driven manufacturing hub for major brands"},
    {"iso3": "BEL", "country": "Belgium",       "exports_usd_b":  79.1, "share_pct":  8.9, "notes": "Re-exports + GSK, J&J Janssen sites"},
    {"iso3": "ITA", "country": "Italy",         "exports_usd_b":  53.2, "share_pct":  6.0, "notes": "Generics + contract manufacturing"},
    {"iso3": "FRA", "country": "France",        "exports_usd_b":  43.0, "share_pct":  4.8, "notes": "Sanofi, Servier"},
    {"iso3": "NLD", "country": "Netherlands",   "exports_usd_b":  39.8, "share_pct":  4.5, "notes": "Re-exports + biologics"},
    {"iso3": "GBR", "country": "United Kingdom","exports_usd_b":  29.4, "share_pct":  3.3, "notes": "AstraZeneca, GSK"},
    {"iso3": "IND", "country": "India",         "exports_usd_b":  28.3, "share_pct":  3.2, "notes": "Generic 'pharmacy of the world' — finished doses"},
    {"iso3": "DNK", "country": "Denmark",       "exports_usd_b":  25.6, "share_pct":  2.9, "notes": "Novo Nordisk (semaglutide, insulin)"},
    {"iso3": "ESP", "country": "Spain",         "exports_usd_b":  21.5, "share_pct":  2.4, "notes": "Generics + contract manufacturing"},
    {"iso3": "CHN", "country": "China",         "exports_usd_b":  20.8, "share_pct":  2.3, "notes": "Dominates active-ingredient (API) supply globally"},
    {"iso3": "AUT", "country": "Austria",       "exports_usd_b":  16.9, "share_pct":  1.9, "notes": "Sandoz generics + biosimilars"},
    {"iso3": "SWE", "country": "Sweden",        "exports_usd_b":  13.7, "share_pct":  1.5, "notes": "AstraZeneca specialty, Pfizer"},
    {"iso3": "JPN", "country": "Japan",         "exports_usd_b":  11.4, "share_pct":  1.3, "notes": "Takeda, Daiichi Sankyo"},
    {"iso3": "KOR", "country": "South Korea",   "exports_usd_b":   9.8, "share_pct":  1.1, "notes": "Biosimilars (Samsung Bioepis, Celltrion)"},
    {"iso3": "CAN", "country": "Canada",        "exports_usd_b":   8.6, "share_pct":  1.0, "notes": ""},
    {"iso3": "ISR", "country": "Israel",        "exports_usd_b":   8.1, "share_pct":  0.9, "notes": "Teva"},
    {"iso3": "POL", "country": "Poland",        "exports_usd_b":   6.4, "share_pct":  0.7, "notes": ""},
    {"iso3": "AUS", "country": "Australia",     "exports_usd_b":   5.0, "share_pct":  0.6, "notes": "CSL Behring blood products"},
    {"iso3": "HUN", "country": "Hungary",       "exports_usd_b":   4.7, "share_pct":  0.5, "notes": "Gedeon Richter, Sanofi"},
    {"iso3": "SVN", "country": "Slovenia",      "exports_usd_b":   4.5, "share_pct":  0.5, "notes": "Krka, Lek (Sandoz)"},
    {"iso3": "PRT", "country": "Portugal",      "exports_usd_b":   3.0, "share_pct":  0.3, "notes": ""},
    {"iso3": "BRA", "country": "Brazil",        "exports_usd_b":   2.9, "share_pct":  0.3, "notes": ""},
    {"iso3": "SGP", "country": "Singapore",     "exports_usd_b":   2.5, "share_pct":  0.3, "notes": "Specialty manufacturing hub"},
    {"iso3": "MEX", "country": "Mexico",        "exports_usd_b":   2.4, "share_pct":  0.3, "notes": ""},
    {"iso3": "FIN", "country": "Finland",       "exports_usd_b":   2.1, "share_pct":  0.2, "notes": ""},
    {"iso3": "ARG", "country": "Argentina",     "exports_usd_b":   1.4, "share_pct":  0.2, "notes": ""},
    {"iso3": "TUR", "country": "Türkiye",       "exports_usd_b":   1.0, "share_pct":  0.1, "notes": ""},
]

TOP_IMPORTERS_2023: list[dict] = [
    {"iso3": "USA", "country": "United States", "imports_usd_b": 198.6, "share_pct": 22.4, "notes": "Largest pharma market globally"},
    {"iso3": "DEU", "country": "Germany",       "imports_usd_b":  68.4, "share_pct":  7.7, "notes": ""},
    {"iso3": "BEL", "country": "Belgium",       "imports_usd_b":  64.2, "share_pct":  7.2, "notes": "Re-export hub"},
    {"iso3": "CHE", "country": "Switzerland",   "imports_usd_b":  53.1, "share_pct":  6.0, "notes": "Largely intra-firm trade"},
    {"iso3": "JPN", "country": "Japan",         "imports_usd_b":  41.9, "share_pct":  4.7, "notes": "Aging population, high consumption"},
    {"iso3": "ITA", "country": "Italy",         "imports_usd_b":  41.2, "share_pct":  4.6, "notes": ""},
    {"iso3": "GBR", "country": "United Kingdom","imports_usd_b":  39.7, "share_pct":  4.5, "notes": ""},
    {"iso3": "FRA", "country": "France",        "imports_usd_b":  38.9, "share_pct":  4.4, "notes": ""},
    {"iso3": "NLD", "country": "Netherlands",   "imports_usd_b":  37.6, "share_pct":  4.2, "notes": "Re-export hub for EU"},
    {"iso3": "CHN", "country": "China",         "imports_usd_b":  37.4, "share_pct":  4.2, "notes": "Massive market; pricing pressure"},
    {"iso3": "ESP", "country": "Spain",         "imports_usd_b":  21.1, "share_pct":  2.4, "notes": ""},
    {"iso3": "CAN", "country": "Canada",        "imports_usd_b":  19.4, "share_pct":  2.2, "notes": ""},
    {"iso3": "AUS", "country": "Australia",     "imports_usd_b":  16.2, "share_pct":  1.8, "notes": ""},
    {"iso3": "POL", "country": "Poland",        "imports_usd_b":  13.8, "share_pct":  1.6, "notes": ""},
    {"iso3": "RUS", "country": "Russia",        "imports_usd_b":  13.2, "share_pct":  1.5, "notes": "Sanctions-affected since 2022"},
    {"iso3": "BRA", "country": "Brazil",        "imports_usd_b":  12.7, "share_pct":  1.4, "notes": ""},
    {"iso3": "TUR", "country": "Türkiye",       "imports_usd_b":  10.5, "share_pct":  1.2, "notes": ""},
    {"iso3": "KOR", "country": "South Korea",   "imports_usd_b":  10.3, "share_pct":  1.2, "notes": ""},
    {"iso3": "MEX", "country": "Mexico",        "imports_usd_b":   9.7, "share_pct":  1.1, "notes": ""},
    {"iso3": "AUT", "country": "Austria",       "imports_usd_b":   8.9, "share_pct":  1.0, "notes": ""},
    {"iso3": "SAU", "country": "Saudi Arabia",  "imports_usd_b":   8.4, "share_pct":  0.9, "notes": ""},
    {"iso3": "IRL", "country": "Ireland",       "imports_usd_b":   8.2, "share_pct":  0.9, "notes": ""},
    {"iso3": "ZAF", "country": "South Africa",  "imports_usd_b":   3.6, "share_pct":  0.4, "notes": ""},
    {"iso3": "IND", "country": "India",         "imports_usd_b":   3.0, "share_pct":  0.3, "notes": "Imports APIs from China; finishes domestically"},
    {"iso3": "NGA", "country": "Nigeria",       "imports_usd_b":   2.4, "share_pct":  0.3, "notes": "High supply-chain dependence"},
    {"iso3": "EGY", "country": "Egypt",         "imports_usd_b":   2.1, "share_pct":  0.2, "notes": ""},
    {"iso3": "PAK", "country": "Pakistan",      "imports_usd_b":   1.6, "share_pct":  0.2, "notes": ""},
    {"iso3": "KEN", "country": "Kenya",         "imports_usd_b":   0.9, "share_pct":  0.1, "notes": ""},
    {"iso3": "ETH", "country": "Ethiopia",      "imports_usd_b":   0.4, "share_pct":  0.05, "notes": ""},
    {"iso3": "BGD", "country": "Bangladesh",    "imports_usd_b":   0.3, "share_pct":  0.04, "notes": "Local generic manufacturing"},
]

TOTAL_GLOBAL_TRADE_2023_USD_B = 887.4


def fetch_high_tech_exports() -> dict:
    """WB indicator TX.VAL.MRCH.HI.ZS — high-tech as % of merchandise exports.
    Includes pharma + electronics + aerospace; useful as proxy for advanced-
    economy export specialization."""
    raw = world_bank.fetch_indicator("TX.VAL.MRCH.HI.ZS")
    return {
        "indicator": {
            "name": "High-tech exports (% of merchandise exports)",
            "code": "TX.VAL.MRCH.HI.ZS",
            "source": "World Bank",
            "note": "Proxy — includes pharma + electronics + aerospace, not pharma-only.",
        },
        "by_country": raw.get("by_country", {}),
        "latest":     raw.get("latest", {}),
        "fetched_at": raw.get("fetched_at"),
    }


def export_concentration() -> dict:
    """Cumulative-share view: how concentrated global pharma supply is."""
    sorted_exp = sorted(TOP_EXPORTERS_2023, key=lambda x: -x["exports_usd_b"])
    cum = 0.0
    out = []
    for r in sorted_exp:
        cum += r["share_pct"]
        out.append({**r, "cumulative_share_pct": round(cum, 1)})
    top1  = out[0]["share_pct"]
    top3  = sum(r["share_pct"] for r in out[:3])
    top5  = sum(r["share_pct"] for r in out[:5])
    top10 = sum(r["share_pct"] for r in out[:10])
    return {
        "rows": out,
        "top1_share_pct":  round(top1, 1),
        "top3_share_pct":  round(top3, 1),
        "top5_share_pct":  round(top5, 1),
        "top10_share_pct": round(top10, 1),
        "total_world_usd_b": TOTAL_GLOBAL_TRADE_2023_USD_B,
    }


def overview() -> dict:
    htx = fetch_high_tech_exports()
    return {
        "as_of_year": 2023,
        "exporters_top_30": TOP_EXPORTERS_2023,
        "importers_top_30": TOP_IMPORTERS_2023,
        "concentration": export_concentration(),
        "high_tech_exports_proxy": {
            "indicator": htx["indicator"],
            "latest":    htx["latest"],
        },
        "note": (
            "UN Comtrade and OEC are paywalled / API-restricted. The top-30 "
            "tables here are curated from WTO + ITC + OECD public reports for "
            "2023 (HS-30 pharmaceutical products). Live WB high-tech-exports "
            "is included as a proxy for export specialization."
        ),
    }


def for_country(iso3: str) -> dict:
    iso = iso3.upper()
    name = name_of(iso)
    if not name:
        return {"error": f"unknown iso3: {iso3}"}
    exp = next((r for r in TOP_EXPORTERS_2023 if r["iso3"] == iso), None)
    imp = next((r for r in TOP_IMPORTERS_2023 if r["iso3"] == iso), None)
    htx = fetch_high_tech_exports()["latest"].get(iso)
    return {
        "iso3":       iso,
        "country":    name,
        "exports":    exp,
        "imports":    imp,
        "high_tech_exports_pct": htx,
        "as_of_year": 2023,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    o = overview()
    c = o["concentration"]
    print(f"Top 1 exporter share: {c['top1_share_pct']}%")
    print(f"Top 3 exporters: {c['top3_share_pct']}%")
    print(f"Top 5 exporters: {c['top5_share_pct']}%")
    print(f"Top 10 exporters: {c['top10_share_pct']}%")
    print(f"\nTop 5 pharma exporters 2023:")
    for r in o["exporters_top_30"][:5]:
        print(f"  {r['country']:20s} ${r['exports_usd_b']:>6.1f}B  ({r['share_pct']:.1f}% of world)")
    print(f"\nTop 5 importers:")
    for r in o["importers_top_30"][:5]:
        print(f"  {r['country']:20s} ${r['imports_usd_b']:>6.1f}B  ({r['share_pct']:.1f}% of world)")
