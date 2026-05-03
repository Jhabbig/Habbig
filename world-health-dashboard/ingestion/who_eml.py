"""WHO Essential Medicines List (EML) → disease/drug mapping.

WHO publishes the EML as a long PDF/HTML document, with no machine-readable
API. We curate the most-frequently-used disease → first-line-drug mappings
here, sourced from WHO EML 24th edition (2025) and disease-specific WHO
guidelines. This is the kind of authoritative reference that doesn't need to
be 100% comprehensive to be useful — it covers the leading killers.

For each drug we list the INN (international non-proprietary name = generic
name); RxNorm resolution to brand names happens in `rxnorm.py`.

To extend: add an entry under `EML_BY_DISEASE` keyed by the WHO factsheet
slug. Multiple drugs can be listed; the first is treated as first-line.
"""

from __future__ import annotations

# Disease slug (matches WHO factsheet slugs) → list of essential medicines.
# Each entry: (generic_name, role, notes)
#   role ∈ {"first-line", "alternative", "adjunct", "vaccine", "prophylaxis"}
EML_BY_DISEASE: dict[str, list[tuple[str, str, str]]] = {
    "malaria": [
        ("artemether-lumefantrine", "first-line", "Uncomplicated P. falciparum (ACT)"),
        ("artesunate", "first-line", "Severe malaria, IV / IM"),
        ("dihydroartemisinin-piperaquine", "alternative", "Uncomplicated falciparum"),
        ("primaquine", "adjunct", "Radical cure of P. vivax / ovale"),
        ("chloroquine", "alternative", "P. vivax in chloroquine-sensitive areas"),
        ("RTS,S/AS01 vaccine", "vaccine", "Children in endemic areas"),
        ("R21/Matrix-M vaccine", "vaccine", "Approved 2023"),
    ],
    "tuberculosis": [
        ("isoniazid", "first-line", "Part of HRZE regimen"),
        ("rifampicin", "first-line", "HRZE first 2 mo, HR continuation"),
        ("pyrazinamide", "first-line", "First 2 mo only"),
        ("ethambutol", "first-line", "First 2 mo only"),
        ("bedaquiline", "alternative", "MDR/XDR-TB"),
        ("linezolid", "alternative", "MDR/XDR-TB"),
        ("BCG vaccine", "vaccine", "Childhood; protective against severe paeds TB"),
    ],
    "hiv-aids": [
        ("dolutegravir", "first-line", "INSTI, part of TLD"),
        ("tenofovir disoproxil fumarate", "first-line", "NRTI"),
        ("lamivudine", "first-line", "NRTI"),
        ("efavirenz", "alternative", "NNRTI, second-line"),
        ("emtricitabine", "first-line", "NRTI"),
        ("rilpivirine", "alternative", "NNRTI"),
        ("cabotegravir LA", "prophylaxis", "Long-acting PrEP"),
    ],
    "cholera": [
        ("oral rehydration salts", "first-line", "Cornerstone of treatment"),
        ("doxycycline", "alternative", "Adults"),
        ("azithromycin", "alternative", "Children, pregnant women"),
        ("Dukoral / Shanchol vaccine", "vaccine", "Oral cholera vaccine"),
    ],
    "diabetes": [
        ("metformin", "first-line", "Type 2 diabetes"),
        ("insulin (regular, NPH, glargine)", "first-line", "Type 1 + advanced T2D"),
        ("glibenclamide", "alternative", "Sulfonylurea, low-cost"),
        ("gliclazide", "alternative", "Sulfonylurea, preferred over glibenclamide"),
        ("empagliflozin", "alternative", "SGLT2 inhibitor"),
        ("semaglutide", "alternative", "GLP-1 agonist (Ozempic)"),
    ],
    "hypertension": [
        ("amlodipine", "first-line", "Calcium channel blocker"),
        ("hydrochlorothiazide", "first-line", "Thiazide diuretic"),
        ("lisinopril", "first-line", "ACE inhibitor"),
        ("losartan", "first-line", "ARB"),
        ("atenolol", "alternative", "Beta-blocker"),
    ],
    "cardiovascular-diseases-(cvds)": [
        ("aspirin (low-dose)", "first-line", "Secondary prevention"),
        ("atorvastatin", "first-line", "Statin"),
        ("clopidogrel", "first-line", "Antiplatelet post-ACS"),
        ("metoprolol", "first-line", "Beta-blocker"),
        ("ramipril", "first-line", "ACE inhibitor"),
    ],
    "asthma": [
        ("salbutamol", "first-line", "SABA, rescue"),
        ("budesonide", "first-line", "ICS, controller"),
        ("formoterol", "first-line", "LABA"),
        ("budesonide-formoterol", "first-line", "Combined controller / reliever"),
    ],
    "epilepsy": [
        ("phenytoin", "first-line", ""),
        ("carbamazepine", "first-line", "Focal seizures"),
        ("valproate", "first-line", "Generalized; avoid in pregnancy"),
        ("lamotrigine", "alternative", ""),
        ("levetiracetam", "alternative", ""),
    ],
    "depression": [
        ("fluoxetine", "first-line", "SSRI"),
        ("sertraline", "first-line", "SSRI"),
        ("amitriptyline", "alternative", "TCA"),
    ],
    "schizophrenia": [
        ("haloperidol", "first-line", "Typical antipsychotic"),
        ("risperidone", "first-line", "Atypical"),
        ("clozapine", "alternative", "Treatment-resistant"),
        ("olanzapine", "alternative", ""),
    ],
    "measles": [
        ("vitamin A", "adjunct", "All measles cases — reduces mortality"),
        ("MMR vaccine", "vaccine", "Routine immunization, 2 doses"),
    ],
    "polio-and-other-acute-paralysis": [
        ("OPV vaccine", "vaccine", "Bivalent, type-1+3"),
        ("IPV vaccine", "vaccine", "Routine, supplements OPV"),
    ],
    "hepatitis-b": [
        ("tenofovir disoproxil fumarate", "first-line", "Chronic HBV"),
        ("entecavir", "alternative", ""),
        ("HepB vaccine", "vaccine", "Birth dose + 3 follow-up"),
    ],
    "hepatitis-c": [
        ("sofosbuvir-velpatasvir", "first-line", "Pan-genotypic DAA"),
        ("glecaprevir-pibrentasvir", "alternative", "Pan-genotypic DAA"),
    ],
    "ebola-disease": [
        ("inmazeb (atoltivimab/maftivimab/odesivimab)", "first-line", "Anti-Ebola mAb cocktail"),
        ("ansuvimab (Ebanga)", "alternative", "Single mAb"),
        ("rVSV-ZEBOV (Ervebo) vaccine", "vaccine", "Zaire ebolavirus"),
    ],
    "marburg-virus-disease": [
        ("supportive care", "first-line", "No approved specific therapeutic; rehydration + ICU"),
    ],
    "mpox": [
        ("tecovirimat (TPOXX)", "first-line", "Used under EUA / expanded access"),
        ("MVA-BN (Jynneos) vaccine", "vaccine", "Two-dose subq"),
    ],
    "dengue-and-severe-dengue": [
        ("paracetamol", "first-line", "Symptomatic only — avoid NSAIDs"),
        ("Qdenga (TAK-003) vaccine", "vaccine", "Live attenuated tetravalent"),
    ],
    "yellow-fever": [
        ("YF-VAX / Stamaril vaccine", "vaccine", "Single dose, lifelong immunity"),
    ],
    "rabies": [
        ("rabies immunoglobulin (HRIG)", "first-line", "Post-exposure"),
        ("rabies vaccine (PCEC, PVRV)", "first-line", "Post-exposure 4-5 doses"),
    ],
    "pneumonia": [
        ("amoxicillin", "first-line", "Community-acquired, paeds"),
        ("ceftriaxone", "alternative", "Severe / hospital"),
        ("azithromycin", "alternative", "Atypical coverage"),
        ("PCV13/PCV15/PCV20 vaccine", "vaccine", "Pneumococcal"),
    ],
    "obesity-and-overweight": [
        ("semaglutide", "first-line", "GLP-1 (Ozempic, Wegovy)"),
        ("tirzepatide", "first-line", "GIP/GLP-1 (Mounjaro, Zepbound)"),
        ("liraglutide", "alternative", "GLP-1 (Saxenda)"),
        ("orlistat", "alternative", "Lipase inhibitor"),
    ],
    "antimicrobial-resistance": [
        ("(see specific infections)", "first-line", "Stewardship rather than single drug"),
    ],
    "anaemia": [
        ("ferrous sulfate", "first-line", "Iron-deficiency anaemia"),
        ("folic acid", "first-line", "Megaloblastic / preconception"),
        ("vitamin B12", "first-line", "Pernicious anaemia"),
    ],
    "diarrhoeal-disease": [
        ("oral rehydration salts", "first-line", "Cornerstone"),
        ("zinc", "adjunct", "Reduces duration in children"),
    ],
    "leprosy": [
        ("dapsone", "first-line", "Multidrug therapy"),
        ("rifampicin", "first-line", "MDT"),
        ("clofazimine", "first-line", "MDT for multibacillary"),
    ],
    "schistosomiasis": [
        ("praziquantel", "first-line", "Mass drug administration"),
    ],
    "lymphatic-filariasis": [
        ("ivermectin", "first-line", "MDA"),
        ("albendazole", "first-line", "MDA"),
        ("diethylcarbamazine (DEC)", "first-line", "MDA in non-onchocerciasis areas"),
    ],
    "onchocerciasis": [
        ("ivermectin", "first-line", "Annual MDA"),
    ],
    "soil-transmitted-helminth-infections": [
        ("albendazole", "first-line", "MDA in school-age children"),
        ("mebendazole", "first-line", "MDA alternative"),
    ],
    "trachoma": [
        ("azithromycin", "first-line", "MDA"),
    ],
    "leishmaniasis": [
        ("liposomal amphotericin B", "first-line", "Visceral"),
        ("miltefosine", "alternative", "Oral, visceral / cutaneous"),
        ("paromomycin", "alternative", ""),
    ],
    "human-african-trypanosomiasis-(sleeping-sickness)": [
        ("fexinidazole", "first-line", "First oral, all stages"),
        ("nifurtimox-eflornithine combination therapy (NECT)", "alternative", "T. b. gambiense stage 2"),
    ],
    "buruli-ulcer-(mycobacterium-ulcerans-infection)": [
        ("rifampicin", "first-line", ""),
        ("clarithromycin", "first-line", ""),
    ],
    "typhoid": [
        ("ceftriaxone", "first-line", "Severe / MDR"),
        ("azithromycin", "first-line", "Uncomplicated"),
        ("ciprofloxacin", "alternative", "Where susceptible"),
        ("Typbar TCV vaccine", "vaccine", "Typhoid conjugate"),
    ],
    "meningitis": [
        ("ceftriaxone", "first-line", "Empirical bacterial"),
        ("ampicillin", "adjunct", "Listeria coverage"),
        ("MenACWY vaccine", "vaccine", "Quadrivalent meningococcal"),
        ("MenB vaccine", "vaccine", "Bexsero / Trumenba"),
    ],
    "haemophilus-influenzae-type-b-(hib)": [
        ("Hib conjugate vaccine", "vaccine", "Routine immunization"),
    ],
    "rotavirus": [
        ("Rotarix / Rotateq vaccine", "vaccine", "Oral, infant"),
        ("oral rehydration salts", "first-line", "Diarrhoea management"),
    ],
    "cervical-cancer": [
        ("HPV vaccine (Gardasil 9 / Cervarix)", "vaccine", "Adolescent girls (and boys)"),
    ],
    "pertussis": [
        ("azithromycin", "first-line", ""),
        ("DTaP / Tdap vaccine", "vaccine", "Routine + boosters"),
    ],
    "covid-19": [
        ("nirmatrelvir-ritonavir (Paxlovid)", "first-line", "Outpatient, high-risk, within 5d"),
        ("dexamethasone", "first-line", "Hospitalized requiring O2"),
        ("remdesivir", "alternative", "Hospitalized"),
        ("COVID-19 vaccines (mRNA / protein)", "vaccine", "Pfizer/BioNTech, Moderna, Novavax"),
    ],
    "influenza-(seasonal)": [
        ("oseltamivir", "first-line", "Within 48h of symptom onset"),
        ("baloxavir marboxil", "alternative", "Single-dose"),
        ("seasonal influenza vaccine", "vaccine", "Annual"),
    ],
    "stroke": [
        ("alteplase", "first-line", "tPA, within 4.5h"),
        ("tenecteplase", "alternative", "Trial-use thrombolytic"),
        ("aspirin", "first-line", "Within 24-48h post-tPA"),
    ],
    "chronic-obstructive-pulmonary-disease-(copd)": [
        ("tiotropium", "first-line", "LAMA"),
        ("salmeterol", "first-line", "LABA"),
        ("budesonide-formoterol", "alternative", ""),
    ],
}


def for_disease(slug: str) -> list[dict]:
    """Return treatment list for a disease slug, or [] if not curated."""
    items = EML_BY_DISEASE.get(slug, [])
    return [{"generic": g, "role": r, "notes": n} for g, r, n in items]


def all_drugs() -> set[str]:
    """All unique generic-drug names across the catalog."""
    out: set[str] = set()
    for items in EML_BY_DISEASE.values():
        for g, _, _ in items:
            out.add(g)
    return out


def diseases_with_treatment() -> list[str]:
    return sorted(EML_BY_DISEASE.keys())


if __name__ == "__main__":
    print(f"Diseases with curated treatment: {len(EML_BY_DISEASE)}")
    print(f"Unique drugs: {len(all_drugs())}")
    print("\nMalaria treatment:")
    for d in for_disease("malaria"):
        print(f"  [{d['role']}] {d['generic']:40s} — {d['notes']}")
