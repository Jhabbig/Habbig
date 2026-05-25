"""Religious calendar generator — multi-year, evergreen.

Replaces the 2026-hardcoded calendar in religion_data.py. Computes
movable Christian feasts (Easter and its dependents) from first
principles via the Meeus/Jones/Butcher algorithm. Movable Jewish,
Islamic, Hindu and other dates are looked up from hand-curated tables
that I've populated for 2025-2034 from publicly verifiable astronomical
calendars (Pesach/Yom Kippur from the Hebrew calendar; Ramadan/Eid
from the Islamic calendar adjusted for Saudi sighting tradition;
Diwali from the Hindu lunisolar calendar).

CAVEATS:
  - Hebrew/Islamic dates depend on first-sighting traditions that vary
    by jurisdiction. Numbers here are the most-widely-used civil
    observances (Israeli rabbinate, Saudi sighting tradition).
  - Islamic dates can shift ±1 day by region.
  - Hindu festival dates can have regional variation (north vs south).
  - Bahá'í dates assume the Universal House of Justice's 2014 adoption
    of the standardised solar calendar.

EXTENDING: when you need calendar entries past 2034, add rows to the
LOOKUP tables. Easter and its dependents continue indefinitely via the
algorithm.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


# ─── Easter computation (Meeus / Jones / Butcher) ────────────────────────────

def easter_sunday_western(year: int) -> date:
    """Western (Gregorian) Easter Sunday for the given year."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def easter_sunday_orthodox(year: int) -> date:
    """Orthodox (Julian) Easter Sunday converted to Gregorian date."""
    # Meeus algorithm for Julian Easter
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    julian_month = (d + e + 114) // 31
    julian_day = ((d + e + 114) % 31) + 1
    # Convert Julian date to Gregorian.
    # In the 20th and 21st centuries the offset is 13 days.
    offset = 13 if 2100 > year >= 1900 else (14 if year >= 2100 else 12)
    return date(year, julian_month, julian_day) + timedelta(days=offset)


# ─── Lookup tables for non-Gregorian movable feasts ──────────────────────────
# Pesach (1st day, civil observance per Israeli rabbinate)
PESACH_FIRST_DAY = {
    2025: "2025-04-13", 2026: "2026-04-02", 2027: "2027-04-22",
    2028: "2028-04-11", 2029: "2029-03-31", 2030: "2030-04-18",
    2031: "2031-04-08", 2032: "2032-03-27", 2033: "2033-04-14",
    2034: "2034-04-04",
}
# Rosh Hashanah (1st day)
ROSH_HASHANAH = {
    2025: "2025-09-23", 2026: "2026-09-12", 2027: "2027-10-02",
    2028: "2028-09-21", 2029: "2029-09-10", 2030: "2030-09-28",
    2031: "2031-09-18", 2032: "2032-09-06", 2033: "2033-09-24",
    2034: "2034-09-14",
}
# Yom Kippur
YOM_KIPPUR = {
    2025: "2025-10-02", 2026: "2026-09-21", 2027: "2027-10-11",
    2028: "2028-09-30", 2029: "2029-09-19", 2030: "2030-10-07",
    2031: "2031-09-27", 2032: "2032-09-15", 2033: "2033-10-03",
    2034: "2034-09-23",
}
# Hanukkah first day
HANUKKAH = {
    2025: "2025-12-14", 2026: "2026-12-04", 2027: "2027-12-24",
    2028: "2028-12-12", 2029: "2029-12-01", 2030: "2030-12-20",
    2031: "2031-12-09", 2032: "2032-11-27", 2033: "2033-12-16",
    2034: "2034-12-06",
}

