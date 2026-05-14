#!/usr/bin/env python3
"""
Pew Research Center loader.

Pew does not publish a clean public API. Their reports ship as PDFs and
XLSX/CSV downloads attached to individual study pages. The robust path
for an MVP is therefore a curated YAML, refreshed semi-annually by hand.

Two input modes - the loader uses whichever is present:

  1. **Curated YAML** (default): `data/sources/pew_curated.yaml`
     Hand-maintained list of headline findings from recent Pew global
     studies. Schema:

         schema_version: 1
         last_curated: "2026-04-01"
         next_refresh_due: "2026-10-01"
         findings:
           - iso: USA
             issue: "Inflation / cost of living"
             pct: 41
             year: 2026
             study: "Spring 2026 Global Attitudes"
             source_url: "https://www.pewresearch.org/..."

  2. **Drop directory**: `data/sources/pew_drops/*.csv`
     If anything is in this directory, the loader picks it up additively.
     Columns: iso, issue, pct, year, source_url

Manual refresh checklist (semi-annual, June + December):
    a. Visit https://www.pewresearch.org/global/ for the latest Global
       Attitudes Survey release.
    b. Pull topline percentages for the priority countries.
    c. Edit `pew_curated.yaml` - bump `last_curated` and `next_refresh_due`.
    d. Re-run this script. The committed YAML snapshot
       (`data/snapshot_pew.yaml`) becomes the new last-known-good.

Cadence: semi-annual (manual). Safe to wire into the monthly cron
alongside V-Dem + World Bank.

Usage:
    python3 data/sources/pew_loader.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import write_overlay  # noqa: E402

HERE = Path(__file__).resolve().parent
CURATED_YAML = HERE / "pew_curated.yaml"
DROP_DIR = HERE / "pew_drops"


def _add(by_iso, iso, issue, pct, year, source_url, study=None, origin=None):
    if not (iso and issue and 0 <= pct <= 100):
        return
    entry = {
        "issue": issue,
        "pct": pct,
        "year": year,
        "source_url": source_url,
    }
    if study:
        entry["study"] = study
    if origin:
        entry["_origin"] = origin
    by_iso.setdefault(iso, []).append(entry)


def load_curated(path):
    """Load `pew_curated.yaml`. Returns (by_iso, meta_dict)."""
    by_iso = {}
    meta = {}
    if not path.exists():
        return by_iso, meta
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print("pew_loader: curated YAML unreadable: " + str(e), file=sys.stderr)
        return by_iso, meta
    meta = {
        "schema_version": doc.get("schema_version"),
        "last_curated": doc.get("last_curated"),
        "next_refresh_due": doc.get("next_refresh_due"),
    }
    for row in doc.get("findings", []) or []:
        try:
            pct = float(row.get("pct") or 0)
        except (TypeError, ValueError):
            continue
        _add(
            by_iso,
            iso=(row.get("iso") or "").strip().upper(),
            issue=(row.get("issue") or "").strip(),
            pct=pct,
            year=str(row.get("year") or "").strip(),
            source_url=(row.get("source_url") or "").strip(),
            study=(row.get("study") or "").strip() or None,
            origin="curated",
        )
    return by_iso, meta


def load_drops(drop_dir):
    """Append-only ingestion of any drop CSVs."""
    by_iso = {}
    if not drop_dir.exists():
        return by_iso
    for csv_path in sorted(drop_dir.glob("*.csv")):
        with csv_path.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    pct = float(row.get("pct") or 0)
                except (TypeError, ValueError):
                    continue
                _add(
                    by_iso,
                    iso=(row.get("iso") or "").strip().upper(),
                    issue=(row.get("issue") or "").strip(),
                    pct=pct,
                    year=(row.get("year") or "").strip(),
                    source_url=(row.get("source_url") or "").strip(),
                    origin="drop:" + csv_path.name,
                )
    return by_iso


def _merge(a, b):
    """Union by (iso, issue, year) - drop entries override curated entries
    so an ad-hoc CSV can correct a stale curated value.
    """
    out = {iso: list(items) for iso, items in a.items()}
    for iso, items in b.items():
        existing = {(e["issue"], e["year"]): i for i, e in enumerate(out.get(iso, []))}
        for entry in items:
            key = (entry["issue"], entry["year"])
            if key in existing:
                out[iso][existing[key]] = entry
            else:
                out.setdefault(iso, []).append(entry)
    return out


def main():
    curated, meta = load_curated(CURATED_YAML)
    drops = load_drops(DROP_DIR)
    by_iso = _merge(curated, drops)

    payload = {
        "source": "pew",
        "source_url": "https://www.pewresearch.org/global/",
        "cadence": "semi-annual (manual curation)",
        "by_iso": by_iso,
    }
    payload.update({k: v for k, v in meta.items() if v is not None})

    path = write_overlay("pew", payload)
    n = sum(len(v) for v in by_iso.values())
    print("pew_loader: wrote " + str(path) + " (" + str(n) + " findings across " + str(len(by_iso)) + " countries)")
    if not by_iso:
        print("pew_loader: warning - no findings; "
              "edit pew_curated.yaml or drop a CSV in pew_drops/",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
