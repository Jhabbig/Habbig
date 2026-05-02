from __future__ import annotations
"""Seed mapping of top institutional filers to their CIK families.

A single bank/asset manager files under MANY different CIKs — JPM has 13F filings
under JP Morgan Chase Bank NA, JP Morgan Investment Management, JP Morgan Asset
Management, etc. Without rolling them up, the dashboard radically undercounts
their real book.

This seed is intentionally curated, not auto-generated. The CIKs here are
verified from SEC EDGAR's company search; expand the list over time. The
entity_resolver will fall back to fuzzy name match for unknown filers but the
top ~50 filers should always be hand-mapped — they're 80% of the AUM.

Each CIK is given as an integer (no leading zeros). Look them up at
https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>

Format:
    SEED[slug] = {
        "parent_name": "Display Name",
        "entity_type": "bank" | "hedge_fund" | "asset_mgr" | "activist" | "family_office",
        "description": str,
        "ciks": [(cik:int, sub_name:str, authority:str), ...],
    }

`authority` is one of {"13F", "13D", "Form 4", "all"} — most filers use "13F"
for institutional rollups. Use "all" if the same CIK files across forms.
"""

from typing import Dict, List, Tuple, TypedDict


class EntityRecord(TypedDict):
    parent_name: str
    entity_type: str
    description: str
    ciks: List[Tuple[int, str, str]]


SEED: Dict[str, EntityRecord] = {
    "jpmorgan": {
        "parent_name": "JPMorgan Chase",
        "entity_type": "bank",
        "description": "Largest US bank by assets; 13F filings span asset mgmt, "
                       "private bank, and broker-dealer subsidiaries.",
        "ciks": [
            (19617, "JPMorgan Chase & Co", "all"),
            (1067983, "JPMorgan Chase Bank, National Association", "13F"),
            (1422183, "JPMorgan Investment Management Inc", "13F"),
            (1217286, "J.P. Morgan Securities LLC", "13F"),
        ],
    },
    "morgan_stanley": {
        "parent_name": "Morgan Stanley",
        "entity_type": "bank",
        "description": "Investment bank + wealth management; multiple 13F filers.",
        "ciks": [
            (895421, "Morgan Stanley", "all"),
            (1418091, "Morgan Stanley Smith Barney LLC", "13F"),
            (1037389, "Morgan Stanley Investment Management Inc", "13F"),
        ],
    },
    "goldman_sachs": {
        "parent_name": "Goldman Sachs Group",
        "entity_type": "bank",
        "description": "Investment bank with sizeable asset management arm.",
        "ciks": [
            (886982, "Goldman Sachs Group Inc", "all"),
            (769993, "Goldman Sachs Asset Management, L.P.", "13F"),
        ],
    },
    "blackrock": {
        "parent_name": "BlackRock",
        "entity_type": "asset_mgr",
        "description": "World's largest asset manager (~$10T AUM). iShares ETFs.",
        "ciks": [
            (1364742, "BlackRock Inc.", "all"),
            (1006249, "BlackRock Fund Advisors", "13F"),
            (1086364, "BlackRock Institutional Trust Company, NA", "13F"),
        ],
    },
    "vanguard": {
        "parent_name": "The Vanguard Group",
        "entity_type": "asset_mgr",
        "description": "Index-fund pioneer; second largest asset manager.",
        "ciks": [
            (102909, "Vanguard Group Inc", "all"),
        ],
    },
    "state_street": {
        "parent_name": "State Street",
        "entity_type": "asset_mgr",
        "description": "SPDR ETF sponsor; major institutional custodian.",
        "ciks": [
            (93751, "State Street Corp", "all"),
            (1112302, "SSgA Funds Management, Inc.", "13F"),
        ],
    },
    "fidelity": {
        "parent_name": "FMR LLC (Fidelity)",
        "entity_type": "asset_mgr",
        "description": "Fidelity Investments — privately held, files as FMR LLC.",
        "ciks": [
            (315066, "FMR LLC", "13F"),
        ],
    },
    "berkshire": {
        "parent_name": "Berkshire Hathaway",
        "entity_type": "family_office",
        "description": "Warren Buffett's holding company. The single most-watched 13F.",
        "ciks": [
            (1067983, "Berkshire Hathaway Inc", "13F"),
        ],
    },
    "citadel": {
        "parent_name": "Citadel Advisors",
        "entity_type": "hedge_fund",
        "description": "Multi-strategy pod-shop. 13F is one entity but reflects ~100 pods.",
        "ciks": [
            (1423053, "Citadel Advisors LLC", "13F"),
        ],
    },
    "millennium": {
        "parent_name": "Millennium Management",
        "entity_type": "hedge_fund",
        "description": "Multi-strategy pod-shop run by Izzy Englander.",
        "ciks": [
            (1273087, "Millennium Management LLC", "13F"),
        ],
    },
    "point72": {
        "parent_name": "Point72 Asset Management",
        "entity_type": "hedge_fund",
        "description": "Steve Cohen's family-office-turned-hedge-fund.",
        "ciks": [
            (1603466, "Point72 Asset Management, L.P.", "13F"),
        ],
    },
    "bridgewater": {
        "parent_name": "Bridgewater Associates",
        "entity_type": "hedge_fund",
        "description": "Ray Dalio's macro fund; 13F is a small slice of true book.",
        "ciks": [
            (1350694, "Bridgewater Associates, LP", "13F"),
        ],
    },
    "elliott": {
        "parent_name": "Elliott Investment Management",
        "entity_type": "activist",
        "description": "Paul Singer's activist fund. 13D filings are the signal.",
        "ciks": [
            (1791786, "Elliott Investment Management L.P.", "all"),
        ],
    },
    "pershing_square": {
        "parent_name": "Pershing Square Capital",
        "entity_type": "activist",
        "description": "Bill Ackman's concentrated activist book.",
        "ciks": [
            (1336528, "Pershing Square Capital Management, L.P.", "all"),
        ],
    },
    "icahn": {
        "parent_name": "Icahn Capital",
        "entity_type": "activist",
        "description": "Carl Icahn's activist vehicle.",
        "ciks": [
            (921669, "Icahn Carl C", "all"),
        ],
    },
    "valueact": {
        "parent_name": "ValueAct Capital",
        "entity_type": "activist",
        "description": "Constructive activist focused on operational improvement.",
        "ciks": [
            (1418814, "ValueAct Holdings, L.P.", "all"),
        ],
    },
    "third_point": {
        "parent_name": "Third Point",
        "entity_type": "activist",
        "description": "Daniel Loeb's event-driven / activist fund.",
        "ciks": [
            (1040273, "Third Point LLC", "all"),
        ],
    },
}


def load_seed_into_db() -> None:
    """Seed the entities + cik_map tables. Idempotent."""
    from database import map_cik, upsert_entity

    for slug, rec in SEED.items():
        entity_id = upsert_entity(
            slug=slug,
            parent_name=rec["parent_name"],
            entity_type=rec["entity_type"],
            description=rec["description"],
        )
        for cik, sub_name, authority in rec["ciks"]:
            map_cik(cik=cik, entity_id=entity_id, sub_name=sub_name,
                    filing_authority=authority, confidence=1.0)