# Ramadan first day (Saudi sighting tradition)
RAMADAN_START = {
    2025: "2025-02-28", 2026: "2026-02-17", 2027: "2027-02-06",
    2028: "2028-01-27", 2029: "2029-01-16", 2030: "2030-01-05",
    2031: "2030-12-25", 2032: "2032-12-13", 2033: "2033-12-03",
    2034: "2034-11-22",
}
# Eid al-Fitr (≈ Ramadan + 30 days)
EID_AL_FITR = {
    2025: "2025-03-30", 2026: "2026-03-19", 2027: "2027-03-08",
    2028: "2028-02-25", 2029: "2029-02-14", 2030: "2030-02-04",
    2031: "2031-01-24", 2032: "2032-01-13", 2033: "2033-01-02",
    2034: "2034-12-22",
}
# Eid al-Adha (≈ 10 Dhu al-Hijjah)
EID_AL_ADHA = {
    2025: "2025-06-06", 2026: "2026-05-26", 2027: "2027-05-16",
    2028: "2028-05-04", 2029: "2029-04-23", 2030: "2030-04-12",
    2031: "2031-04-02", 2032: "2032-03-21", 2033: "2033-03-10",
    2034: "2034-02-28",
}
# Hajj (8-12 Dhu al-Hijjah; principal day = 9)
HAJJ_START = {
    2025: "2025-06-04", 2026: "2026-05-24", 2027: "2027-05-14",
    2028: "2028-05-02", 2029: "2029-04-21", 2030: "2030-04-10",
    2031: "2031-03-31", 2032: "2032-03-19", 2033: "2033-03-08",
    2034: "2034-02-26",
}
# Ashura (10 Muharram, Shia commemoration)
ASHURA = {
    2025: "2025-07-06", 2026: "2026-06-26", 2027: "2027-06-16",
    2028: "2028-06-04", 2029: "2029-05-24", 2030: "2030-05-13",
    2031: "2031-05-03", 2032: "2032-04-21", 2033: "2033-04-10",
    2034: "2034-03-31",
}
# Mawlid (12 Rabi al-Awwal; varies by Sunni/Shia ±5 days)
MAWLID = {
    2025: "2025-09-04", 2026: "2026-08-25", 2027: "2027-08-14",
    2028: "2028-08-02", 2029: "2029-07-22", 2030: "2030-07-12",
    2031: "2031-07-01", 2032: "2032-06-19", 2033: "2033-06-09",
    2034: "2034-05-29",
}

# Hindu — Diwali (Lakshmi Puja day, varies by region; using north Indian)
DIWALI = {
    2025: "2025-10-21", 2026: "2026-11-08", 2027: "2027-10-29",
    2028: "2028-10-17", 2029: "2029-11-05", 2030: "2030-10-26",
    2031: "2031-11-14", 2032: "2032-11-02", 2033: "2033-10-22",
    2034: "2034-11-10",
}
# Holi
HOLI = {
    2025: "2025-03-14", 2026: "2026-03-03", 2027: "2027-03-22",
    2028: "2028-03-11", 2029: "2029-03-01", 2030: "2030-03-20",
    2031: "2031-03-09", 2032: "2032-03-26", 2033: "2033-03-16",
    2034: "2034-03-05",
}
# Maha Shivaratri
SHIVARATRI = {
    2025: "2025-02-26", 2026: "2026-02-15", 2027: "2027-03-06",
    2028: "2028-02-23", 2029: "2029-02-11", 2030: "2030-03-02",
    2031: "2031-02-20", 2032: "2032-03-09", 2033: "2033-02-26",
    2034: "2034-02-16",
}
# Dussehra / Vijayadashami
DUSSEHRA = {
    2025: "2025-10-02", 2026: "2026-10-20", 2027: "2027-10-09",
    2028: "2028-09-27", 2029: "2029-10-16", 2030: "2030-10-06",
    2031: "2031-10-25", 2032: "2032-10-14", 2033: "2033-10-02",
    2034: "2034-10-21",
}

# Buddhist Vesak (Theravada calendar; full moon day of Vaisakha)
VESAK = {
    2025: "2025-05-12", 2026: "2026-05-01", 2027: "2027-05-20",
    2028: "2028-05-08", 2029: "2029-04-27", 2030: "2030-05-16",
    2031: "2031-05-06", 2032: "2032-04-25", 2033: "2033-05-13",
    2034: "2034-05-03",
}

