"""Disease Atlas — assembles the full per-disease record from all sources.

Pipeline:
  1. Pull WHO Fact Sheet records (rich, parsed HTML).
  2. Pull curated stubs (~237 ICD-10-organized diseases).
  3. Merge by slug — WHO data wins where both exist; stub metadata
     (ICD-10 chapter, category emoji) is added on top.
  4. Layer in WHO EML treatment for diseases with curated drugs.
  5. Resolve each generic drug to brand names via RxNorm.

Output: a flat catalog of ~430 diseases plus a `get_one(slug)` for the
detail view. Catalog is cached in memory after first build (the WHO
factsheets are individually disk-cached, so cold first-build is ~30s,
subsequent calls are instant).
"""

from __future__ import annotations

import logging
from threading import Lock

from ingestion import disease_catalog, rxnorm, who_eml, who_factsheets

log = logging.getLogger(__name__)


_lock = Lock()
_atlas: dict | None = None


def _build() -> dict:
    """Assemble the catalog from all sources."""
    log.info("Building disease atlas …")

    # 1. WHO factsheets (rich data)
    who_records = who_factsheets.fetch_all()
    by_slug: dict[str, dict] = {}
    for r in who_records:
        slug = r.get("slug")
        if not slug:
            continue
        # Promote section text to top-level keys for easier UI access.
        sections = r.get("sections", {})
        record = {
            "slug": slug,
            "name": r.get("name"),
            "source": "WHO",
            "source_url": r.get("source_url"),
            "key_facts": r.get("key_facts", []),
            "overview":     sections.get("overview", {}).get("text"),
            "symptoms":     sections.get("symptoms", {}).get("text"),
            "transmission": sections.get("transmission", {}).get("text"),
            "causes":       sections.get("causes", {}).get("text"),
            "risk_factors": sections.get("risk_factors", {}).get("text"),
            "treatment":    sections.get("treatment", {}).get("text"),
            "prevention":   sections.get("prevention", {}).get("text"),
            "diagnosis":    sections.get("diagnosis", {}).get("text"),
            "burden":       sections.get("burden", {}).get("text"),
            "vaccines":     sections.get("vaccines", {}).get("text"),
            "epidemiology": sections.get("epidemiology", {}).get("text"),
            "extra_sections": r.get("extra_sections", {}),
            "has_who_data": True,
        }
        by_slug[slug] = record

    # 2. Curated stubs — fill in any gaps + add ICD-10 metadata.
    for stub in disease_catalog.stub_records():
        slug = stub["slug"]
        if slug in by_slug:
            # WHO record wins, but pick up ICD-10 + category from stub.
            by_slug[slug]["category"] = stub["category"]
            by_slug[slug]["category_emoji"] = stub["category_emoji"]
            by_slug[slug]["icd10_chapter"] = stub["icd10_chapter"]
            by_slug[slug]["icd10"] = stub["icd10"]
            if not by_slug[slug].get("overview") and stub.get("summary"):
                by_slug[slug]["overview"] = stub["summary"]
        else:
            by_slug[slug] = {
                "slug": slug,
                "name": stub["name"],
                "source": "curated",
                "category": stub["category"],
                "category_emoji": stub["category_emoji"],
                "icd10_chapter": stub["icd10_chapter"],
                "icd10": stub["icd10"],
                "overview": stub["summary"],
                "key_facts": [],
                "has_who_data": False,
            }

    # 2b. WHO records that lack ICD-10 metadata get a default category from
    # name keywords — this keeps the UI grouping coherent.
    for slug, rec in by_slug.items():
        if rec.get("category"):
            continue
        nm = (rec.get("name") or slug).lower()
        guess = _guess_category(nm)
        rec["category"] = guess["name"]
        rec["category_emoji"] = guess["emoji"]
        rec["icd10_chapter"] = guess["chapter"]

    # 3. Layer in EML treatment.
    for slug, drugs in who_eml.EML_BY_DISEASE.items():
        if slug not in by_slug:
            continue
        by_slug[slug]["essential_medicines"] = [
            {"generic": g, "role": r, "notes": n} for g, r, n in drugs
        ]

    # 4. Resolve RxNorm brand names for all drugs in the EML.
    all_generics = sorted(who_eml.all_drugs())
    log.info("Resolving %d generics via RxNorm …", len(all_generics))
    rx = rxnorm.resolve_many(all_generics)
    for slug, rec in by_slug.items():
        meds = rec.get("essential_medicines") or []
        for med in meds:
            r = rx.get(med["generic"]) or {}
            med["brands"] = r.get("brands", [])
            med["scd_count"] = r.get("scd_count", 0)
            med["sbd_count"] = r.get("sbd_count", 0)

    # Final sort: alphabetical by name.
    catalog = sorted(by_slug.values(), key=lambda x: x["name"].lower())
    log.info("Disease atlas: %d diseases", len(catalog))

    return {
        "diseases": catalog,
        "by_slug": by_slug,
        "categories": disease_catalog.all_categories(),
        "rxnorm_resolutions": len(rx),
        "drugs_total": len(all_generics),
    }


