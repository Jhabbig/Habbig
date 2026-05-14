#!/usr/bin/env python3
"""
seed_13f.py — placeholder seeder for SEC EDGAR data.

NOT YET WIRED. This script exists to document the ingestion pattern so
the EDGAR fetcher can be implemented later without re-designing it.

EDGAR has three relevant endpoints. All require a descriptive User-Agent
header per https://www.sec.gov/os/accessing-edgar-data — without it,
requests are 403'd. EDGAR rate-limits at ~10 req/s globally, so the
real implementation must sleep between calls and back off on 429.

  1) Filer submissions index:
       https://data.sec.gov/submissions/CIK{cik10}.json
     Returns a JSON blob with `filings.recent.{form,accessionNumber,filingDate,
     reportDate,primaryDocument}`. Filter `form in ('13F-HR','SC 13D','SC 13D/A',
     'SC 13G','SC 13G/A','4')`.

  2) 13F holdings (per-accession):
       https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_no_dashes}/
       primary_doc.xml                  (cover page metadata)
       {accession_no_no_dashes}-index.json (lists secondary docs)
       The actual holdings table is an "informationtable.xml" sibling document;
     parse it with `xml.etree.ElementTree` — schema is at
     https://www.sec.gov/info/edgar/specifications/form13f.htm.

  3) Form 4 individual transactions:
       Same Archives path; primary doc is typically "doc4.xml".
     Transaction codes of interest: 'P' (open-market purchase), 'S' (sale),
     'A' (grant), 'F' (tax-withholding). For the "is_buy" heuristic the
     server stores: txn_code in ('P','A') AND shares > 0.

Activist heuristic for 13D:
  - form_type == 'SC 13D' (not a 13G or amendment) AND filer.kind == 'activist'
  - OR the 'item 4' text contains keywords like "engage management",
    "board representation", "strategic alternatives", "spin-off".

Wiring sketch (TODO):
  for whale in db_whales(is_active=1):
      subs = http_get(f"https://data.sec.gov/submissions/CIK{whale.cik}.json")
      for filing in subs["filings"]["recent"]:
          if filing["form"] in WANTED_FORMS:
              persist_filing(whale, filing)
              if filing["form"].startswith("13F"):
                  positions = fetch_13f_positions(whale.cik, filing.accession_no)
                  persist_positions(filing.accession_no, positions)

Run manually once SEC keys / UA are configured:
    UA="whale-watch (ops@narve.ai)" python3 scripts/seed_13f.py
"""

from __future__ import annotations

import os
import sys


EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"
WANTED_FORMS = {"13F-HR", "13F-HR/A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "4", "4/A"}


def main() -> int:
    ua = os.environ.get("UA", "").strip()
    if not ua:
        print(
            "Set UA env var to a descriptive User-Agent before talking to "
            "EDGAR (e.g. UA='whale-watch (ops@narve.ai)').",
            file=sys.stderr,
        )
        return 2
    print(
        "seed_13f.py is a placeholder — implement EDGAR ingestion here. "
        "See docstring for the endpoint pattern.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
