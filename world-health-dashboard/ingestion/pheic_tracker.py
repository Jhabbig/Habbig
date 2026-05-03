"""Public Health Emergency of International Concern (PHEIC) tracker.

PHEIC is a formal designation by the WHO Director-General under the
International Health Regulations (2005). Only a handful have ever been
declared, and they're announced via WHO press releases — there is no API.

This module is a hand-curated record of every PHEIC declaration with start /
end dates and notes. It needs to be updated when the IHR Emergency Committee
meets and the DG either declares a new PHEIC or terminates an existing one.

⚠ Source-of-record: https://www.who.int/news-room/events-of-public-health-concern

Last reviewed: 2026-05-02
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Pheic:
    name: str
    disease: str
    declared: str               # ISO date
    ended: str | None           # ISO date, or None if still active
    short: str                  # 1-line characterization
    notes: str                  # longer narrative

    def to_dict(self) -> dict:
        d = asdict(self)
        d["active"] = self.ended is None
        return d


# Newest first.
HISTORY: list[Pheic] = [
    Pheic(
        name="Mpox (2024)",
        disease="Mpox",
        declared="2024-08-14",
        ended=None,
        short="Clade I MPXV outbreak in Central/East Africa with cross-border spread",
        notes=(
            "Declared after the rapid expansion of clade I (Ib in particular) MPXV "
            "from the DRC into multiple neighbouring countries (Burundi, Kenya, "
            "Rwanda, Uganda) plus initial detections outside Africa. Distinct from "
            "the 2022 PHEIC, which was driven by clade II circulating among MSM "
            "networks globally."
        ),
    ),
    Pheic(
        name="Mpox (2022)",
        disease="Mpox",
        declared="2022-07-23",
        ended="2023-05-11",
        short="Multi-country clade II MPXV outbreak primarily affecting MSM",
        notes=(
            "First time the IHR Emergency Committee was overruled — the DG declared "
            "PHEIC despite the committee's 'no consensus' position. Sustained "
            "human-to-human transmission outside Africa was the key escalation."
        ),
    ),
    Pheic(
        name="COVID-19",
        disease="SARS-CoV-2",
        declared="2020-01-30",
        ended="2023-05-05",
        short="SARS-CoV-2 pandemic — the longest active PHEIC to date",
        notes=(
            "Declared 8 days after WHO was first notified of the cluster in Wuhan. "
            "Termination came >3 years later when WHO determined the disease had "
            "transitioned to a long-term established health issue."
        ),
    ),
    Pheic(
        name="Ebola DRC (Kivu)",
        disease="Ebola virus disease",
        declared="2019-07-17",
        ended="2020-06-26",
        short="Ebola outbreak in the eastern DRC during armed conflict",
        notes=(
            "Second-largest Ebola outbreak on record. Notable for the use of the "
            "rVSV-ZEBOV vaccine in active conflict zones and the assassination of "
            "responders by armed groups."
        ),
    ),
    Pheic(
        name="Zika",
        disease="Zika virus",
        declared="2016-02-01",
        ended="2016-11-18",
        short="Zika-linked microcephaly cluster in the Americas",
        notes=(
            "PHEIC was tied specifically to neurological complications (microcephaly, "
            "Guillain-Barré) rather than the spread of Zika itself. Declared while "
            "the etiologic link was still being established."
        ),
    ),
    Pheic(
        name="Ebola West Africa",
        disease="Ebola virus disease",
        declared="2014-08-08",
        ended="2016-03-29",
        short="Ebola in Guinea, Liberia, Sierra Leone — 28,000+ cases",
        notes=(
            "Largest Ebola outbreak in history; exposed major gaps in IHR core "
            "capacities and triggered significant reform of global health emergency "
            "preparedness."
        ),
    ),
    Pheic(
        name="Polio",
        disease="Wild & vaccine-derived poliovirus",
        declared="2014-05-05",
        ended=None,
        short="International spread of poliovirus — longest-running PHEIC",
        notes=(
            "Renewed by every Emergency Committee meeting since 2014. Currently "
            "reflects circulating vaccine-derived strains (cVDPV2 mostly) plus "
            "residual WPV1 in Pakistan and Afghanistan."
        ),
    ),
    Pheic(
        name="H1N1 influenza",
        disease="Influenza A(H1N1)pdm09",
        declared="2009-04-25",
        ended="2010-08-10",
        short="Swine-origin H1N1 influenza pandemic",
        notes=(
            "First PHEIC ever declared. Transitioned to a seasonal H1N1 strain "
            "now part of routine influenza vaccine composition."
        ),
    ),
]


def all_pheics() -> list[dict]:
    return [p.to_dict() for p in HISTORY]


def active() -> list[dict]:
    return [p.to_dict() for p in HISTORY if p.ended is None]


def by_year(year: int) -> list[dict]:
    """PHEICs that were active for any portion of `year`."""
    out = []
    for p in HISTORY:
        start = int(p.declared[:4])
        end = int(p.ended[:4]) if p.ended else 9999
        if start <= year <= end:
            out.append(p.to_dict())
    return out


if __name__ == "__main__":
    print(f"PHEICs in record: {len(HISTORY)}")
    for p in HISTORY:
        status = "ACTIVE" if p.ended is None else f"ended {p.ended}"
        print(f"  {p.declared}  {p.name:30s}  {status}")
    print(f"\nCurrently active: {len(active())}")
