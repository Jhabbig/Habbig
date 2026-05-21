"""Hand-curated personnel watch — v0.6 seed roster.

==============================================================================
  EVERY ENTRY MUST CARRY A `source_url` POINTING TO THE OFFICIAL ROSTER PAGE
  THAT CONFIRMS THE DATE. Re-verify annually — these dates drift when terms
  are renewed early, commissioners resign, or chairs are re-nominated.
  Auto-scraping the confirmation calendars is roadmap item v1.1.
==============================================================================

Date semantics in `term_end`:

  - For "Chair" / "Chairman" / "CEO" roles where the position has no
    statutory fixed end (Fed Chair, SEC Chair), `term_end` is the
    **next likely transition anchor** — usually the incumbent's
    underlying commissioner-term end, or a known re-nomination
    decision point. The `term_type` field describes which.

  - For fixed-term roles (commissioner, governor, board member),
    `term_end` is the statutory end of that fixed term.

This is best-effort. The dashboard's role is to surface upcoming
transition anchors so a reader knows when to start watching — not to
be a system of record. Verify against `source_url` before relying.

To extend: append a dict to `PEOPLE` with all six fields populated.
"""

from __future__ import annotations

PEOPLE: list[dict] = [
    # ── US: Federal Reserve ────────────────────────────────────────────────
    {
        "regulator":  "Fed",
        "role":       "Chair",
        "name":       "Jerome Powell",
        "term_end":   "2026-05-23",
        "term_type":  "Chair term end",
        "source_url": "https://www.federalreserve.gov/aboutthefed/bios/board/powell.htm",
        "notes":      "Second four-year term as Chair ends May 2026. Governor term runs through 2028; renomination-as-Chair is the market-relevant event.",
    },

    # ── US: SEC ────────────────────────────────────────────────────────────
    {
        "regulator":  "SEC",
        "role":       "Chair",
        "name":       "Paul Atkins",
        "term_end":   "2030-06-05",
        "term_type":  "Commissioner term end",
        "source_url": "https://www.sec.gov/biography/sec-chairman-paul-s-atkins",
        "notes":      "Confirmed by the Senate on April 9, 2025. Commissioner-term end is the next statutory transition anchor; the Chair role itself has no fixed term.",
    },
    {
        "regulator":  "SEC",
        "role":       "Commissioner",
        "name":       "Mark Uyeda",
        "term_end":   "2028-06-05",
        "term_type":  "Commissioner term end",
        "source_url": "https://www.sec.gov/about/commissioner-mark-t-uyeda",
        "notes":      "Served as Acting Chair Jan–Apr 2025. Verify exact term-end against the source page annually.",
    },
    {
        "regulator":  "SEC",
        "role":       "Commissioner",
        "name":       "Hester Peirce",
        "term_end":   "2025-06-05",
        "term_type":  "Commissioner term end",
        "source_url": "https://www.sec.gov/about/commissioner-hester-m-peirce",
        "notes":      "Statutory term ended June 2025; under SEC rules commissioners may serve up to 18 months past expiry without renomination.",
    },
    {
        "regulator":  "SEC",
        "role":       "Commissioner",
        "name":       "Caroline Crenshaw",
        "term_end":   "2024-06-05",
        "term_type":  "Commissioner term end",
        "source_url": "https://www.sec.gov/about/commissioner-caroline-a-crenshaw",
        "notes":      "Re-nomination contested in 2024; verify current standing against the source page.",
    },

    # ── US: CFTC ───────────────────────────────────────────────────────────
    {
        "regulator":  "CFTC",
        "role":       "Acting Chair",
        "name":       "Caroline Pham",
        "term_end":   "2027-04-13",
        "term_type":  "Commissioner term end",
        "source_url": "https://www.cftc.gov/About/Commissioners/CarolinePham",
        "notes":      "Designated Acting Chair Jan 2025 pending the new Chair's Senate confirmation.",
    },

    # ── US: CFPB ───────────────────────────────────────────────────────────
    {
        "regulator":  "CFPB",
        "role":       "Director",
        "name":       "(vacant / acting)",
        "term_end":   "",
        "term_type":  "Director seat",
        "source_url": "https://www.consumerfinance.gov/about-us/the-bureau/about-director/",
        "notes":      "Director status changes frequently — verify against the source page before relying.",
    },

    # ── UK: FCA ────────────────────────────────────────────────────────────
    {
        "regulator":  "FCA",
        "role":       "Chief Executive",
        "name":       "Nikhil Rathi",
        "term_end":   "2030-09-30",
        "term_type":  "Second term end",
        "source_url": "https://www.fca.org.uk/about/who-we-are/governance/our-board",
        "notes":      "Reappointed for a second five-year term in 2025.",
    },

    # ── UK: BoE ────────────────────────────────────────────────────────────
    {
        "regulator":  "BoE",
        "role":       "Governor",
        "name":       "Andrew Bailey",
        "term_end":   "2028-03-15",
        "term_type":  "Eight-year term end",
        "source_url": "https://www.bankofengland.co.uk/about/people/andrew-bailey",
        "notes":      "Appointed March 2020 for an eight-year non-renewable term.",
    },

    # ── EU: ESMA ───────────────────────────────────────────────────────────
    {
        "regulator":  "ESMA",
        "role":       "Chair",
        "name":       "Verena Ross",
        "term_end":   "2026-10-31",
        "term_type":  "First term end",
        "source_url": "https://www.esma.europa.eu/about-esma/governance/board-supervisors",
        "notes":      "Appointed November 2021 for a single five-year term. Re-appointment decision and successor-selection news are the market-relevant events.",
    },

    # ── EU: ECB ────────────────────────────────────────────────────────────
    {
        "regulator":  "ECB",
        "role":       "President",
        "name":       "Christine Lagarde",
        "term_end":   "2027-10-31",
        "term_type":  "Eight-year term end",
        "source_url": "https://www.ecb.europa.eu/ecb/orga/decisions/html/cvlagarde.en.html",
        "notes":      "Appointed November 2019 for a non-renewable eight-year term.",
    },

    # ── DE: BaFin ──────────────────────────────────────────────────────────
    {
        "regulator":  "BaFin",
        "role":       "President",
        "name":       "Mark Branson",
        "term_end":   "2026-08-01",
        "term_type":  "First term end",
        "source_url": "https://www.bafin.de/EN/DieBaFin/Praesidium/praesidium_node_en.html",
        "notes":      "Appointed August 2021 for a five-year term; re-appointment decision is the market-relevant event.",
    },

    # ── CH: FINMA ──────────────────────────────────────────────────────────
    {
        "regulator":  "FINMA",
        "role":       "Director",
        "name":       "Stefan Walter",
        "term_end":   "",
        "term_type":  "Director seat",
        "source_url": "https://www.finma.ch/en/finma/finma-an-overview/organisation/the-board-of-directors-and-executive-board/",
        "notes":      "Took office April 2024; verify current standing against the source page.",
    },

    # ── SG: MAS ────────────────────────────────────────────────────────────
    {
        "regulator":  "MAS",
        "role":       "Managing Director",
        "name":       "Chia Der Jiun",
        "term_end":   "",
        "term_type":  "Indefinite (govt appointment)",
        "source_url": "https://www.mas.gov.sg/who-we-are/our-management/managing-director",
        "notes":      "Appointed January 2024.",
    },

    # ── HK: HKMA ───────────────────────────────────────────────────────────
    {
        "regulator":  "HKMA",
        "role":       "Chief Executive",
        "name":       "Eddie Yue",
        "term_end":   "2026-09-30",
        "term_type":  "Term end",
        "source_url": "https://www.hkma.gov.hk/eng/about-us/our-people/eddie-yue/",
        "notes":      "Appointed October 2019 for a non-renewable seven-year term.",
    },

    # ── AU: ASIC ───────────────────────────────────────────────────────────
    {
        "regulator":  "ASIC",
        "role":       "Chair",
        "name":       "Joseph Longo",
        "term_end":   "2026-06-01",
        "term_type":  "Five-year term end",
        "source_url": "https://asic.gov.au/about-asic/asic-investigations-and-enforcement/asic-commissioners/joe-longo/",
        "notes":      "Appointed June 2021 for a five-year term.",
    },

    # ── IN: SEBI ───────────────────────────────────────────────────────────
    {
        "regulator":  "SEBI",
        "role":       "Chair",
        "name":       "Tuhin Kanta Pandey",
        "term_end":   "2028-03-01",
        "term_type":  "Three-year term end",
        "source_url": "https://www.sebi.gov.in/sebi_data/chairman.html",
        "notes":      "Appointed March 2025 for a three-year term.",
    },

    # ── JP: BoJ ────────────────────────────────────────────────────────────
    {
        "regulator":  "BoJ",
        "role":       "Governor",
        "name":       "Kazuo Ueda",
        "term_end":   "2028-04-08",
        "term_type":  "Five-year term end",
        "source_url": "https://www.boj.or.jp/en/about/organization/policyboard/gv_ueda.htm",
        "notes":      "Appointed April 2023.",
    },
]
