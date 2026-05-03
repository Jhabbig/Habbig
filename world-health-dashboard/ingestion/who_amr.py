"""WHO antimicrobial-resistance surveillance (via GHO).

WHO's Global AMR Surveillance System (GLASS) and Gonococcal AMR Surveillance
Programme (GASP) feed the Global Health Observatory. We pull the indicators
that report actual *resistance rates* (not capacity / process indicators) so
we can render a worldwide HAI / AMR layer.

Reusing the existing `who_gho` module — its OData parser handles SEX_BTSX,
caching, and stale-fallback the same way.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from . import who_gho

log = logging.getLogger(__name__)


# Curated AMR indicator catalog.
#   id          our internal id (used in API)
#   code        WHO GHO indicator code
#   name        display name
#   pathogen    organism / disease
#   antibiotic  the antimicrobial in question
#   higher      direction: True if "higher value = worse"
INDICATORS: list[dict] = [
    {
        "id": "amr_mrsa",
        "code": "AMR_INFECT_MRSA",
        "name": "MRSA in S. aureus bloodstream infections",
        "pathogen": "Staphylococcus aureus",
        "antibiotic": "Methicillin",
        "specimen": "Blood (BSI)",
        "unit": "%",
        "source": "WHO GLASS",
        "higher": True,
        "description": (
            "Share of S. aureus bloodstream isolates that are methicillin-"
            "resistant (MRSA). MRSA limits beta-lactam options and is a "
            "leading cause of healthcare-associated mortality."
        ),
    },
    {
        "id": "amr_ecoli_3gc",
        "code": "AMR_INFECT_ECOLI",
        "name": "E. coli 3GC-resistant in BSI",
        "pathogen": "Escherichia coli",
        "antibiotic": "3rd-generation cephalosporins",
        "specimen": "Blood (BSI)",
        "unit": "%",
        "source": "WHO GLASS",
        "higher": True,
        "description": (
            "Share of E. coli bloodstream isolates resistant to third-"
            "generation cephalosporins (ESBL-driver). High prevalence forces "
            "carbapenem use, accelerating CRE emergence."
        ),
    },
    {
        "id": "tb_rifampicin",
        "code": "TB_drs_rr_prct",
        "name": "Rifampicin resistance in pulmonary TB",
        "pathogen": "Mycobacterium tuberculosis",
        "antibiotic": "Rifampicin",
        "specimen": "Sputum",
        "unit": "%",
        "source": "WHO Drug Resistance Surveillance",
        "higher": True,
        "description": (
            "Prevalence of rifampicin-resistant TB among new pulmonary cases "
            "— rifampicin is the cornerstone of first-line TB treatment, so "
            "RR-TB requires longer, costlier MDR regimens."
        ),
    },
    # Gonococcal AMR — global GASP programme
    {
        "id": "gono_ceftriaxone",
        "code": "GASPRSCRO",
        "name": "N. gonorrhoeae ceftriaxone resistance",
        "pathogen": "Neisseria gonorrhoeae",
        "antibiotic": "Ceftriaxone (last-line)",
        "specimen": "Genital swab",
        "unit": "%",
        "source": "WHO GASP",
        "higher": True,
        "description": (
            "Ceftriaxone is the last reliable monotherapy for gonorrhoea; "
            "any resistance is a global public health alarm."
        ),
    },
    {
        "id": "gono_azithromycin",
        "code": "GASPRSAZM",
        "name": "N. gonorrhoeae azithromycin resistance",
        "pathogen": "Neisseria gonorrhoeae",
        "antibiotic": "Azithromycin",
        "specimen": "Genital swab",
        "unit": "%",
        "source": "WHO GASP",
        "higher": True,
        "description": (
            "Decreased azithromycin susceptibility — driver of dual-therapy "
            "policy changes."
        ),
    },
    {
        "id": "gono_ciprofloxacin",
        "code": "GASPRSCIP",
        "name": "N. gonorrhoeae ciprofloxacin resistance",
        "pathogen": "Neisseria gonorrhoeae",
        "antibiotic": "Ciprofloxacin",
        "specimen": "Genital swab",
        "unit": "%",
        "source": "WHO GASP",
        "higher": True,
        "description": (
            "Quinolone resistance is widespread; ciprofloxacin no longer "
            "first-line in most settings."
        ),
    },
]


def all_indicators() -> list[dict]:
    return [{k: v for k, v in i.items()} for i in INDICATORS]


def get_indicator(indicator_id: str) -> dict | None:
    return next((i for i in INDICATORS if i["id"] == indicator_id), None)


def fetch_indicator_data(indicator_id: str, force: bool = False) -> dict:
    ind = get_indicator(indicator_id)
    if not ind:
        return {"error": f"unknown indicator_id: {indicator_id}"}
    raw = who_gho.fetch_indicator(ind["code"], force=force)
    return {
        "indicator": ind,
        **raw,
    }


def fetch_all(force: bool = False) -> dict[str, dict]:
    """Pull every catalog indicator concurrently."""
    out: dict[str, dict] = {}

    def _one(ind: dict) -> tuple[str, dict]:
        try:
            return ind["id"], fetch_indicator_data(ind["id"], force=force)
        except Exception as exc:
            log.warning("AMR fetch failed for %s: %s", ind["id"], exc)
            return ind["id"], {"indicator": ind, "by_country": {}, "latest": {}, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for ind_id, payload in pool.map(_one, INDICATORS):
            out[ind_id] = payload
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    payload = fetch_all()
    for ind_id, data in payload.items():
        ind = data["indicator"]
        latest = data.get("latest", {})
        usa = latest.get("USA", {})
        gbr = latest.get("GBR", {})
        ind_year = max((v.get("year") for v in latest.values() if v.get("year")), default=None)
        print(f"  {ind_id:20s} {ind['name'][:55]:55s}  "
              f"countries={len(latest):3d}  USA={usa.get('value', '—')}  "
              f"GBR={gbr.get('value', '—')}  latest_year={ind_year}")