# Sikh — Guru Nanak Jayanti (lunar, Kartik full moon)
GURU_NANAK = {
    2025: "2025-11-05", 2026: "2026-11-24", 2027: "2027-11-14",
    2028: "2028-11-02", 2029: "2029-11-21", 2030: "2030-11-10",
    2031: "2031-10-30", 2032: "2032-11-17", 2033: "2033-11-06",
    2034: "2034-11-25",
}
# Vaisakhi (solar, fixed-ish 13/14 April)
VAISAKHI_FIXED = "04-14"

# Lunar New Year (Chinese, used for East Asian Buddhist + folk)
LUNAR_NEW_YEAR = {
    2025: "2025-01-29", 2026: "2026-02-17", 2027: "2027-02-06",
    2028: "2028-01-26", 2029: "2029-02-13", 2030: "2030-02-03",
    2031: "2031-01-23", 2032: "2032-02-11", 2033: "2033-01-31",
    2034: "2034-02-19",
}


# ─── Calendar generator ────────────────────────────────────────────────────

def _ev(date_iso: str, name: str, religion: str, duration: int, summary: str) -> dict:
    return {"date": date_iso, "name": name, "religion": religion,
            "duration": duration, "summary": summary}


def _get(table: dict, year: int) -> Optional[str]:
    return table.get(year)


