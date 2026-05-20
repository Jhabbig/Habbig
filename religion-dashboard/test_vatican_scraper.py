"""Fixture-based test for vatican_scraper.parse_cardinals_html.

Runs without network access. Verifies the parser extracts names,
birth dates, nationalities, consistory data, and roles from a small
synthetic HTML sample that mimics the Vatican Press Office format.

Run with:  python3 test_vatican_scraper.py
"""

from __future__ import annotations

import sys
from datetime import date

from vatican_scraper import (
    _name_to_proper,
    _parse_date,
    _normalize_pope,
    parse_cardinals_html,
    merge_with_curated,
    detect_drift,
)

# Synthetic HTML mimicking the canonical Vatican page format.
SAMPLE_HTML = """
<html><body>
<h1>Cardinals of the Holy Roman Church</h1>

<p><b>Card. PAROLIN, Pietro</b><br>
<i>Secretary of State of the Holy See</i><br>
Born: 17 January 1955 in Schiavon, Italy<br>
Nationality: Italian<br>
Created and proclaimed Cardinal by Pope Francis in the consistory of 22 February 2014<br>
Of the Order of Deacons<br>
</p>

<p><b>Card. ZUPPI, Matteo Maria</b><br>
<i>Archbishop of Bologna</i><br>
Born: 11 October 1955 in Roma, Italy<br>
Nationality: Italian<br>
Created and proclaimed Cardinal by Pope Francis in the consistory of 5 October 2019<br>
</p>

<p><b>Card. ERDŐ, Péter</b><br>
<i>Archbishop of Esztergom-Budapest</i><br>
Born: 25 June 1952 in Budapest, Hungary<br>
Nationality: Hungarian<br>
Created and proclaimed Cardinal by Pope John Paul II in the consistory of 21 October 2003<br>
</p>

<p><b>Card. SARAH, Robert</b><br>
<i>Prefect emeritus of the Congregation for Divine Worship</i><br>
Born: 15 June 1945 in Ourous, Guinea<br>
Nationality: Guinean<br>
Created and proclaimed Cardinal by Pope Benedict XVI in the consistory of 20 November 2010<br>
</p>
</body></html>
"""

REF = date(2026, 5, 20)


def t(name: str, ok: bool, details: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {details}" if details else ""))
    if not ok:
        sys.exit(1)


def main() -> None:
    print("\n— Helpers —")
    t("parse_date english", _parse_date("17 January 1955") == "1955-01-17",
      f"got {_parse_date('17 January 1955')}")
    t("parse_date US",      _parse_date("January 17, 1955") == "1955-01-17",
      f"got {_parse_date('January 17, 1955')}")
    t("parse_date Italian", _parse_date("17 gennaio 1955") == "1955-01-17",
      f"got {_parse_date('17 gennaio 1955')}")
    t("parse_date bad",     _parse_date("not a date") is None)

    t("name caps→proper", _name_to_proper("PAROLIN, Pietro") == "Pietro Parolin",
      f"got '{_name_to_proper('PAROLIN, Pietro')}'")
    t("name particles",   _name_to_proper("DE KESEL, Jozef") == "Jozef de Kesel",
      f"got '{_name_to_proper('DE KESEL, Jozef')}'")

    t("pope normalise Francis",  _normalize_pope("Pope Francis") == "Francis")
    t("pope normalise Benedict", _normalize_pope("Benedetto XVI") == "Benedict XVI")
    t("pope normalise JPII",     _normalize_pope("John Paul II") == "John Paul II")

    print("\n— Parser on synthetic page —")
    out = parse_cardinals_html(SAMPLE_HTML, ref=REF)
    t("count == 4", len(out) == 4, f"got {len(out)}")

    by_name = {c["name"]: c for c in out}
    t("Parolin extracted", "Pietro Parolin" in by_name)
    p = by_name["Pietro Parolin"]
    t("Parolin born_iso",  p["born_iso"] == "1955-01-17", f"got {p['born_iso']}")
    t("Parolin age 2026",  p["age"] == 71,                f"got {p['age']}")
    t("Parolin appointer", p["appointer"] == "Francis")
    t("Parolin consistory",p["consistory_date_iso"] == "2014-02-22", f"got {p['consistory_date_iso']}")
    t("Parolin elector",   p["elector"] is True)

    t("Erdő appointer",    by_name["Péter Erdő"]["appointer"] == "John Paul II")
    t("Sarah appointer",   by_name["Robert Sarah"]["appointer"] == "Benedict XVI")
    t("Sarah age 80 → non-elector", by_name["Robert Sarah"]["age"] == 80)
    t("Sarah elector flag",         by_name["Robert Sarah"]["elector"] is False)

    print("\n— Merge with curated —")
    curated = [
        {"name": "Pietro Parolin", "wing": "moderate", "papabile_tier": 3, "summary": "test"},
        {"name": "Matteo Maria Zuppi", "wing": "progressive", "papabile_tier": 3, "summary": "test"},
    ]
    merged = merge_with_curated(out, curated)
    p_merged = next(c for c in merged if c["name"] == "Pietro Parolin")
    t("merge attaches wing",        p_merged["wing"] == "moderate")
    t("merge attaches tier",        p_merged["papabile_tier"] == 3)
    t("merge marks matched",        p_merged["matched_curated"] is True)
    erdo_merged = next(c for c in merged if c["name"] == "Péter Erdő")
    t("unmatched defaults to T0",   erdo_merged["papabile_tier"] == 0)
    t("unmatched flagged",          erdo_merged["matched_curated"] is False)

    print("\n— Drift detection —")
    drift = detect_drift(out, curated)
    t("drift counts scraped", drift["scraped_count"] == 4)
    t("drift counts curated", drift["curated_count"] == 2)
    t("drift surfaces new",   "erdő" in drift["added_since_curated"] or "erd" in " ".join(drift["added_since_curated"]))

    print("\nAll parser tests pass.")


if __name__ == "__main__":
    main()
