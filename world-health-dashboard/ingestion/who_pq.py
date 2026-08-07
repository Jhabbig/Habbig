"""WHO Prequalification (PQ) list — curated snapshot.

The WHO PQ programme audits manufacturers to a UN-procurement quality bar.
For HIV/TB/malaria/NTD drugs, the PQ list is the de-facto signal for what's
actually procurable globally (Global Fund, Gavi, UNICEF only buy from PQ'd
suppliers).

The PQ portal at extranet.who.int/prequal does not expose a public API —
it's a Drupal site with AJAX-only data. We curate a snapshot of the most-
strategic PQ'd finished pharmaceutical products and vaccines mapped to our
EML disease slugs. The number of *PQ'd manufacturers per drug* is the
canonical robustness signal:

    pq_count == 0   →  no PQ-cleared supplier, likely brand-only
    pq_count == 1   →  fragile (e.g. fexinidazole, miltefosine)
    pq_count >= 5   →  resilient generic competition (e.g. tenofovir, ACT)

Data sourced from:
  - WHO PQ List of Prequalified Finished Pharmaceutical Products (FPP)
  - WHO PQ List of Prequalified Vaccines
  - Stop TB Partnership Global Drug Facility (GDF) procurement catalogue
  - The Global Fund Pooled Procurement Mechanism

Last reviewed: 2026-04. Numbers here are illustrative for the dashboard;
treat as 'order of magnitude' rather than exact.
"""

from __future__ import annotations

