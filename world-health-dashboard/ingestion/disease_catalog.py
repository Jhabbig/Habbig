"""Disease catalog — assembles ~500 diseases from WHO factsheets + curated stubs.

Two sources:
  • WHO Fact Sheets (rich data)  — 211 entries with overview, symptoms,
    treatment, prevention sections parsed from WHO HTML.
  • Curated stub list (this file) — ~290 additional diseases organized by
    ICD-10 chapter. Each stub has at minimum: slug, name, category, ICD-10
    range, one-line summary. Many also have curated-treatment links via
    `who_eml`.

Combined and deduplicated by slug, the catalog is the disease universe the
atlas exposes. WHO entries take precedence when slugs collide (richer data).
"""

from __future__ import annotations

# ICD-10 chapters → broad category labels we use in the UI.
ICD_CHAPTERS: list[tuple[str, str, str]] = [
    ("A00-B99", "Infectious & parasitic", "🦠"),
    ("C00-D49", "Cancers & neoplasms",    "🧬"),
    ("D50-D89", "Blood & immune",         "🩸"),
    ("E00-E89", "Endocrine & metabolic",  "🧪"),
    ("F01-F99", "Mental & behavioural",   "🧠"),
    ("G00-G99", "Nervous system",         "⚡"),
    ("H00-H59", "Eye",                    "👁"),
    ("H60-H95", "Ear",                    "👂"),
    ("I00-I99", "Cardiovascular",         "❤"),
    ("J00-J99", "Respiratory",            "🫁"),
    ("K00-K95", "Digestive",              "🍽"),
    ("L00-L99", "Skin",                   "🩹"),
    ("M00-M99", "Musculoskeletal",        "🦴"),
    ("N00-N99", "Genitourinary",          "🚻"),
    ("O00-O9A", "Pregnancy & childbirth", "🤰"),
    ("P00-P96", "Perinatal",              "👶"),
    ("Q00-Q99", "Congenital",             "🧬"),
    ("R00-R99", "Symptoms",               "❓"),
    ("S00-T88", "Injury & poisoning",     "🚑"),
    ("V00-Y99", "External causes",        "⚠"),
]

CATEGORY_BY_CHAPTER: dict[str, str] = {ch: name for ch, name, _ in ICD_CHAPTERS}


# ─── Curated stub diseases ──────────────────────────────────────────────────
# Each: (slug, display_name, ICD-10 chapter prefix, ICD-10 code or "", summary)

# Note: some slugs intentionally overlap with WHO factsheet slugs; merging
# logic in `assemble()` prefers WHO data. We keep stubs for slugs WHO covers
# so the category/icd10 metadata is available even on the rich record.