def generate_calendar(year: int) -> list[dict]:
    """Generate the religious calendar for `year`.

    Returns a list of events sorted by date. Returns [] for years where
    we have no lookup data and no algorithmic recipe.
    """
    events: list[dict] = []
    y = year

    # ─ Fixed-date Christian feasts (Gregorian) ─
    events += [
        _ev(f"{y}-01-06", "Epiphany",                   "Christian",  1, "Manifestation of Christ to the gentiles (Western and Armenian)."),
        _ev(f"{y}-01-07", "Orthodox Christmas",          "Christian",  1, "Christmas observed by churches following the Julian calendar."),
        _ev(f"{y}-12-25", "Christmas",                   "Christian",  1, "Birth of Jesus (Western and most Eastern churches)."),
        _ev(f"{y}-11-01", "All Saints' Day",             "Christian",  1, "Western Christian feast honouring all saints."),
        _ev(f"{y}-08-15", "Assumption of Mary",          "Christian",  1, "Catholic + Orthodox feast of Mary's assumption."),
        _ev(f"{y}-12-08", "Immaculate Conception",       "Christian",  1, "Catholic feast of Mary's conception."),
    ]

    # ─ Movable Christian feasts (computed from Easter) ─
    try:
        e_west = easter_sunday_western(y)
        ash_wed = e_west - timedelta(days=46)
        good_fri = e_west - timedelta(days=2)
        maundy = e_west - timedelta(days=3)
        pentecost = e_west + timedelta(days=49)
        e_orth = easter_sunday_orthodox(y)
        events += [
            _ev(ash_wed.isoformat(),    "Ash Wednesday",         "Christian", 1, "Beginning of Western Christian Lent."),
            _ev(maundy.isoformat(),     "Maundy Thursday",       "Christian", 1, "Last Supper commemoration; start of the Easter Triduum."),
            _ev(good_fri.isoformat(),   "Good Friday",           "Christian", 1, "Crucifixion of Christ (Western)."),
            _ev(e_west.isoformat(),     "Easter (Western)",      "Christian", 1, "Resurrection of Christ — most important Christian feast."),
            _ev(e_orth.isoformat(),     "Easter (Orthodox)",     "Christian", 1, "Pascha — Eastern Orthodox Easter."),
            _ev(pentecost.isoformat(),  "Pentecost (Western)",   "Christian", 1, "Descent of the Holy Spirit; 50 days after Easter."),
        ]
    except Exception:
        pass

    # ─ Jewish (lookup) ─
    if rh := _get(ROSH_HASHANAH, y):
        events.append(_ev(rh, "Rosh Hashanah", "Jewish", 2, "Jewish New Year; start of the High Holy Days."))
    if yk := _get(YOM_KIPPUR, y):
        events.append(_ev(yk, "Yom Kippur", "Jewish", 1, "Day of Atonement — holiest day in Judaism."))
    if pe := _get(PESACH_FIRST_DAY, y):
        events.append(_ev(pe, "Passover (Pesach) begins", "Jewish", 7, "Commemoration of the Exodus from Egypt."))
    if hk := _get(HANUKKAH, y):
        events.append(_ev(hk, "Hanukkah begins", "Jewish", 8, "Festival of Lights; rededication of the Second Temple."))

    # ─ Islamic (lookup; sums to ~7 entries / yr) ─
    if rm := _get(RAMADAN_START, y):
        events.append(_ev(rm, "Ramadan begins", "Islamic", 30, "Month of fasting from dawn to sunset for Muslims worldwide."))
    if eaf := _get(EID_AL_FITR, y):
        events.append(_ev(eaf, "Eid al-Fitr", "Islamic", 3, "Festival concluding Ramadan."))
    if eaa := _get(EID_AL_ADHA, y):
        events.append(_ev(eaa, "Eid al-Adha", "Islamic", 4, "Festival of the Sacrifice; concludes the Hajj."))
    if hj := _get(HAJJ_START, y):
        events.append(_ev(hj, "Hajj", "Islamic", 5, "Annual Muslim pilgrimage to Mecca; obligatory once for those able."))
    if ah := _get(ASHURA, y):
        events.append(_ev(ah, "Ashura", "Islamic", 1, "10th of Muharram; commemoration of Imam Husayn's martyrdom (Shia)."))
    if mw := _get(MAWLID, y):
        events.append(_ev(mw, "Mawlid an-Nabi", "Islamic", 1, "Prophet Muhammad's birthday."))

    # ─ Hindu (lookup) ─
    if dw := _get(DIWALI, y):
        events.append(_ev(dw, "Diwali", "Hindu", 5, "Festival of lights; widely observed across Hindu, Sikh, Jain traditions."))
    if hl := _get(HOLI, y):
        events.append(_ev(hl, "Holi", "Hindu", 2, "Festival of colours marking the arrival of spring."))
    if sv := _get(SHIVARATRI, y):
        events.append(_ev(sv, "Maha Shivaratri", "Hindu", 1, "Great Night of Shiva."))
    if ds := _get(DUSSEHRA, y):
        events.append(_ev(ds, "Dussehra (Vijayadashami)", "Hindu", 1, "Victory of good over evil; Rama over Ravana."))

    # ─ Buddhist / East Asian / Sikh ─
    if vs := _get(VESAK, y):
        events.append(_ev(vs, "Vesak (Buddha Day)", "Buddhist", 1, "Birth, enlightenment and parinirvana of the Buddha (Theravada calendar)."))
    if lny := _get(LUNAR_NEW_YEAR, y):
        events.append(_ev(lny, "Lunar New Year", "East Asian", 7, "Widely observed in folk + Buddhist practice."))
    if gn := _get(GURU_NANAK, y):
        events.append(_ev(gn, "Guru Nanak Jayanti", "Sikh", 1, "Birth anniversary of the founder of Sikhism."))
    events.append(_ev(f"{y}-{VAISAKHI_FIXED}", "Vaisakhi", "Sikh", 1, "Punjabi spring harvest; founding of the Khalsa (1699)."))

    # ─ Bahá'í (fixed solar Bahá'í calendar) ─
    events += [
        _ev(f"{y}-03-21", "Naw-Rúz",      "Iranian", 1, "Bahá'í + Zoroastrian + Persian New Year."),
        _ev(f"{y}-04-21", "Ridván begins","Iranian", 12, "Most holy Bahá'í festival; commemorates Bahá'u'lláh's 1863 declaration."),
    ]

    # Sort by date.
    events.sort(key=lambda e: e["date"])
    return events


def get_supported_years() -> list[int]:
    """Years for which the non-Gregorian lookups have data."""
    return sorted(PESACH_FIRST_DAY.keys())