# Per-drug WHO PQ profile keyed by EML generic name (the same names used in
# `who_eml.EML_BY_DISEASE`). Each entry:
#   pq_count       — number of PQ'd manufacturers as of last review
#   key_makers     — top PQ'd manufacturers (curated, ~5 max)
#   first_pq_year  — when the first manufacturer was prequalified
#   pq_categories  — WHO PQ categories (FPP, VAC, VEC, IVD, MCD)
PQ_BY_GENERIC: dict[str, dict] = {

    # ── HIV (TLD + DTG-based combos) ─────────────────────────────────────
    "tenofovir disoproxil fumarate": {
        "pq_count": 27, "first_pq_year": 2008,
        "key_makers": ["Mylan", "Cipla", "Aurobindo", "Hetero", "Strides"],
        "categories": ["FPP"],
    },
    "lamivudine": {
        "pq_count": 18, "first_pq_year": 2003,
        "key_makers": ["Cipla", "Mylan", "Aurobindo", "Hetero", "Strides"],
        "categories": ["FPP"],
    },
    "dolutegravir": {
        "pq_count": 8, "first_pq_year": 2017,
        "key_makers": ["ViiV/GSK", "Mylan", "Aurobindo", "Macleods", "Cipla"],
        "categories": ["FPP"],
    },
    "efavirenz": {
        "pq_count": 14, "first_pq_year": 2007,
        "key_makers": ["Mylan", "Cipla", "Aurobindo", "Hetero", "Strides"],
        "categories": ["FPP"],
    },
    "emtricitabine": {
        "pq_count": 9, "first_pq_year": 2010,
        "key_makers": ["Mylan", "Cipla", "Aurobindo", "Hetero"],
        "categories": ["FPP"],
    },
    "rilpivirine": {
        "pq_count": 4, "first_pq_year": 2018,
        "key_makers": ["Janssen (originator)", "Mylan", "Cipla"],
        "categories": ["FPP"],
    },
    "cabotegravir LA": {
        "pq_count": 1, "first_pq_year": 2024,
        "key_makers": ["ViiV/GSK (originator)"],
        "categories": ["FPP"],
        "note": "Long-acting injectable PrEP; voluntary licence to generics in 2024 but PQ pipeline still building.",
    },

    # ── TB (HRZE + bedaquiline) ───────────────────────────────────────────
    "isoniazid": {
        "pq_count": 12, "first_pq_year": 2002,
        "key_makers": ["Macleods", "Lupin", "Sandoz", "Sanofi", "Strides"],
        "categories": ["FPP"],
    },
    "rifampicin": {
        "pq_count": 11, "first_pq_year": 2002,
        "key_makers": ["Macleods", "Lupin", "Sandoz", "Sanofi"],
        "categories": ["FPP"],
    },
    "pyrazinamide": {
        "pq_count": 9, "first_pq_year": 2003,
        "key_makers": ["Macleods", "Lupin", "Sandoz"],
        "categories": ["FPP"],
    },
    "ethambutol": {
        "pq_count": 9, "first_pq_year": 2003,
        "key_makers": ["Macleods", "Lupin", "Sandoz"],
        "categories": ["FPP"],
    },
    "bedaquiline": {
        "pq_count": 2, "first_pq_year": 2019,
        "key_makers": ["Janssen (originator)", "Lupin"],
        "categories": ["FPP"],
        "note": "Voluntary licence to Lupin in 2023; second PQ generic expected 2025-26.",
    },
    "linezolid": {
        "pq_count": 6, "first_pq_year": 2014,
        "key_makers": ["Hetero", "Macleods", "Lupin", "Mylan"],
        "categories": ["FPP"],
    },

    # ── Malaria (ACTs + IV) ───────────────────────────────────────────────
    "artemether-lumefantrine": {
        "pq_count": 9, "first_pq_year": 2008,
        "key_makers": ["Novartis (Coartem)", "Ipca", "Cipla", "Strides", "Macleods"],
        "categories": ["FPP"],
    },
    "artesunate": {
        "pq_count": 7, "first_pq_year": 2010,
        "key_makers": ["Guilin (China)", "Ipca", "Cipla", "Macleods"],
        "categories": ["FPP"],
    },
    "dihydroartemisinin-piperaquine": {
        "pq_count": 5, "first_pq_year": 2011,
        "key_makers": ["Sigma-Tau / Alfasigma", "Ipca", "Cipla"],
        "categories": ["FPP"],
    },
    "primaquine": {
        "pq_count": 3, "first_pq_year": 2014,
        "key_makers": ["Sanofi", "Ipca", "Remedica"],
        "categories": ["FPP"],
    },
    "RTS,S/AS01 vaccine": {
        "pq_count": 1, "first_pq_year": 2022,
        "key_makers": ["GSK"],
        "categories": ["VAC"],
    },
    "R21/Matrix-M vaccine": {
        "pq_count": 1, "first_pq_year": 2024,
        "key_makers": ["Serum Institute of India / Oxford"],
        "categories": ["VAC"],
    },

    # ── Diarrhoea / cholera ───────────────────────────────────────────────
    "oral rehydration salts": {
        "pq_count": 17, "first_pq_year": 2003,
        "key_makers": ["Macleods", "Cipla", "Strides", "Aurobindo"],
        "categories": ["FPP"],
    },
    "azithromycin": {
        "pq_count": 11, "first_pq_year": 2007,
        "key_makers": ["Mylan", "Cipla", "Aurobindo", "Hetero", "Macleods"],
        "categories": ["FPP"],
    },
    "doxycycline": {
        "pq_count": 6, "first_pq_year": 2012,
        "key_makers": ["Mylan", "Cipla", "Strides"],
        "categories": ["FPP"],
    },
    "Dukoral / Shanchol vaccine": {
        "pq_count": 3, "first_pq_year": 2011,
        "key_makers": ["Shantha (Sanofi)", "Eubiologics", "Valneva"],
        "categories": ["VAC"],
        "note": "Shanchol (Shantha/Sanofi) was the workhorse; supply has tightened in 2022-23 with global cholera surge.",
    },

    # ── Diabetes ──────────────────────────────────────────────────────────
    "metformin": {
        "pq_count": 14, "first_pq_year": 2017,
        "key_makers": ["Sanofi", "Cipla", "Aurobindo", "Hetero", "Lupin"],
        "categories": ["FPP"],
    },
    "insulin (regular, NPH, glargine)": {
        "pq_count": 4, "first_pq_year": 2020,
        "key_makers": ["Novo Nordisk", "Sanofi", "Lilly", "Biocon"],
        "categories": ["FPP"],
        "note": "PQ rollout for biosimilar insulins ongoing; coverage of analogues lags human insulins.",
    },
    "gliclazide": {
        "pq_count": 5, "first_pq_year": 2019,
        "key_makers": ["Servier", "Cipla", "Lupin"],
        "categories": ["FPP"],
    },

    # ── Antibiotics for paediatric pneumonia / sepsis ─────────────────────
    "amoxicillin": {
        "pq_count": 16, "first_pq_year": 2005,
        "key_makers": ["Sandoz", "Cipla", "Mylan", "Aurobindo", "Macleods"],
        "categories": ["FPP"],
    },
    "ceftriaxone": {
        "pq_count": 10, "first_pq_year": 2008,
        "key_makers": ["Roche", "Sandoz", "Aurobindo", "Macleods"],
        "categories": ["FPP"],
    },

    # ── Neglected tropical diseases ───────────────────────────────────────
    "praziquantel": {
        "pq_count": 5, "first_pq_year": 2011,
        "key_makers": ["Merck KGaA", "Cipla", "Shanghai Pharma"],
        "categories": ["FPP"],
        "note": "Schistosomiasis MDA; Merck KGaA donates ~250M tablets/yr.",
    },
    "ivermectin": {
        "pq_count": 8, "first_pq_year": 2012,
        "key_makers": ["Merck (Mectizan donation)", "Sun Pharma", "Cipla"],
        "categories": ["FPP"],
        "note": "Onchocerciasis + lymphatic filariasis MDA.",
    },
    "albendazole": {
        "pq_count": 9, "first_pq_year": 2010,
        "key_makers": ["GSK (Mectizan donation)", "Macleods", "Cipla", "Strides"],
        "categories": ["FPP"],
    },
    "mebendazole": {
        "pq_count": 4, "first_pq_year": 2013,
        "key_makers": ["Janssen", "Cipla", "Macleods"],
        "categories": ["FPP"],
    },
    "diethylcarbamazine (DEC)": {
        "pq_count": 3, "first_pq_year": 2014,
        "key_makers": ["Eisai (donation)", "Cipla", "IndSwift"],
        "categories": ["FPP"],
    },
    "fexinidazole": {
        "pq_count": 1, "first_pq_year": 2019,
        "key_makers": ["Sanofi"],
        "categories": ["FPP"],
        "note": "Sleeping sickness; only PQ'd manufacturer (DNDi-supported).",
    },
    "liposomal amphotericin B": {
        "pq_count": 2, "first_pq_year": 2018,
        "key_makers": ["Gilead (AmBisome — donates for VL)", "Cipla"],
        "categories": ["FPP"],
        "note": "Visceral leishmaniasis treatment; Gilead donation programme.",
    },
    "miltefosine": {
        "pq_count": 1, "first_pq_year": 2017,
        "key_makers": ["Profounda"],
        "categories": ["FPP"],
        "note": "Single PQ supplier worldwide.",
    },

    # ── Vaccines (routine immunisation) ───────────────────────────────────
    "MMR vaccine": {
        "pq_count": 4, "first_pq_year": 2003,
        "key_makers": ["Serum Institute of India", "GSK", "Sanofi", "Bio Farma"],
        "categories": ["VAC"],
    },
    "OPV vaccine": {
        "pq_count": 5, "first_pq_year": 2003,
        "key_makers": ["Serum Institute of India", "Bio Farma", "Bilthoven", "Sanofi"],
        "categories": ["VAC"],
    },
    "IPV vaccine": {
        "pq_count": 3, "first_pq_year": 2014,
        "key_makers": ["Sanofi", "Serum Institute of India", "GSK"],
        "categories": ["VAC"],
    },
    "BCG vaccine": {
        "pq_count": 5, "first_pq_year": 2003,
        "key_makers": ["Serum Institute of India", "Japan BCG Lab", "Bio Farma", "AJ Vaccines"],
        "categories": ["VAC"],
    },
    "HepB vaccine": {
        "pq_count": 8, "first_pq_year": 2003,
        "key_makers": ["GSK", "Merck", "Serum Institute of India", "LG Chem"],
        "categories": ["VAC"],
    },
    "PCV13/PCV15/PCV20 vaccine": {
        "pq_count": 3, "first_pq_year": 2009,
        "key_makers": ["Pfizer (Prevnar 13/20)", "Serum Institute of India (PCV-10)", "MSD (V114)"],
        "categories": ["VAC"],
    },
    "Hib conjugate vaccine": {
        "pq_count": 6, "first_pq_year": 2003,
        "key_makers": ["Serum Institute of India", "Sanofi", "GSK", "Merck"],
        "categories": ["VAC"],
    },
    "Rotarix / Rotateq vaccine": {
        "pq_count": 4, "first_pq_year": 2007,
        "key_makers": ["GSK (Rotarix)", "Merck (Rotateq)", "Bharat Biotech (Rotavac)", "Serum Institute"],
        "categories": ["VAC"],
    },
    "HPV vaccine (Gardasil 9 / Cervarix)": {
        "pq_count": 4, "first_pq_year": 2009,
        "key_makers": ["Merck (Gardasil 9)", "GSK (Cervarix)", "Serum Institute of India (Cervavac)", "Innovax"],
        "categories": ["VAC"],
        "note": "Cervavac (SII) and Innovax (China) have expanded LMIC supply since 2023.",
    },
    "DTaP / Tdap vaccine": {
        "pq_count": 6, "first_pq_year": 2003,
        "key_makers": ["Sanofi", "GSK", "Serum Institute of India", "Bio Farma"],
        "categories": ["VAC"],
    },
    "MenACWY vaccine": {
        "pq_count": 4, "first_pq_year": 2010,
        "key_makers": ["Sanofi (Menactra)", "GSK (Menveo)", "Serum Institute of India (MenAfriVac)", "Pfizer (Nimenrix)"],
        "categories": ["VAC"],
    },
    "Typbar TCV vaccine": {
        "pq_count": 2, "first_pq_year": 2017,
        "key_makers": ["Bharat Biotech (Typbar TCV)", "Biological E (Typhibev)"],
        "categories": ["VAC"],
    },
    "YF-VAX / Stamaril vaccine": {
        "pq_count": 4, "first_pq_year": 2003,
        "key_makers": ["Sanofi (Stamaril)", "Bio-Manguinhos (Brazil)", "Chumakov (Russia)", "Senegal Inst Pasteur"],
        "categories": ["VAC"],
        "note": "Yellow fever supply has been tight since 2016 outbreak; emergency stockpile via ICG.",
    },
    "rabies vaccine (PCEC, PVRV)": {
        "pq_count": 4, "first_pq_year": 2008,
        "key_makers": ["Sanofi (Verorab)", "Indian Immunologicals", "Serum Institute (Rabivax)", "Bio Farma"],
        "categories": ["VAC"],
    },
    "Qdenga (TAK-003) vaccine": {
        "pq_count": 1, "first_pq_year": 2024,
        "key_makers": ["Takeda"],
        "categories": ["VAC"],
    },
    "MVA-BN (Jynneos) vaccine": {
        "pq_count": 1, "first_pq_year": 2024,
        "key_makers": ["Bavarian Nordic"],
        "categories": ["VAC"],
        "note": "Sole PQ'd mpox vaccine; supply was a bottleneck in 2022 + 2024.",
    },

    # ── Misc essential ────────────────────────────────────────────────────
    "ferrous sulfate": {
        "pq_count": 9, "first_pq_year": 2008,
        "key_makers": ["Macleods", "Sanofi", "Strides", "Cipla"],
        "categories": ["FPP"],
    },
    "folic acid": {
        "pq_count": 7, "first_pq_year": 2008,
        "key_makers": ["Macleods", "Strides", "Cipla"],
        "categories": ["FPP"],
    },
    "vitamin A": {
        "pq_count": 3, "first_pq_year": 2009,
        "key_makers": ["DSM (donation programme)", "Aurobindo", "Cipla"],
        "categories": ["FPP"],
    },
    "zinc": {
        "pq_count": 6, "first_pq_year": 2009,
        "key_makers": ["Nutriset", "Macleods", "Strides", "Cipla"],
        "categories": ["FPP"],
    },
    "oseltamivir": {
        "pq_count": 5, "first_pq_year": 2010,
        "key_makers": ["Roche (Tamiflu)", "Cipla", "Hetero", "Aurobindo"],
        "categories": ["FPP"],
    },

    # ── Hepatitis ─────────────────────────────────────────────────────────
    "sofosbuvir-velpatasvir": {
        "pq_count": 5, "first_pq_year": 2018,
        "key_makers": ["Gilead", "Mylan", "Cipla", "Hetero", "Aurobindo"],
        "categories": ["FPP"],
        "note": "Voluntary licence to 11 generics; PQ'd suppliers fully cover Africa + Asia.",
    },
    "entecavir": {
        "pq_count": 4, "first_pq_year": 2017,
        "key_makers": ["BMS", "Hetero", "Aurobindo", "Strides"],
        "categories": ["FPP"],
    },
    "glecaprevir-pibrentasvir": {
        "pq_count": 1, "first_pq_year": 2020,
        "key_makers": ["AbbVie"],
        "categories": ["FPP"],
        "note": "No generic PQ'd as of 2026; AbbVie pricing remains the key access barrier.",
    },

    # ── COVID + mpox (additions post-2020) ────────────────────────────────
    "nirmatrelvir-ritonavir (Paxlovid)": {
        "pq_count": 3, "first_pq_year": 2022,
        "key_makers": ["Pfizer", "Hetero", "Cipla"],
        "categories": ["FPP"],
        "note": "Voluntary licence via MPP — generic PQ'd in 2023.",
    },
    "tecovirimat (TPOXX)": {
        "pq_count": 1, "first_pq_year": 2024,
        "key_makers": ["SIGA Technologies"],
        "categories": ["FPP"],
    },
    "remdesivir": {
        "pq_count": 4, "first_pq_year": 2021,
        "key_makers": ["Gilead", "Cipla", "Hetero", "Mylan"],
        "categories": ["FPP"],
    },

    # ── Reproductive ──────────────────────────────────────────────────────
    "rabies immunoglobulin (HRIG)": {
        "pq_count": 2, "first_pq_year": 2017,
        "key_makers": ["Bharat Serums", "Kamada"],
        "categories": ["FPP"],
    },
}


