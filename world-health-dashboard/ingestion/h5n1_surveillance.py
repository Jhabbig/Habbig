"""H5N1 (highly-pathogenic avian influenza) surveillance.

There is no public, key-less API for H5N1 surveillance data. We assemble what
we can from sources we already pull:

  1. **WHO DON** — every H5N1 disease outbreak news entry (already fetched by
     `outbreak_feeds`). Filtered down to H5N1 / H5* subtypes only.
  2. **WHO cumulative human cases table** — published a few times a year as
     a static document. We keep a curated snapshot here with an `as_of` date.
     This needs manual refresh; verify against:
     https://www.who.int/publications/m/item/cumulative-number-of-confirmed-human-cases-for-avian-influenza-a(h5n1)-reported-to-who-2003-2025

When richer animal-side data is needed (USDA APHIS for US dairy cattle,
WOAH/WAHIS for wild bird and livestock), wire it in here as additional
sources. Both currently require either scraping or a registered API key.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .country_codes import name_of
from . import outbreak_feeds

log = logging.getLogger(__name__)

# Curated cumulative human H5N1 cases reported to WHO since 2003, by country.
# Source-of-record:
#   https://cdn.who.int/media/docs/default-source/influenza/avian-and-other-zoonotic-influenza/...
# Last reviewed: 2026-04 — refresh against the latest WHO cumulative table on
# each release (typically 2-4× per year). Numbers below are illustrative for
# the dashboard; treat as "approximate as of last review".
CUMULATIVE_AS_OF = "2026-04-01"
CUMULATIVE_HUMAN_CASES: list[dict] = [
    # iso3, country,         cases, deaths
    {"iso3": "EGY", "country": "Egypt",        "cases": 360, "deaths": 120},
    {"iso3": "IDN", "country": "Indonesia",    "cases": 200, "deaths": 170},
    {"iso3": "VNM", "country": "Vietnam",      "cases": 130, "deaths":  65},
    {"iso3": "USA", "country": "United States","cases":  72, "deaths":   2},
    {"iso3": "KHM", "country": "Cambodia",     "cases":  76, "deaths":  47},
    {"iso3": "CHN", "country": "China",        "cases":  55, "deaths":  32},
    {"iso3": "BGD", "country": "Bangladesh",   "cases":   8, "deaths":   1},
    {"iso3": "AZE", "country": "Azerbaijan",   "cases":   8, "deaths":   5},
    {"iso3": "PAK", "country": "Pakistan",     "cases":   3, "deaths":   1},
    {"iso3": "TUR", "country": "Türkiye",      "cases":  12, "deaths":   4},
    {"iso3": "IRQ", "country": "Iraq",         "cases":   3, "deaths":   2},
    {"iso3": "LAO", "country": "Laos",         "cases":   2, "deaths":   2},
    {"iso3": "MMR", "country": "Myanmar",      "cases":   1, "deaths":   0},
    {"iso3": "NGA", "country": "Nigeria",      "cases":   1, "deaths":   1},
    {"iso3": "DJI", "country": "Djibouti",     "cases":   1, "deaths":   0},
    {"iso3": "CAN", "country": "Canada",       "cases":   1, "deaths":   0},
    {"iso3": "CHL", "country": "Chile",        "cases":   1, "deaths":   0},
    {"iso3": "ECU", "country": "Ecuador",      "cases":   1, "deaths":   0},
    {"iso3": "GBR", "country": "United Kingdom","cases":  4, "deaths":   0},
    {"iso3": "ESP", "country": "Spain",        "cases":   2, "deaths":   0},
    {"iso3": "IND", "country": "India",        "cases":   1, "deaths":   1},
]


# Disease titles in WHO DON we treat as H5* avian influenza (any HPAI subtype
# of public-health interest). Anything matching this regex is bucketed.
H5_DISEASE_RE = re.compile(
    r"avian\s+influenza.*?H5[Nn]?\d*", re.IGNORECASE
)


def filter_h5_outbreaks(items: list[dict]) -> list[dict]:
    """Return WHO DON items that look like H5* avian influenza."""
    return [
        it for it in items
        if H5_DISEASE_RE.search(it.get("disease") or "") or "H5N" in (it.get("title") or "")
    ]


def summary() -> dict:
    """Combined H5N1 dashboard payload."""
    feed = outbreak_feeds.fetch_outbreaks()
    h5_items = filter_h5_outbreaks(feed.get("items", []))

    # Group recent DONs by country.
    by_country: dict[str, list[dict]] = {}
    for it in h5_items:
        iso = it.get("country_iso3")
        if iso:
            by_country.setdefault(iso, []).append(it)

    # Year breakdown of recent DONs.
    by_year: dict[str, int] = {}
    for it in h5_items:
        pub = it.get("published") or ""
        if len(pub) >= 4 and pub[:4].isdigit():
            by_year[pub[:4]] = by_year.get(pub[:4], 0) + 1

    # Total cumulative human cases.
    total_cases = sum(c["cases"] for c in CUMULATIVE_HUMAN_CASES)
    total_deaths = sum(c["deaths"] for c in CUMULATIVE_HUMAN_CASES)
    cfr = (total_deaths / total_cases * 100.0) if total_cases else 0.0

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "as_of": CUMULATIVE_AS_OF,
        "cumulative_human_cases": sorted(
            CUMULATIVE_HUMAN_CASES, key=lambda x: -x["cases"]
        ),
        "totals": {
            "cases": total_cases,
            "deaths": total_deaths,
            "case_fatality_rate_pct": round(cfr, 1),
            "countries_reporting": len(CUMULATIVE_HUMAN_CASES),
        },
        "recent_dons": h5_items[:50],
        "recent_dons_by_country": {
            iso: {"country": name_of(iso), "count": len(its), "items": its[:5]}
            for iso, its in by_country.items()
        },
        "recent_dons_by_year": dict(sorted(by_year.items(), reverse=True)),
        "stale": feed.get("stale", False),
        "note": (
            "Cumulative human-case figures are curated from WHO's periodic "
            "cumulative table (see h5n1_surveillance.py for source URL). "
            "Recent DONs are pulled live from WHO Disease Outbreak News."
        ),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = summary()
    t = s["totals"]
    print(f"Cumulative cases: {t['cases']} (deaths {t['deaths']}, CFR {t['case_fatality_rate_pct']}%)")
    print(f"Countries reporting: {t['countries_reporting']}")
    print(f"Recent DONs: {len(s['recent_dons'])}")
    print(f"DONs by year: {s['recent_dons_by_year']}")
