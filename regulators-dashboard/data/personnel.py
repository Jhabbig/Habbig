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
    {
        "regulator":  "Fed",
        "role":       "Chair",
        "name":       "Jerome Powell",
        "term_end":   "2026-05-23",
        "term_type":  "Chair term end",
        "source_url": "https://www.federalreserve.gov/aboutthefed/bios/board/powell.htm",
        "notes":      "Second four-year term as Chair ends May 2026. Governor term runs through 2028; renomination-as-Chair is the market-relevant event.",
    },
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
        "regulator":  "FCA",
        "role":       "Chief Executive",
        "name":       "Nikhil Rathi",
        "term_end":   "2030-09-30",
        "term_type":  "Second term end",
        "source_url": "https://www.fca.org.uk/about/who-we-are/governance/our-board",
        "notes":      "Reappointed for a second five-year term in 2025.",
    },
    {
        "regulator":  "ESMA",
        "role":       "Chair",
        "name":       "Verena Ross",
        "term_end":   "2026-10-31",
        "term_type":  "First term end",
        "source_url": "https://www.esma.europa.eu/about-esma/governance/board-supervisors",
        "notes":      "Appointed November 2021 for a single five-year term. Re-appointment decision and successor-selection news are the market-relevant events.",
    },
]