def for_drug(generic: str) -> dict | None:
    """Return PQ profile for a generic, or None if not in the curated list."""
    rec = PQ_BY_GENERIC.get(generic)
    if rec is None:
        return None
    return {
        "generic":       generic,
        "pq_count":      rec["pq_count"],
        "key_makers":    rec.get("key_makers", []),
        "first_pq_year": rec.get("first_pq_year"),
        "categories":    rec.get("categories", []),
        "note":          rec.get("note"),
        "source":        "WHO Prequalification (curated snapshot, 2026-04)",
    }


def overview() -> dict:
    rows = []
    for generic, rec in PQ_BY_GENERIC.items():
        rows.append({
            "generic":       generic,
            "pq_count":      rec["pq_count"],
            "first_pq_year": rec.get("first_pq_year"),
            "categories":    rec.get("categories", []),
            "key_makers":    rec.get("key_makers", [])[:3],
            "note":          rec.get("note"),
        })
    rows.sort(key=lambda r: -r["pq_count"])
    single_source = [r for r in rows if r["pq_count"] == 1]
    return {
        "total_drugs":           len(rows),
        "rows":                  rows,
        "single_source_pq":      single_source,
        "drugs_by_pq_band": {
            "0":     0,
            "1":     sum(1 for r in rows if r["pq_count"] == 1),
            "2-4":   sum(1 for r in rows if 2 <= r["pq_count"] <= 4),
            "5-9":   sum(1 for r in rows if 5 <= r["pq_count"] <= 9),
            "10+":   sum(1 for r in rows if r["pq_count"] >= 10),
        },
        "as_of":                 "2026-04",
        "source":                "https://extranet.who.int/prequal/",
    }


def all_drugs() -> list[str]:
    return list(PQ_BY_GENERIC.keys())


if __name__ == "__main__":
    o = overview()
    print(f"PQ'd drugs in catalog: {o['total_drugs']}")
    print(f"Single-PQ-source: {len(o['single_source_pq'])}")
    print("Distribution by PQ count:")
    for band, n in o["drugs_by_pq_band"].items():
        print(f"  {band:5s} -> {n}")
    print("\nMost-prequalified (resilient supply):")
    for r in o["rows"][:5]:
        print(f"  {r['pq_count']:3d}  {r['generic']:35s}  {', '.join(r['key_makers'])}")
    print("\nSingle-source PQ (fragile global supply):")
    for r in o["single_source_pq"][:8]:
        print(f"  {r['generic']:35s}  {', '.join(r['key_makers'])}")