STUBS: list[tuple[str, str, str, str, str]] = [
    # ── Infectious & parasitic (A00-B99) ────────────────────────────────────
    ("anthrax",                "Anthrax",                       "A00-B99", "A22",   "Bacterial zoonosis caused by Bacillus anthracis."),
    ("brucellosis",            "Brucellosis",                   "A00-B99", "A23",   "Zoonotic bacterial infection from livestock."),
    ("plague",                 "Plague",                        "A00-B99", "A20",   "Yersinia pestis infection — bubonic, septicaemic, pneumonic forms."),
    ("listeriosis",            "Listeriosis",                   "A00-B99", "A32",   "Foodborne Listeria monocytogenes infection."),
    ("legionellosis",          "Legionellosis",                 "A00-B99", "A48.1", "Legionnaires' disease + Pontiac fever."),
    ("salmonellosis",          "Salmonellosis (non-typhoidal)", "A00-B99", "A02",   "Foodborne Salmonella enterica gastroenteritis."),
    ("shigellosis",            "Shigellosis",                   "A00-B99", "A03",   "Bloody diarrhoea from Shigella."),
    ("campylobacteriosis",     "Campylobacteriosis",            "A00-B99", "A04.5", "Most common bacterial cause of diarrhoea worldwide."),
    ("escherichia-coli-stec",  "STEC / EHEC",                   "A00-B99", "A04.3", "Shiga-toxin-producing E. coli — HUS risk."),
    ("toxoplasmosis",          "Toxoplasmosis",                 "A00-B99", "B58",   "T. gondii infection — risk in pregnancy + immunocompromised."),
    ("cryptosporidiosis",      "Cryptosporidiosis",             "A00-B99", "A07.2", "Waterborne protozoan diarrhoea."),
    ("giardiasis",             "Giardiasis",                    "A00-B99", "A07.1", "Waterborne protozoan diarrhoea."),
    ("amoebiasis",             "Amoebiasis",                    "A00-B99", "A06",   "Entamoeba histolytica — invasive colitis + liver abscess."),
    ("chagas-disease",         "Chagas disease",                "A00-B99", "B57",   "Trypanosoma cruzi — Latin American + cardiomyopathy."),
    ("japanese-encephalitis",  "Japanese encephalitis",         "A00-B99", "A83.0", "Vector-borne flavivirus, Asia-Pacific."),
    ("west-nile-virus",        "West Nile virus",               "A00-B99", "A92.3", "Vector-borne flavivirus, encephalitis risk."),
    ("crimean-congo-haemorrhagic-fever", "CCHF",                "A00-B99", "A98.0", "Tick-borne haemorrhagic fever, ~30% CFR."),
    ("lassa-fever",            "Lassa fever",                   "A00-B99", "A96.2", "West African arenavirus, rodent-borne."),
    ("nipah-virus-infection",  "Nipah virus infection",         "A00-B99", "B33.8", "Bat-borne henipavirus, high CFR encephalitis."),
    ("hantavirus",             "Hantavirus",                    "A00-B99", "A98.5", "Rodent-borne — pulmonary syndrome (Americas) or HFRS (Asia)."),
    ("scrub-typhus",           "Scrub typhus",                  "A00-B99", "A75.3", "Mite-borne Orientia tsutsugamushi, Asia-Pacific."),
    ("syphilis",               "Syphilis",                      "A00-B99", "A53",   "Treponema pallidum — primary, secondary, tertiary stages."),
    ("gonorrhoea",             "Gonorrhoea",                    "A00-B99", "A54",   "N. gonorrhoeae — increasingly resistant to last-line ABx."),
    ("chlamydia",              "Chlamydia",                     "A00-B99", "A56",   "Most common bacterial STI."),
    ("genital-herpes",         "Genital herpes (HSV-2)",        "A00-B99", "A60",   "Lifelong recurrent vesicular STI."),
    ("trichomoniasis",         "Trichomoniasis",                "A00-B99", "A59",   "Most common curable STI globally."),
    ("epstein-barr-virus",     "EBV mononucleosis",             "A00-B99", "B27.0", "Common viral; lymphoma associations."),
    ("cytomegalovirus",        "CMV infection",                 "A00-B99", "B25",   "Risk in pregnancy + immunocompromised."),
    ("varicella-(chickenpox)", "Varicella (chickenpox)",        "A00-B99", "B01",   "VZV primary infection — vaccine-preventable."),
    ("herpes-zoster-(shingles)","Herpes zoster (shingles)",     "A00-B99", "B02",   "VZV reactivation — Shingrix vaccine recommended 50+."),
    ("rubella",                "Rubella",                       "A00-B99", "B06",   "Mild in adults; congenital syndrome catastrophic."),
    ("mumps",                  "Mumps",                         "A00-B99", "B26",   "Vaccine-preventable parotitis."),
    ("rsv",                    "Respiratory syncytial virus",   "A00-B99", "B97.4", "Major cause of paediatric LRTI; nirsevimab + maternal vaccine 2024."),
    ("norovirus",              "Norovirus",                     "A00-B99", "A08.1", "Most common cause of acute gastroenteritis."),
    ("rotavirus-infection",    "Rotavirus gastroenteritis",     "A00-B99", "A08.0", "Vaccine-preventable, leading cause of paeds severe diarrhoea."),
    ("trichinellosis",         "Trichinellosis",                "A00-B99", "B75",   "Trichinella from undercooked pork/wildlife."),
    ("echinococcosis",         "Echinococcosis (hydatid)",      "A00-B99", "B67",   "Tapeworm cysts — surgery + albendazole."),
    ("cysticercosis",          "Cysticercosis",                 "A00-B99", "B69",   "T. solium — leading cause of acquired epilepsy in LMICs."),
    ("loiasis",                "Loiasis",                       "A00-B99", "B74.3", "Eyeworm; complicates onchocerciasis MDA."),
    ("dracunculiasis",         "Dracunculiasis (Guinea worm)",  "A00-B99", "B72",   "Near-eradicated; <20 human cases/yr."),
    ("yaws",                   "Yaws",                          "A00-B99", "A66",   "Treponema pallidum pertenue — single-dose azithromycin treatment."),
    ("scabies",                "Scabies",                       "A00-B99", "B86",   "Sarcoptes scabiei — treatable with permethrin/ivermectin."),

    # ── Cancers & neoplasms (C00-D49) ───────────────────────────────────────
    ("lung-cancer",            "Lung cancer",                   "C00-D49", "C34",   "Leading cancer killer; smoking + radon + air pollution."),
    ("breast-cancer",          "Breast cancer",                 "C00-D49", "C50",   "Most-common cancer in women; HER2 + hormone-receptor subtypes."),
    ("colorectal-cancer",      "Colorectal cancer",             "C00-D49", "C18-C20", "3rd most-common cancer; screening saves lives."),
    ("prostate-cancer",        "Prostate cancer",               "C00-D49", "C61",   "Most-common cancer in men; PSA controversy."),
    ("stomach-cancer",         "Gastric cancer",                "C00-D49", "C16",   "H. pylori is the leading cause."),
    ("liver-cancer",           "Hepatocellular carcinoma",      "C00-D49", "C22",   "HBV/HCV cirrhosis + aflatoxin."),
    ("pancreatic-cancer",      "Pancreatic cancer",             "C00-D49", "C25",   "Worst-prognosis common cancer; smoking + family history."),
    ("oesophageal-cancer",     "Oesophageal cancer",            "C00-D49", "C15",   "Squamous (alcohol/tobacco) vs adeno (GERD/Barrett's)."),
    ("bladder-cancer",         "Bladder cancer",                "C00-D49", "C67",   "Smoking + occupational aromatic amines."),
    ("kidney-cancer",          "Renal cell carcinoma",          "C00-D49", "C64",   "Smoking + obesity + hypertension."),
    ("thyroid-cancer",         "Thyroid cancer",                "C00-D49", "C73",   "Mostly indolent papillary; rising incidence partly overdiagnosis."),
    ("leukaemia",              "Leukaemia",                     "C00-D49", "C91-C95", "Acute (AML/ALL) + chronic (CML/CLL); paediatric ALL >90% cure."),
    ("lymphoma-non-hodgkin",   "Non-Hodgkin lymphoma",          "C00-D49", "C82-C85", "Heterogeneous B-cell + T-cell malignancies."),
    ("lymphoma-hodgkin",       "Hodgkin lymphoma",              "C00-D49", "C81",   "Reed-Sternberg cells; >85% cure rate."),
    ("multiple-myeloma",       "Multiple myeloma",              "C00-D49", "C90",   "Plasma cell malignancy; bone lesions + renal failure."),
    ("melanoma",               "Melanoma",                      "C00-D49", "C43",   "Skin cancer; UV + family history; checkpoint inhibitors transform survival."),
    ("ovarian-cancer",         "Ovarian cancer",                "C00-D49", "C56",   "Often diagnosed late; BRCA association."),
    ("uterine-cancer",         "Uterine cancer",                "C00-D49", "C54-C55", "Endometrial cancer most common; obesity-linked."),
    ("brain-cancer",           "Brain & CNS cancer",            "C00-D49", "C71",   "Glioblastoma highly lethal; meningioma usually benign."),
    ("childhood-leukaemia",    "Childhood leukaemia",           "C00-D49", "C91.0", "Most common paediatric cancer; ALL responds to combination chemo."),

    # ── Blood & immune (D50-D89) ────────────────────────────────────────────
    ("iron-deficiency-anaemia","Iron-deficiency anaemia",       "D50-D89", "D50",   "Most common anaemia globally."),
    ("sickle-cell-disease",    "Sickle cell disease",           "D50-D89", "D57",   "Inherited; voxelotor + crizanlizumab + bone marrow transplant + new gene therapies."),
    ("thalassaemia",           "Thalassaemia",                  "D50-D89", "D56",   "Inherited Hb chain disorders; transfusion-dependent forms."),
    ("haemophilia",            "Haemophilia",                   "D50-D89", "D66-D67", "X-linked Factor VIII (A) or IX (B); novel gene therapy approved."),
    ("idiopathic-thrombocytopenia","ITP",                       "D50-D89", "D69.3", "Autoimmune low platelets; steroids first-line."),
    ("aplastic-anaemia",       "Aplastic anaemia",              "D50-D89", "D61",   "Pancytopenia; immunosuppression + BMT."),
    ("primary-immunodeficiency","Primary immunodeficiency",     "D50-D89", "D80-D84", "Inherited; IVIG + early infection control."),

    # ── Endocrine & metabolic (E00-E89) ─────────────────────────────────────
    ("type-1-diabetes",        "Type 1 diabetes",               "E00-E89", "E10",   "Autoimmune β-cell destruction; lifelong insulin."),
    ("type-2-diabetes",        "Type 2 diabetes",               "E00-E89", "E11",   "Insulin resistance + relative deficiency; lifestyle + metformin first."),
    ("gestational-diabetes",   "Gestational diabetes",          "E00-E89", "O24.4", "Onset in pregnancy; T2D risk in mother."),
    ("hypothyroidism",         "Hypothyroidism",                "E00-E89", "E03",   "Hashimoto's most common cause; levothyroxine."),
    ("hyperthyroidism",        "Hyperthyroidism",               "E00-E89", "E05",   "Graves' disease most common; methimazole, radioiodine."),
    ("addisons-disease",       "Adrenal insufficiency",         "E00-E89", "E27.1", "Hydrocortisone + fludrocortisone replacement."),
    ("cushing-syndrome",       "Cushing syndrome",              "E00-E89", "E24",   "Endogenous cortisol excess; pituitary tumour most common."),
    ("metabolic-syndrome",     "Metabolic syndrome",            "E00-E89", "E88.81","Cluster: obesity + dyslipidaemia + insulin resistance + hypertension."),
    ("hyperlipidaemia",        "Hyperlipidaemia",               "E00-E89", "E78",   "LDL-C reduction is foundational for CVD prevention."),
    ("gout",                   "Gout",                          "E00-E89", "M10",   "Hyperuricaemia + crystal arthritis; allopurinol prophylaxis."),
    ("pku",                    "Phenylketonuria (PKU)",         "E00-E89", "E70.0", "Newborn screening + dietary phe restriction."),
    ("galactosaemia",          "Galactosaemia",                 "E00-E89", "E74.2", "Newborn screening; lactose-free diet."),
    ("vitamin-d-deficiency",   "Vitamin D deficiency",          "E00-E89", "E55",   "Common globally; supplementation."),
    ("iodine-deficiency",      "Iodine deficiency",             "E00-E89", "E01",   "Goitre + cretinism; salt iodization."),
    ("kwashiorkor",            "Kwashiorkor",                   "E00-E89", "E40",   "Acute severe malnutrition with oedema."),
    ("marasmus",               "Marasmus",                      "E00-E89", "E41",   "Severe energy deficiency; ready-to-use therapeutic foods."),

    # ── Mental & behavioural (F01-F99) ──────────────────────────────────────
    ("anxiety-disorders",      "Anxiety disorders",             "F01-F99", "F40-F41", "GAD, panic, social anxiety; SSRIs + CBT."),
    ("bipolar-disorder",       "Bipolar disorder",              "F01-F99", "F31",   "Mood stabilisers (lithium, valproate)."),
    ("ptsd",                   "Post-traumatic stress disorder","F01-F99", "F43.1", "Trauma-focused CBT, EMDR, SSRIs."),
    ("ocd",                    "Obsessive-compulsive disorder", "F01-F99", "F42",   "SSRIs + ERP therapy."),
    ("autism-spectrum",        "Autism spectrum disorder",      "F01-F99", "F84.0", "Neurodevelopmental; early intensive intervention."),
    ("adhd",                   "ADHD",                          "F01-F99", "F90",   "Methylphenidate, amphetamines, atomoxetine."),
    ("eating-disorders",       "Eating disorders",              "F01-F99", "F50",   "Anorexia, bulimia, binge-eating; CBT + nutritional rehab."),
    ("substance-use-disorders","Substance use disorders",       "F01-F99", "F10-F19", "MAT (methadone, buprenorphine, naltrexone) for OUD."),
    ("dementia-alzheimers",    "Alzheimer's disease",           "F01-F99", "F00",   "Most common dementia; lecanemab/donanemab modify."),
    ("dementia-vascular",      "Vascular dementia",             "F01-F99", "F01",   "Stepwise decline post-stroke; risk-factor control."),
    ("learning-disabilities",  "Intellectual disability",       "F01-F99", "F70-F79","Heterogeneous; supportive."),
    ("personality-disorders",  "Personality disorders",         "F01-F99", "F60",   "DBT for borderline; long-term psychotherapy."),

    # ── Nervous system (G00-G99) ────────────────────────────────────────────
    ("parkinsons-disease",     "Parkinson's disease",           "G00-G99", "G20",   "Dopamine-replacement (levodopa) + adjuncts."),
    ("multiple-sclerosis",     "Multiple sclerosis",            "G00-G99", "G35",   "Demyelinating; high-efficacy DMTs (ocrelizumab, ofatumumab)."),
    ("als",                    "Amyotrophic lateral sclerosis", "G00-G99", "G12.2", "Riluzole + edaravone; Tofersen for SOD1 ALS."),
    ("migraine",               "Migraine",                      "G00-G99", "G43",   "CGRP mAbs + gepants + triptans."),
    ("tension-headache",       "Tension-type headache",         "G00-G99", "G44.2", "Most common headache."),
    ("cluster-headache",       "Cluster headache",              "G00-G99", "G44.0", "Triptans + verapamil + galcanezumab."),
    ("trigeminal-neuralgia",   "Trigeminal neuralgia",          "G00-G99", "G50.0", "Carbamazepine first-line."),
    ("guillain-barre",         "Guillain-Barré syndrome",       "G00-G99", "G61.0", "Acute demyelinating; IVIG / plasmapheresis."),
    ("myasthenia-gravis",      "Myasthenia gravis",             "G00-G99", "G70.0", "AChR antibodies; pyridostigmine + immunosuppression."),
    ("huntingtons-disease",    "Huntington's disease",          "G00-G99", "G10",   "Autosomal dominant; tetrabenazine + supportive."),
    ("cerebral-palsy",         "Cerebral palsy",                "G00-G99", "G80",   "Non-progressive perinatal brain injury."),
    ("spina-bifida",           "Spina bifida",                  "G00-G99", "Q05",   "Folic acid prevention; in-utero closure trials."),
    ("narcolepsy",             "Narcolepsy",                    "G00-G99", "G47.4", "Modafinil, sodium oxybate."),
    ("restless-legs",          "Restless legs syndrome",        "G00-G99", "G25.81","Iron, dopamine agonists, gabapentinoids."),

    # ── Eye (H00-H59) ───────────────────────────────────────────────────────
    ("cataract",               "Cataract",                      "H00-H59", "H25",   "Leading cause of preventable blindness; surgery curative."),
    ("glaucoma",               "Glaucoma",                      "H00-H59", "H40",   "IOP lowering — timolol, latanoprost, surgery."),
    ("amd",                    "Age-related macular degeneration","H00-H59", "H35.3", "Anti-VEGF for wet AMD."),
    ("diabetic-retinopathy",   "Diabetic retinopathy",          "H00-H59", "H36",   "Anti-VEGF + laser; leading cause of vision loss in working age."),
    ("refractive-error",       "Refractive error",              "H00-H59", "H52",   "Most-common cause of vision impairment globally."),
    ("retinal-detachment",     "Retinal detachment",            "H00-H59", "H33",   "Surgical emergency."),
    ("conjunctivitis",         "Conjunctivitis",                "H00-H59", "H10",   "Viral / bacterial / allergic."),
    ("dry-eye",                "Dry eye disease",               "H00-H59", "H04.12","Tears, cyclosporine drops, varenicline nasal."),
    ("uveitis",                "Uveitis",                       "H00-H59", "H20",   "Inflammation; steroid drops, biologics."),

    # ── Ear (H60-H95) ───────────────────────────────────────────────────────
    ("otitis-media",           "Otitis media",                  "H60-H95", "H66",   "Common in children; watchful waiting + amoxicillin."),
    ("hearing-loss",           "Hearing loss",                  "H60-H95", "H90",   "Age-related most common; hearing aids + cochlear implants."),
    ("tinnitus",               "Tinnitus",                      "H60-H95", "H93.1", "Often idiopathic; CBT + sound therapy."),
    ("vertigo",                "Vertigo",                       "H60-H95", "H81",   "BPPV most common — Epley manoeuvre."),
    ("menieres",               "Ménière's disease",             "H60-H95", "H81.0", "Episodic vertigo + hearing loss + tinnitus."),

    # ── Cardiovascular (I00-I99) ────────────────────────────────────────────
    ("ischaemic-heart-disease","Ischaemic heart disease",       "I00-I99", "I20-I25", "Leading killer worldwide."),
    ("myocardial-infarction",  "Myocardial infarction",         "I00-I99", "I21",   "STEMI: PCI <90min; NSTEMI: optimal medical + early invasive."),
    ("heart-failure",          "Heart failure",                 "I00-I99", "I50",   "ARNI + beta-blocker + MRA + SGLT2i (the 'four pillars')."),
    ("atrial-fibrillation",    "Atrial fibrillation",           "I00-I99", "I48",   "Rate/rhythm control + anticoagulation; ablation."),
    ("ventricular-arrhythmia", "Ventricular arrhythmia",        "I00-I99", "I47",   "ICD for high-risk patients."),
    ("cardiomyopathy-dilated", "Dilated cardiomyopathy",        "I00-I99", "I42.0", "Genetic + post-viral; HF therapy."),
    ("cardiomyopathy-hypertrophic","Hypertrophic cardiomyopathy","I00-I99", "I42.1", "Most common inherited heart disease; mavacamten 2022."),
    ("rheumatic-heart-disease","Rheumatic heart disease",       "I00-I99", "I05-I09", "Sequela of Group A strep; LMIC burden."),
    ("ischaemic-stroke",       "Ischaemic stroke",              "I00-I99", "I63",   "tPA <4.5h, thrombectomy <24h; secondary prevention."),
    ("haemorrhagic-stroke",    "Haemorrhagic stroke",           "I00-I99", "I61",   "BP control, neurosurgery for select."),
    ("aortic-aneurysm",        "Aortic aneurysm",               "I00-I99", "I71",   "AAA screening in 65+ men smokers."),
    ("aortic-dissection",      "Aortic dissection",             "I00-I99", "I71.0", "Surgical emergency for type A."),
    ("dvt-pe",                 "Venous thromboembolism (DVT/PE)","I00-I99", "I80-I82","Anticoagulation; DOACs first-line."),
    ("varicose-veins",         "Varicose veins",                "I00-I99", "I83",   "Endovenous laser / sclerotherapy."),
    ("pad",                    "Peripheral artery disease",     "I00-I99", "I73.9", "Smoking cessation + statin + antiplatelet."),
    ("pericarditis",           "Pericarditis",                  "I00-I99", "I30",   "Colchicine + NSAIDs."),
    ("endocarditis",           "Infective endocarditis",        "I00-I99", "I33",   "Long-course IV ABx + valve surgery if indicated."),

    # ── Respiratory (J00-J99) ───────────────────────────────────────────────
    ("acute-bronchitis",       "Acute bronchitis",              "J00-J99", "J20",   "Usually viral; supportive care."),
    ("bronchiolitis",          "Bronchiolitis (RSV)",           "J00-J99", "J21",   "Infant LRTI; supportive care + nirsevimab prevention."),
    ("sleep-apnoea",           "Obstructive sleep apnoea",      "J00-J99", "G47.33","CPAP + weight loss + GLP-1 (tirzepatide approved 2024)."),
    ("pulmonary-embolism",     "Pulmonary embolism",            "J00-J99", "I26",   "Anticoagulation; thrombolysis if hemodynamically unstable."),
    ("interstitial-lung-disease","Interstitial lung disease",   "J00-J99", "J84",   "Pirfenidone, nintedanib for IPF."),
    ("cystic-fibrosis",        "Cystic fibrosis",               "J00-J99", "E84",   "CFTR modulators (elexacaftor/tezacaftor/ivacaftor) transformative."),
    ("bronchiectasis",         "Bronchiectasis",                "J00-J99", "J47",   "Airway clearance + antibiotics."),
    ("silicosis",              "Silicosis",                     "J00-J99", "J62",   "Resurgent in artificial-stone fabricators."),
    ("asbestosis",             "Asbestosis & mesothelioma",     "J00-J99", "J61",   "Late presentation; supportive."),

    # ── Digestive (K00-K95) ─────────────────────────────────────────────────
    ("peptic-ulcer-disease",   "Peptic ulcer disease",          "K00-K95", "K25-K27","H. pylori eradication + PPIs."),
    ("gerd",                   "GERD",                          "K00-K95", "K21",   "PPIs; lifestyle + weight loss."),
    ("ibs",                    "Irritable bowel syndrome",      "K00-K95", "K58",   "Low-FODMAP + antispasmodics."),
    ("ibd-crohns",             "Crohn's disease",               "K00-K95", "K50",   "Biologics (anti-TNF, anti-integrin, anti-IL-23)."),
    ("ibd-ulcerative-colitis", "Ulcerative colitis",            "K00-K95", "K51",   "5-ASA, biologics, JAK inhibitors."),
    ("coeliac-disease",        "Coeliac disease",               "K00-K95", "K90.0", "Gluten-free diet."),
    ("appendicitis",           "Appendicitis",                  "K00-K95", "K35",   "Surgical or selected antibiotic-only."),
    ("cholelithiasis",         "Gallstones / cholecystitis",    "K00-K95", "K80",   "Laparoscopic cholecystectomy."),
    ("pancreatitis",           "Acute & chronic pancreatitis",  "K00-K95", "K85-K86","Gallstones + alcohol most common."),
    ("hepatic-cirrhosis",      "Liver cirrhosis",               "K00-K95", "K74",   "Alcohol, MASLD, viral hepatitis."),
    ("masld",                  "MASLD (formerly NAFLD)",        "K00-K95", "K76.0", "Resmetirom approved 2024 for MASH."),
    ("haemorrhoids",           "Haemorrhoids",                  "K00-K95", "K64",   "Lifestyle, banding, surgery."),
    ("diverticular-disease",   "Diverticular disease",          "K00-K95", "K57",   "Common >60; complicated forms need ABx ± surgery."),
    ("dental-caries",          "Dental caries",                 "K00-K95", "K02",   "Most common chronic disease worldwide."),
    ("periodontal-disease",    "Periodontal disease",           "K00-K95", "K05",   "Linked to systemic inflammation, CVD."),

    # ── Skin (L00-L99) ──────────────────────────────────────────────────────
    ("atopic-dermatitis",      "Atopic dermatitis (eczema)",    "L00-L99", "L20",   "Emollients + topical steroids; dupilumab + JAKi for severe."),
    ("psoriasis",              "Psoriasis",              "L00-L99", "L40",   "Biologics (anti-IL-17, IL-23); methotrexate."),
    ("acne",                   "Acne vulgaris",                 "L00-L99", "L70",   "Retinoids + benzoyl peroxide; isotretinoin for severe."),
    ("rosacea",                "Rosacea",                       "L00-L99", "L71",   "Metronidazole, ivermectin, brimonidine, doxycycline low-dose."),
    ("urticaria",              "Urticaria (hives)",             "L00-L99", "L50",   "H1-antihistamines; omalizumab for chronic."),
    ("vitiligo",               "Vitiligo",                      "L00-L99", "L80",   "Ruxolitinib cream approved 2022."),
    ("alopecia-areata",        "Alopecia areata",               "L00-L99", "L63",   "JAKi (baricitinib, ritlecitinib) approved 2022-23."),
    ("hidradenitis-suppurativa","Hidradenitis suppurativa",     "L00-L99", "L73.2", "Adalimumab, secukinumab; surgical excision."),
    ("contact-dermatitis",     "Contact dermatitis",            "L00-L99", "L23-L25","Allergen avoidance + topical steroids."),
    ("pressure-ulcers",        "Pressure ulcers",               "L00-L99", "L89",   "Prevention via repositioning, surfaces."),
    ("cellulitis",             "Cellulitis",                    "L00-L99", "L03",   "Beta-lactams; MRSA coverage if needed."),

    # ── Musculoskeletal (M00-M99) ───────────────────────────────────────────
    ("osteoarthritis",         "Osteoarthritis",                "M00-M99", "M15-M19","Most-common joint disease; topical NSAIDs + physical therapy."),
    ("rheumatoid-arthritis",   "Rheumatoid arthritis",          "M00-M99", "M05-M06","Methotrexate + biologics + JAKi."),
    ("ankylosing-spondylitis", "Ankylosing spondylitis",        "M00-M99", "M45",   "NSAIDs + anti-TNF / IL-17."),
    ("psoriatic-arthritis",    "Psoriatic arthritis",           "M00-M99", "M07",   "DMARDs + biologics."),
    ("lupus",                  "Systemic lupus erythematosus",  "M00-M99", "M32",   "Hydroxychloroquine + immunosuppression; anifrolumab 2021."),
    ("scleroderma",            "Scleroderma",                   "M00-M99", "M34",   "Tocilizumab for ILD; vasodilators."),
    ("sjogrens",               "Sjögren's syndrome",            "M00-M99", "M35.0", "Tear/saliva replacement + immunomodulation."),
    ("fibromyalgia",           "Fibromyalgia",                  "M00-M99", "M79.7", "SNRIs (duloxetine), pregabalin, exercise."),
    ("low-back-pain",          "Low back pain",                 "M00-M99", "M54.5", "Leading cause of disability globally."),
    ("osteoporosis",           "Osteoporosis",                  "M00-M99", "M81",   "DXA screening; bisphosphonates, denosumab, romosozumab."),
    ("osteomyelitis",          "Osteomyelitis",                 "M00-M99", "M86",   "Long-course IV/oral antibiotics."),
    ("polymyalgia-rheumatica", "Polymyalgia rheumatica",        "M00-M99", "M35.3", "Low-dose corticosteroids."),
    ("vasculitis",             "Vasculitis",                    "M00-M99", "M30-M31","ANCA-vasculitis with rituximab; GCA with tocilizumab."),
    ("sciatica",               "Sciatica",                      "M00-M99", "M54.3", "NSAIDs + PT; surgery if persistent."),
    ("carpal-tunnel",          "Carpal tunnel syndrome",        "M00-M99", "G56.0", "Splinting + steroid injection + surgery."),
    ("rotator-cuff-tear",      "Rotator cuff tear",             "M00-M99", "M75.1", "PT, injections, surgical repair."),

    # ── Genitourinary (N00-N99) ─────────────────────────────────────────────
    ("ckd",                    "Chronic kidney disease",        "N00-N99", "N18",   "ACEi/ARB + SGLT2i + finerenone."),
    ("aki",                    "Acute kidney injury",           "N00-N99", "N17",   "Pre-renal, intrinsic, post-renal causes."),
    ("nephrotic-syndrome",     "Nephrotic syndrome",            "N00-N99", "N04",   "Steroids first-line in MCD."),
    ("uti",                    "Urinary tract infection",       "N00-N99", "N39.0", "Trimethoprim, nitrofurantoin, fosfomycin."),
    ("pyelonephritis",         "Pyelonephritis",                "N00-N99", "N10",   "Ceftriaxone + fluoroquinolones."),
    ("nephrolithiasis",        "Kidney stones",                 "N00-N99", "N20",   "Hydration + medical expulsive therapy + lithotripsy."),
    ("bph",                    "Benign prostatic hyperplasia",  "N00-N99", "N40",   "Alpha-blockers + 5-ARI; UroLift, GreenLight."),
    ("prostatitis",            "Prostatitis",                   "N00-N99", "N41",   "Acute bacterial vs chronic pelvic pain syndrome."),
    ("erectile-dysfunction",   "Erectile dysfunction",          "N00-N99", "N52",   "PDE5i first-line."),
    ("endometriosis",          "Endometriosis",                 "N00-N99", "N80",   "GnRH antagonists (elagolix, relugolix); laparoscopic excision."),
    ("uterine-fibroids",       "Uterine fibroids",              "N00-N99", "D25",   "GnRH antagonists + combined contraception; UAE + myomectomy."),
    ("pcos",                   "PCOS",                          "N00-N99", "E28.2", "Lifestyle + metformin + combined contraception."),
    ("pelvic-inflammatory-disease","Pelvic inflammatory disease","N00-N99","N70",  "Ceftriaxone + doxycycline + metronidazole."),
    ("urinary-incontinence",   "Urinary incontinence",          "N00-N99", "N39.4", "Pelvic floor PT, mirabegron, surgery."),

    # ── Pregnancy (O00-O9A) ────────────────────────────────────────────────
    ("pre-eclampsia",          "Pre-eclampsia / eclampsia",     "O00-O9A", "O14-O15","Aspirin prophylaxis; magnesium sulfate."),
    ("postpartum-haemorrhage", "Postpartum haemorrhage",        "O00-O9A", "O72",   "Oxytocin; tranexamic acid; balloon tamponade."),
    ("preterm-birth",          "Preterm birth",                 "O00-O9A", "O60",   "Antenatal corticosteroids; magnesium for neuroprotection."),
    ("stillbirth",             "Stillbirth",                    "O00-O9A", "O36.4", "Modifiable risk factors: smoking, maternal infection, growth restriction."),
    ("gestational-hypertension","Gestational hypertension",     "O00-O9A", "O13",   "Methyldopa, labetalol, nifedipine."),
    ("hyperemesis-gravidarum", "Hyperemesis gravidarum",        "O00-O9A", "O21",   "Pyridoxine + doxylamine; metoclopramide; ondansetron."),
    ("placenta-praevia",       "Placenta praevia / accreta",    "O00-O9A", "O44",   "Caesarean; embolization; hysterectomy."),

    # ── Perinatal (P00-P96) ─────────────────────────────────────────────────
    ("birth-asphyxia",         "Birth asphyxia",                "P00-P96", "P21",   "Resuscitation; therapeutic hypothermia."),
    ("neonatal-sepsis",        "Neonatal sepsis",               "P00-P96", "P36",   "Empiric ABx; high LMIC burden."),
    ("respiratory-distress-syndrome","Respiratory distress syndrome","P00-P96","P22.0","Surfactant + CPAP."),
    ("low-birth-weight",       "Low birth weight",              "P00-P96", "P07",   "Driver of neonatal mortality."),

    # ── Congenital (Q00-Q99) ────────────────────────────────────────────────
    ("down-syndrome",          "Down syndrome",                 "Q00-Q99", "Q90",   "Trisomy 21; multidisciplinary care."),
    ("congenital-heart-disease","Congenital heart disease",     "Q00-Q99", "Q20-Q28","Surgery + catheter intervention."),
    ("cleft-lip-palate",       "Cleft lip and palate",          "Q00-Q99", "Q35-Q37","Surgical repair + speech therapy."),
    ("neural-tube-defects",    "Neural tube defects",           "Q00-Q99", "Q05",   "Folic acid prevention."),
    ("dmd",                    "Duchenne muscular dystrophy",   "Q00-Q99", "G71.0", "Exon-skipping; corticosteroids; gene therapy approved."),
    ("sma",                    "Spinal muscular atrophy",       "Q00-Q99", "G12.0", "Nusinersen, onasemnogene, risdiplam."),

    # ── Symptoms (R00-R99) ──────────────────────────────────────────────────
    ("chronic-pain",           "Chronic pain",                  "R00-R99", "R52",   "Multimodal analgesia + CBT."),
    ("fever-of-unknown-origin","Fever of unknown origin",       "R00-R99", "R50",   "Diagnostic workup."),
    ("syncope",                "Syncope",                       "R00-R99", "R55",   "Reflex vs cardiac vs orthostatic."),
    ("chronic-cough",          "Chronic cough",                 "R00-R99", "R05.3", "Postnasal drip, GERD, asthma; gefapixant 2024."),

    # ── Injury (S00-T88) ────────────────────────────────────────────────────
    ("traumatic-brain-injury", "Traumatic brain injury",        "S00-T88", "S06",   "Major cause of death + disability."),
    ("burns",                  "Burns",                         "S00-T88", "T20-T31","Fluid resuscitation; early excision + grafting."),
    ("drowning",               "Drowning",                      "S00-T88", "T75.1", "Leading paediatric injury death globally."),
    ("frostbite",              "Frostbite",                     "S00-T88", "T33-T34","Rapid rewarming + iloprost + thrombolysis."),
    ("snakebite",              "Snakebite envenoming",          "S00-T88", "T63.0", "Antivenom; major NTD."),
    ("opioid-overdose",        "Opioid overdose",               "S00-T88", "T40.6", "Naloxone; community distribution."),
    ("food-poisoning",         "Foodborne illness",             "S00-T88", "T62",   "Multiple causative agents."),
    ("heat-stroke",            "Heat stroke",                   "S00-T88", "T67",   "Rapid cooling; rising with climate."),
    ("hypothermia",            "Hypothermia",                   "S00-T88", "T68",   "Active rewarming; ECMO for severe."),

    # ── Stubs that backfill EML diseases without WHO factsheets ─────────────
    ("cardiovascular-diseases-(cvds)", "Cardiovascular diseases (CVDs)", "I00-I99", "I00-I99",
     "Group of disorders of the heart and blood vessels — leading killer worldwide."),
    ("chronic-obstructive-pulmonary-disease-(copd)", "COPD", "J00-J99", "J44",
     "Progressive airflow limitation; smoking + occupational + biomass."),
    ("covid-19",               "COVID-19",                      "A00-B99", "U07.1", "SARS-CoV-2 — endemic since 2023; novel variants tracked."),
    ("haemophilus-influenzae-type-b-(hib)", "Hib (H. influenzae type b)", "A00-B99", "G00.0",
     "Once-major paeds bacterial meningitis pathogen; routine Hib vaccine ~eliminated."),
    ("hpv-and-cervical-cancer", "HPV (human papillomavirus)",   "A00-B99", "B97.7",
     "Most-common STI; high-risk types cause cervical, anal, oropharyngeal cancers."),
    ("human-african-trypanosomiasis-(sleeping-sickness)", "Sleeping sickness", "A00-B99", "B56",
     "Tsetse-fly-borne T. brucei; near-elimination via fexinidazole."),
    ("influenza-(seasonal)",   "Influenza (seasonal)",          "A00-B99", "J11",   "Annual epidemics; vaccine reformulated yearly."),
    ("pertussis",              "Pertussis (whooping cough)",    "A00-B99", "A37",   "Vaccine-preventable — DTaP / Tdap."),
    ("polio-and-other-acute-paralysis", "Poliomyelitis (polio)", "A00-B99", "A80",   "WPV1 in 2 countries; cVDPV in many; PHEIC since 2014."),
    ("rotavirus",              "Rotavirus",                     "A00-B99", "A08.0", "Vaccine-preventable infant gastroenteritis."),
    ("buruli-ulcer-(mycobacterium-ulcerans-infection)", "Buruli ulcer", "A00-B99", "A31.1",
     "Skin and soft-tissue infection; rifampicin + clarithromycin."),
]


def stub_records() -> list[dict]:
    out: list[dict] = []
    for slug, name, chapter, icd10, summary in STUBS:
        cat = CATEGORY_BY_CHAPTER.get(chapter, "Other")
        emoji = next((e for ch, _, e in ICD_CHAPTERS if ch == chapter), "")
        out.append({
            "slug": slug,
            "name": name,
            "category": cat,
            "category_emoji": emoji,
            "icd10_chapter": chapter,
            "icd10": icd10,
            "summary": summary,
            "source": "curated",
        })
    return out


def all_categories() -> list[dict]:
    return [{"chapter": ch, "name": name, "emoji": e} for ch, name, e in ICD_CHAPTERS]


if __name__ == "__main__":
    stubs = stub_records()
    print(f"Curated stubs: {len(stubs)}")
    from collections import Counter
    by_cat = Counter(s["category"] for s in stubs)
    for cat, n in by_cat.most_common():
        print(f"  {cat:30s} {n}")
