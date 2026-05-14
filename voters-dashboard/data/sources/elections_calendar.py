#!/usr/bin/env python3
"""
Elections calendar ETL.

There is no clean public API for "every election in every country."
Best public references:
  - IFES Election Guide      - https://www.electionguide.org/
  - International IDEA       - https://www.idea.int/
  - Wikipedia: "Elections in YYYY" lists

All three serve HTML, not JSON. For the MVP we hand-curate a calendar
covering all major national elections worldwide 2026-2028 plus a tail
of significant sub-nationals. Re-curate quarterly until an IFES
scraper exists.

Sources for each entry are inline as `stakes:` notes; cross-check
against country YAML elections blocks to avoid double-counting.

Cadence: daily (the dashboard reloads its in-memory cache every 60s).
The script is idempotent - running it daily is effectively a snapshot
refresh once the curated list changes.

Output: `data/cache/elections_calendar.json` (hot path) +
        `data/snapshot_elections_calendar.yaml` (committed fallback).

Usage:
    python3 data/sources/elections_calendar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import write_overlay  # noqa: E402

# Curated calendar of 2026-2028 major elections (national + significant
# sub-nationals). Entries supplement what's already in countries.yaml under
# each country's `elections:` block; server-side merge dedupes by (date, type).
#
# Schema: {date: YYYY-MM-DD or "TBD-YYYY", type: short label, stakes: 1-line note}.
SUPPLEMENTARY_BY_ISO = {
    "USA": [
        {"date": "2026-11-03", "type": "Midterms - House + 1/3 Senate",
         "stakes": "House control; Senate map favors GOP; 36 governors"},
        {"date": "2026-11-03", "type": "Gubernatorial races (36 states)",
         "stakes": "Includes CA, FL, TX, NY, GA, OH, PA"},
        {"date": "2028-11-07", "type": "Presidential general",
         "stakes": "Open seat both parties; Senate class III; House"},
    ],
    "CAN": [
        {"date": "TBD-2026", "type": "Federal general (writ TBD)",
         "stakes": "Carney Liberal minority; CPC challenge; Quebec gains"},
    ],
    "MEX": [
        {"date": "2027-06-06", "type": "Midterm Chamber of Deputies",
         "stakes": "Sheinbaum 2nd half; Morena supermajority test"},
    ],
    "BRA": [
        {"date": "2026-10-04", "type": "Presidential first round + Congress",
         "stakes": "Lula re-election bid vs Bolsonaro proxy; STF tension"},
        {"date": "2026-10-25", "type": "Presidential runoff (if needed)",
         "stakes": "Likely two-round race"},
    ],
    "ARG": [
        {"date": "2027-10-24", "type": "Presidential general",
         "stakes": "Milei re-election test; austerity vote"},
    ],
    "CHL": [
        {"date": "2025-11-16", "type": "Presidential first round",
         "stakes": "Boric successor; right resurgence"},
    ],
    "COL": [
        {"date": "2026-05-31", "type": "Presidential first round",
         "stakes": "Petro successor; left vs right reset"},
        {"date": "2026-06-21", "type": "Presidential runoff",
         "stakes": "Typically required"},
    ],
    "PER": [
        {"date": "2026-04-12", "type": "Presidential general",
         "stakes": "Boluarte successor; first round in 5-year cycle"},
    ],
    "ECU": [
        {"date": "2027-02-07", "type": "Presidential general",
         "stakes": "Noboa re-election bid; security agenda"},
    ],
    "BOL": [
        {"date": "2025-08-17", "type": "Presidential general",
         "stakes": "MAS vs anti-MAS; Morales-Arce split"},
    ],
    "VEN": [
        {"date": "TBD-2027", "type": "Parliamentary",
         "stakes": "Post-2024 contested presidential; opposition under pressure"},
    ],
    "URY": [
        {"date": "TBD-2029", "type": "Presidential",
         "stakes": "Orsi mid-term; Frente Amplio policy execution"},
    ],
    "CRI": [
        {"date": "2026-02-01", "type": "Presidential general",
         "stakes": "Chaves successor; security + fiscal"},
    ],
    "GBR": [
        {"date": "2027-05-06", "type": "Local + Mayoral elections",
         "stakes": "Reform UK breakthrough test; Labour mid-term"},
        {"date": "TBD-2029", "type": "Westminster general (latest)",
         "stakes": "Statutory deadline 2029-08-15"},
    ],
    "DEU": [
        {"date": "2026-03-08", "type": "Sachsen-Anhalt Landtag",
         "stakes": "AfD East-German bellwether"},
        {"date": "2026-09-13", "type": "Berlin & MV Landtag",
         "stakes": "AfD eastern test; SPD/CDU coalition math"},
        {"date": "TBD-2029", "type": "Bundestag general",
         "stakes": "Statutory autumn 2029"},
    ],
    "FRA": [
        {"date": "2026-03-22", "type": "Municipal elections (round 1)",
         "stakes": "Macron base test ahead of 2027"},
        {"date": "2027-04-11", "type": "Presidential first round",
         "stakes": "Macron term-limited; Le Pen, Bardella, Attal field"},
        {"date": "2027-04-25", "type": "Presidential runoff",
         "stakes": "RN vs centrist coalition projected"},
        {"date": "2027-06-13", "type": "Legislative first round",
         "stakes": "Follow-on cohabitation test"},
    ],
    "ITA": [
        {"date": "TBD-2027", "type": "General (Camera+Senato)",
         "stakes": "Meloni term-end; FdI dominance test"},
    ],
    "ESP": [
        {"date": "TBD-2027", "type": "General Cortes (latest)",
         "stakes": "Sanchez minority government stability"},
        {"date": "2026-02-15", "type": "Catalonia regional",
         "stakes": "Illa government; independence movement state"},
    ],
    "PRT": [
        {"date": "2026-01-18", "type": "Presidential",
         "stakes": "Marcelo successor; centre-right favored"},
    ],
    "NLD": [
        {"date": "2027-03-17", "type": "Tweede Kamer general",
         "stakes": "Wilders/PVV cabinet survival test"},
    ],
    "POL": [
        {"date": "2026-05-10", "type": "Presidential first round",
         "stakes": "Duda successor; KO vs PiS axis"},
        {"date": "2026-05-24", "type": "Presidential runoff",
         "stakes": "Will determine Tusk coalition latitude"},
    ],
    "HUN": [
        {"date": "2026-04-12", "type": "Parliamentary",
         "stakes": "Orban 5th term bid vs Magyar TISZA"},
    ],
    "CZE": [
        {"date": "2025-10-04", "type": "Chamber of Deputies",
         "stakes": "Babis return vs SPOLU coalition"},
    ],
    "AUT": [
        {"date": "2024-09-29", "type": "National Council (past)",
         "stakes": "FPO first place; coalition delayed"},
    ],
    "ROU": [
        {"date": "2025-05-04", "type": "Presidential rerun first round",
         "stakes": "Court-annulled 2024 vote redo"},
    ],
    "GRC": [
        {"date": "TBD-2027", "type": "Parliamentary (latest)",
         "stakes": "Mitsotakis ND second-term mid"},
    ],
    "IRL": [
        {"date": "TBD-2029", "type": "Dail general (latest)",
         "stakes": "Statutory; FF-FG-Lab coalition"},
    ],
    "SWE": [
        {"date": "2026-09-13", "type": "Riksdag general",
         "stakes": "Kristersson-SD pact verdict"},
    ],
    "NOR": [
        {"date": "2025-09-08", "type": "Storting",
         "stakes": "Store Labour minority; right resurgence"},
    ],
    "DNK": [
        {"date": "TBD-2026", "type": "Folketing (latest 2026-11)",
         "stakes": "Frederiksen broad coalition survival"},
    ],
    "FIN": [
        {"date": "2027-04-18", "type": "Eduskunta general",
         "stakes": "Orpo coalition; NATO-era first full term"},
    ],
    "UKR": [
        {"date": "TBD-2026", "type": "Presidential (martial-law-permitting)",
         "stakes": "First wartime national vote; Zelenskyy re-run"},
    ],
    "BLR": [
        {"date": "2025-01-26", "type": "Presidential (past)",
         "stakes": "Lukashenko 7th term; non-competitive"},
    ],
    "CHE": [
        {"date": "TBD-2027", "type": "Federal Council renewal",
         "stakes": "Federal Council four-year cycle"},
    ],
    "BEL": [
        {"date": "TBD-2029", "type": "Federal general (latest)",
         "stakes": "De Wever coalition survival"},
    ],
    "NGA": [
        {"date": "TBD-2027", "type": "Presidential + National Assembly",
         "stakes": "Tinubu re-election bid; reform agenda"},
    ],
    "ZAF": [
        {"date": "TBD-2029", "type": "General + provincial",
         "stakes": "GNU survival; ANC under 50%"},
    ],
    "EGY": [
        {"date": "TBD-2028", "type": "Presidential",
         "stakes": "Sisi term-limited; succession question"},
    ],
    "KEN": [
        {"date": "2027-08-10", "type": "General",
         "stakes": "Ruto re-election bid; Gen-Z protest cycle"},
    ],
    "ETH": [
        {"date": "2026-06-01", "type": "General",
         "stakes": "Abiy Prosperity Party; Tigray peace test"},
    ],
    "GHA": [
        {"date": "2028-12-07", "type": "Presidential + parliamentary",
         "stakes": "Mahama mid-term; NDC consolidation"},
    ],
    "TUN": [
        {"date": "2026-10-25", "type": "Parliamentary",
         "stakes": "Saied parliamentary control test"},
    ],
    "MAR": [
        {"date": "TBD-2026", "type": "Parliamentary (Chambre des Representants)",
         "stakes": "Akhannouch RNI coalition; reform agenda"},
    ],
    "DZA": [
        {"date": "TBD-2027", "type": "Legislative",
         "stakes": "Tebboune mid-term"},
    ],
    "SEN": [
        {"date": "TBD-2029", "type": "Presidential (latest)",
         "stakes": "Faye term mid-point"},
    ],
    "UGA": [
        {"date": "2026-01-18", "type": "Presidential + parliamentary",
         "stakes": "Museveni 7th elected term; Bobi Wine challenge"},
    ],
    "RWA": [
        {"date": "TBD-2029", "type": "Presidential",
         "stakes": "Kagame next-term horizon"},
    ],
    "TZA": [
        {"date": "2025-10-29", "type": "General",
         "stakes": "Hassan first elected term"},
    ],
    "COD": [
        {"date": "TBD-2028", "type": "Presidential + legislative",
         "stakes": "Tshisekedi term-end; M23/eastern security"},
    ],
    "ISR": [
        {"date": "TBD-2026", "type": "Knesset (early possible)",
         "stakes": "Netanyahu coalition; post-Gaza politics"},
    ],
    "IRN": [
        {"date": "TBD-2028", "type": "Majles parliamentary",
         "stakes": "Pezeshkian term mid-point; succession debate"},
    ],
    "TUR": [
        {"date": "2028-05-14", "type": "Presidential + parliamentary",
         "stakes": "Erdogan term-limited unless constitution amended"},
    ],
    "ARE": [
        {"date": "TBD-2027", "type": "FNC half-renewal",
         "stakes": "Advisory body; sheikh appointees + electoral college"},
    ],
    "SAU": [
        {"date": "n/a", "type": "Municipal advisory (no national vote)",
         "stakes": "MBS consolidation; Vision 2030"},
    ],
    "IND": [
        {"date": "2026-04-01", "type": "Tamil Nadu state election",
         "stakes": "DMK reelection bid; AIADMK-BJP alliance"},
        {"date": "2026-04-01", "type": "West Bengal state election",
         "stakes": "TMC vs BJP; Mamata third term"},
        {"date": "2027-02-01", "type": "Uttar Pradesh state election",
         "stakes": "BJP heartland; UCC implementation"},
        {"date": "TBD-2029", "type": "Lok Sabha general (latest)",
         "stakes": "Modi-3 mid-term; INDIA bloc next phase"},
    ],
    "PAK": [
        {"date": "TBD-2029", "type": "National Assembly (latest)",
         "stakes": "PML-N coalition mid-term; PTI marginalisation"},
    ],
    "BGD": [
        {"date": "2026-02-01", "type": "Jatiya Sangsad (caretaker plan)",
         "stakes": "Post-Hasina transition; reform commission cycle"},
    ],
    "LKA": [
        {"date": "TBD-2029", "type": "Presidential (latest)",
         "stakes": "Dissanayake NPP term mid-point"},
    ],
    "NPL": [
        {"date": "2027-11-01", "type": "House of Representatives",
         "stakes": "Coalition merry-go-round verdict"},
    ],
    "KHM": [
        {"date": "TBD-2028", "type": "National Assembly",
         "stakes": "Hun Manet first full cycle"},
    ],
    "MMR": [
        {"date": "TBD-2026", "type": "Junta-organised general (disputed)",
         "stakes": "Tatmadaw legitimisation attempt"},
    ],
    "VNM": [
        {"date": "TBD-2026", "type": "13th Congress (CPV)",
         "stakes": "Party congress; not a public vote"},
    ],
    "PHL": [
        {"date": "2025-05-12", "type": "Midterm + barangay",
         "stakes": "Marcos-Duterte split; Senate composition"},
        {"date": "2028-05-08", "type": "Presidential",
         "stakes": "Open seat; VP succession dynamic"},
    ],
    "IDN": [
        {"date": "TBD-2029", "type": "Presidential + DPR (latest)",
         "stakes": "Prabowo mid-term; Gibran VP role"},
    ],
    "THA": [
        {"date": "TBD-2027", "type": "House of Representatives",
         "stakes": "Pheu Thai coalition; military prerogatives"},
    ],
    "MYS": [
        {"date": "TBD-2028", "type": "Federal general (latest)",
         "stakes": "Anwar Madani coalition; PN challenge"},
    ],
    "SGP": [
        {"date": "TBD-2025", "type": "General (called Apr 2025)",
         "stakes": "Wong's first vote as PM; PAP supermajority test"},
    ],
    "KAZ": [
        {"date": "TBD-2026", "type": "Majilis (lower-house)",
         "stakes": "Tokayev consolidation; opposition cosmetic"},
    ],
    "TWN": [
        {"date": "2028-01-22", "type": "Presidential + Legislative Yuan",
         "stakes": "Lai re-election bid; cross-strait posture"},
    ],
    "KOR": [
        {"date": "2028-04-12", "type": "National Assembly",
         "stakes": "Lee Jae-myung mid-term verdict"},
    ],
    "JPN": [
        {"date": "TBD-2027", "type": "House of Representatives (latest)",
         "stakes": "LDP minority survival; Ishiba/successor"},
        {"date": "2028-07-01", "type": "House of Councillors",
         "stakes": "Half-renewal; upper-house balance"},
    ],
    "AUS": [
        {"date": "TBD-2028", "type": "Federal general",
         "stakes": "Albanese second-term mid; Dutton or successor"},
    ],
    "NZL": [
        {"date": "TBD-2026", "type": "General",
         "stakes": "Luxon coalition; ACT-NZF balance"},
    ],
}


def main():
    by_iso = {iso: items[:] for iso, items in SUPPLEMENTARY_BY_ISO.items()}
    total = sum(len(v) for v in by_iso.values())
    path = write_overlay(
        "elections_calendar",
        {
            "source": "curated",
            "source_url": "IFES + IDEA + Wikipedia cross-references",
            "cadence": "daily (no-op until IFES scraper)",
            "by_iso": by_iso,
        },
    )
    print("elections_calendar: wrote " + str(path) + " (" + str(total) + " entries across " + str(len(by_iso)) + " countries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