def _guess_category(name: str) -> dict:
    """Cheap keyword-based category fallback for WHO factsheets without
    an explicit ICD-10 mapping (mostly broad public-health topics)."""
    keywords = [
        ("cancer",     "C00-D49", "Cancers & neoplasms",   "🧬"),
        ("cardiovas",  "I00-I99", "Cardiovascular",        "❤"),
        ("heart",      "I00-I99", "Cardiovascular",        "❤"),
        ("stroke",     "I00-I99", "Cardiovascular",        "❤"),
        ("respiratory","J00-J99", "Respiratory",           "🫁"),
        ("asthma",     "J00-J99", "Respiratory",           "🫁"),
        ("copd",       "J00-J99", "Respiratory",           "🫁"),
        ("diabetes",   "E00-E89", "Endocrine & metabolic", "🧪"),
        ("obesity",    "E00-E89", "Endocrine & metabolic", "🧪"),
        ("nutrition",  "E00-E89", "Endocrine & metabolic", "🧪"),
        ("anaemia",    "D50-D89", "Blood & immune",        "🩸"),
        ("mental",     "F01-F99", "Mental & behavioural",  "🧠"),
        ("depress",    "F01-F99", "Mental & behavioural",  "🧠"),
        ("alcohol",    "F01-F99", "Mental & behavioural",  "🧠"),
        ("tobacco",    "F01-F99", "Mental & behavioural",  "🧠"),
        ("kidney",     "N00-N99", "Genitourinary",         "🚻"),
        ("renal",      "N00-N99", "Genitourinary",         "🚻"),
        ("bone",       "M00-M99", "Musculoskeletal",       "🦴"),
        ("pregnan",    "O00-O9A", "Pregnancy & childbirth", "🤰"),
        ("maternal",   "O00-O9A", "Pregnancy & childbirth", "🤰"),
        ("birth",      "O00-O9A", "Pregnancy & childbirth", "🤰"),
        ("infant",     "P00-P96", "Perinatal",             "👶"),
        ("neonatal",   "P00-P96", "Perinatal",             "👶"),
        ("violence",   "S00-T88", "Injury & poisoning",    "🚑"),
        ("injury",     "S00-T88", "Injury & poisoning",    "🚑"),
        ("road",       "S00-T88", "Injury & poisoning",    "🚑"),
        ("hepatitis",  "A00-B99", "Infectious & parasitic","🦠"),
        ("influenza",  "A00-B99", "Infectious & parasitic","🦠"),
        ("malaria",    "A00-B99", "Infectious & parasitic","🦠"),
        ("hiv",        "A00-B99", "Infectious & parasitic","🦠"),
        ("tubercu",    "A00-B99", "Infectious & parasitic","🦠"),
        ("ebola",      "A00-B99", "Infectious & parasitic","🦠"),
        ("rabies",     "A00-B99", "Infectious & parasitic","🦠"),
        ("dengue",     "A00-B99", "Infectious & parasitic","🦠"),
        ("cholera",    "A00-B99", "Infectious & parasitic","🦠"),
        ("measles",    "A00-B99", "Infectious & parasitic","🦠"),
        ("vector-borne","A00-B99","Infectious & parasitic","🦠"),
        ("leishman",   "A00-B99", "Infectious & parasitic","🦠"),
    ]
    for kw, ch, cat, emoji in keywords:
        if kw in name:
            return {"chapter": ch, "name": cat, "emoji": emoji}
    return {"chapter": "R00-R99", "name": "Other / general health", "emoji": "🩺"}


def get_atlas(force: bool = False) -> dict:
    global _atlas
    with _lock:
        if _atlas is None or force:
            _atlas = _build()
    return _atlas


def list_diseases() -> list[dict]:
    """Lightweight list for the sidebar — slug, name, category, has_who_data."""
    atlas = get_atlas()
    out: list[dict] = []
    for d in atlas["diseases"]:
        out.append({
            "slug": d["slug"],
            "name": d["name"],
            "category": d.get("category"),
            "category_emoji": d.get("category_emoji"),
            "icd10": d.get("icd10"),
            "has_who_data": d.get("has_who_data", False),
            "has_treatment": bool(d.get("essential_medicines")),
        })
    return out


def get_disease(slug: str) -> dict | None:
    atlas = get_atlas()
    return atlas["by_slug"].get(slug)


def stats() -> dict:
    atlas = get_atlas()
    from collections import Counter
    cats = Counter(d.get("category") for d in atlas["diseases"])
    return {
        "total":          len(atlas["diseases"]),
        "with_who_data":  sum(1 for d in atlas["diseases"] if d.get("has_who_data")),
        "with_treatment": sum(1 for d in atlas["diseases"] if d.get("essential_medicines")),
        "categories":     dict(cats.most_common()),
    }
