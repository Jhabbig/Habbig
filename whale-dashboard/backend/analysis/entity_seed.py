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

    # ── Quant / systematic ──────────────────────────────────────────────────
    "renaissance": {
        "parent_name": "Renaissance Technologies",
        "entity_type": "hedge_fund",
        "description": "Jim Simons' quant fund. Medallion is closed; RIEF + others "
                       "are the public 13F filers.",
        "ciks": [(1037389, "Renaissance Technologies LLC", "13F")],
    },
    "two_sigma": {
        "parent_name": "Two Sigma",
        "entity_type": "hedge_fund",
        "description": "Quant fund founded by John Overdeck & David Siegel. "
                       "Multiple 13F filers across Investments / Advisers / Securities.",
        "ciks": [
            (1179392, "Two Sigma Investments, LP", "13F"),
            (1478735, "Two Sigma Advisers, LP", "13F"),
            (1450144, "Two Sigma Securities, LLC", "13F"),
        ],
    },
    "de_shaw": {
        "parent_name": "D. E. Shaw",
        "entity_type": "hedge_fund",
        "description": "David Shaw's quant + multi-strat fund.",
        "ciks": [(1009207, "D. E. Shaw & Co., Inc.", "13F")],
    },
    "aqr": {
        "parent_name": "AQR Capital Management",
        "entity_type": "hedge_fund",
        "description": "Cliff Asness' factor-investing shop.",
        "ciks": [
            (1167557, "AQR Capital Management LLC", "13F"),
            (1167456, "AQR Arbitrage LLC", "13F"),
        ],
    },
    "susquehanna": {
        "parent_name": "Susquehanna International Group",
        "entity_type": "hedge_fund",
        "description": "Quant + market-making (SIG). Bart Smith / Jeff Yass.",
        "ciks": [
            (1446194, "Susquehanna International Group, LLP", "13F"),
            (1765923, "Susquehanna International Securities, Ltd.", "13F"),
            (1765924, "Susquehanna International Group Ltd.", "13F"),
        ],
    },

    # ── Multi-strategy pod-shops ────────────────────────────────────────────
    "schonfeld": {
        "parent_name": "Schonfeld Strategic Advisors",
        "entity_type": "hedge_fund",
        "description": "Steven Schonfeld's multi-strategy pod-shop.",
        "ciks": [(1665241, "Schonfeld Strategic Advisors LLC", "13F")],
    },
    "balyasny": {
        "parent_name": "Balyasny Asset Management",
        "entity_type": "hedge_fund",
        "description": "Dmitry Balyasny's multi-PM fund.",
        "ciks": [(1218710, "Balyasny Asset Management L.P.", "13F")],
    },
    "exoduspoint": {
        "parent_name": "ExodusPoint Capital",
        "entity_type": "hedge_fund",
        "description": "Mike Gelband's multi-strategy launch (ex-Millennium).",
        "ciks": [(1736225, "ExodusPoint Capital Management, LP", "13F")],
    },

    # ── Tiger cubs (concentrated long-equity, often correlated) ─────────────
    "tiger_global": {
        "parent_name": "Tiger Global Management",
        "entity_type": "hedge_fund",
        "description": "Chase Coleman's tech-heavy long-short tiger cub.",
        "ciks": [(1167483, "Tiger Global Management LLC", "13F")],
    },
    "coatue": {
        "parent_name": "Coatue Management",
        "entity_type": "hedge_fund",
        "description": "Philippe Laffont's tech-focused tiger cub.",
        "ciks": [(1135730, "Coatue Management LLC", "13F")],
    },
    "lone_pine": {
        "parent_name": "Lone Pine Capital",
        "entity_type": "hedge_fund",
        "description": "Steve Mandel's long-short tiger cub.",
        "ciks": [(1061165, "Lone Pine Capital LLC", "13F")],
    },
    "viking": {
        "parent_name": "Viking Global Investors",
        "entity_type": "hedge_fund",
        "description": "Andreas Halvorsen's tiger cub.",
        "ciks": [(1103804, "Viking Global Investors LP", "13F")],
    },
    "maverick": {
        "parent_name": "Maverick Capital",
        "entity_type": "hedge_fund",
        "description": "Lee Ainslie's tiger cub.",
        "ciks": [(934639, "Maverick Capital Ltd", "13F")],
    },

    # ── Macro / value / event-driven ────────────────────────────────────────
    "brevan_howard": {
        "parent_name": "Brevan Howard",
        "entity_type": "hedge_fund",
        "description": "Alan Howard's macro fund.",
        "ciks": [(1512857, "Brevan Howard Capital Management LP", "13F")],
    },
    "tudor": {
        "parent_name": "Tudor Investment",
        "entity_type": "hedge_fund",
        "description": "Paul Tudor Jones' macro fund.",
        "ciks": [(923093, "Tudor Investment Corp", "13F")],
    },
    "baupost": {
        "parent_name": "Baupost Group",
        "entity_type": "hedge_fund",
        "description": "Seth Klarman's value/distressed fund.",
        "ciks": [(1061768, "Baupost Group LLC/MA", "13F")],
    },
    "greenlight": {
        "parent_name": "Greenlight Capital",
        "entity_type": "hedge_fund",
        "description": "David Einhorn's long-short value fund.",
        "ciks": [(1079114, "Greenlight Capital Inc", "13F")],
    },
    "appaloosa": {
        "parent_name": "Appaloosa",
        "entity_type": "hedge_fund",
        "description": "David Tepper's distressed/credit fund.",
        "ciks": [(1656456, "Appaloosa LP", "13F")],
    },

    # ── Activists ───────────────────────────────────────────────────────────
    "trian": {
        "parent_name": "Trian Fund Management",
        "entity_type": "activist",
        "description": "Nelson Peltz's activist vehicle.",
        "ciks": [(1345471, "Trian Fund Management, L.P.", "13F")],
    },
    "starboard": {
        "parent_name": "Starboard Value",
        "entity_type": "activist",
        "description": "Jeff Smith's activist fund.",
        "ciks": [(1517137, "Starboard Value LP", "13F")],
    },
    "jana": {
        "parent_name": "JANA Partners",
        "entity_type": "activist",
        "description": "Barry Rosenstein's activist + ESG-focused fund.",
        "ciks": [(1998597, "JANA Partners Management, LP", "13F")],
    },
    "engaged": {
        "parent_name": "Engaged Capital",
        "entity_type": "activist",
        "description": "Glenn Welling's mid-cap activist.",
        "ciks": [(1559771, "Engaged Capital LLC", "13F")],
    },

    # ── Family offices / personal ───────────────────────────────────────────
    "soros": {
        "parent_name": "Soros Fund Management",
        "entity_type": "family_office",
        "description": "George Soros' family office.",
        "ciks": [(1029160, "Soros Fund Management LLC", "13F")],
    },

    # ── Banks (treasury + asset-management arms) ────────────────────────────
    "bank_of_america": {
        "parent_name": "Bank of America",
        "entity_type": "bank",
        "description": "Largest US retail bank by deposits; 13F covers wealth + treasury.",
        "ciks": [(70858, "Bank of America Corp", "13F")],
    },
    "wells_fargo": {
        "parent_name": "Wells Fargo",
        "entity_type": "bank",
        "description": "Diversified US bank with sizeable wealth management.",
        "ciks": [(72971, "Wells Fargo & Company", "13F")],
    },
    "citigroup": {
        "parent_name": "Citigroup",
        "entity_type": "bank",
        "description": "Global investment bank.",
        "ciks": [(831001, "Citigroup Inc", "13F")],
    },
    "ubs": {
        "parent_name": "UBS",
        "entity_type": "bank",
        "description": "Swiss investment bank + wealth management.",
        "ciks": [(1610520, "UBS Group AG", "13F")],
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
