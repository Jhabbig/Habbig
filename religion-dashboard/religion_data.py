"""Curated reference datasets for the religion + cults dashboard.

All numbers are publicly sourced and version-pinned in comments. Update by
hand when a new edition of the underlying report ships — this is reference
data, not a live feed.

Sources:
  - World religions adherent counts: Pew Research Center "The Future of
    World Religions" (baseline 2020 projection edition) and Pew "The Global
    Religious Landscape" updates. Numbers in millions.
  - Religious freedom designations: USCIRF Annual Report 2024
    (https://www.uscirf.gov/annual-reports). CPC = Country of Particular
    Concern; SWL = Special Watch List; EPC = Entity of Particular Concern.
  - New religious movements / cults watchlist: aggregated from publicly
    documented academic and journalistic sources (Britannica, ICSA, FBI
    case files, court records). Only groups with substantial public
    documentation are listed.
"""

from __future__ import annotations

# ─── World religions ─────────────────────────────────────────────────────────
# Pew Research, 2020 baseline. Adherents in millions. Growth columns are the
# 2010-2020 compound annual percent change implied by Pew's projection
# methodology (positive = growing share of world population).

WORLD_RELIGIONS = [
    # name, adherents_m, share_pct, growth_pct_yr, color
    {"name": "Christianity",   "adherents_m": 2382, "share_pct": 31.1, "growth_pct_yr":  1.27, "color": "#58a6ff"},
    {"name": "Islam",          "adherents_m": 1907, "share_pct": 24.9, "growth_pct_yr":  1.97, "color": "#56d364"},
    {"name": "Unaffiliated",   "adherents_m": 1193, "share_pct": 15.6, "growth_pct_yr":  0.32, "color": "#8b949e"},
    {"name": "Hinduism",       "adherents_m": 1161, "share_pct": 15.1, "growth_pct_yr":  1.22, "color": "#f0883e"},
    {"name": "Buddhism",       "adherents_m":  506, "share_pct":  6.6, "growth_pct_yr": -0.15, "color": "#d2a8ff"},
    {"name": "Folk religions", "adherents_m":  430, "share_pct":  5.6, "growth_pct_yr":  0.42, "color": "#f7b955"},
    {"name": "Other",          "adherents_m":   61, "share_pct":  0.8, "growth_pct_yr":  0.65, "color": "#7ee787"},
    {"name": "Judaism",        "adherents_m":   15, "share_pct":  0.2, "growth_pct_yr":  0.74, "color": "#79c0ff"},
]

# Sub-traditions for the larger traditions. Adherents in millions, rough
# breakdowns from Pew + World Christian Database / WRD.
RELIGION_SUBGROUPS = {
    "Christianity": [
        {"name": "Catholic",         "adherents_m": 1390},
        {"name": "Protestant",       "adherents_m":  900},
        {"name": "Orthodox",         "adherents_m":  220},
        {"name": "Other Christian",  "adherents_m":  130},
    ],
    "Islam": [
        {"name": "Sunni",            "adherents_m": 1700},
        {"name": "Shia",             "adherents_m":  200},
        {"name": "Other / Ahmadi",   "adherents_m":   20},
    ],
    "Buddhism": [
        {"name": "Mahayana",         "adherents_m":  300},
        {"name": "Theravada",        "adherents_m":  150},
        {"name": "Vajrayana",        "adherents_m":   20},
    ],
    "Hinduism": [
        {"name": "Vaishnavism",      "adherents_m":  580},
        {"name": "Shaivism",         "adherents_m":  290},
        {"name": "Shaktism",         "adherents_m":  140},
        {"name": "Other / Smartha",  "adherents_m":  150},
    ],
}


# ─── Religious freedom (USCIRF 2024 Annual Report) ───────────────────────────
# Only the formal designations. Tier definitions:
#   CPC = Country of Particular Concern (recommended by USCIRF)
#   SWL = Special Watch List
#   EPC = Entity of Particular Concern (non-state actors)

USCIRF_2024 = {
    "cpc": [
        {"country": "Afghanistan",       "since": 2020},
        {"country": "Burma (Myanmar)",   "since": 1999},
        {"country": "China",             "since": 1999},
        {"country": "Cuba",              "since": 2022},
        {"country": "Eritrea",           "since": 2004},
        {"country": "India",             "since": 2020},  # USCIRF recommendation
        {"country": "Iran",              "since": 1999},
        {"country": "Nicaragua",         "since": 2023},
        {"country": "Nigeria",           "since": 2020},
        {"country": "North Korea",       "since": 2001},
        {"country": "Pakistan",          "since": 2018},
        {"country": "Russia",            "since": 2020},
        {"country": "Saudi Arabia",      "since": 2004},
        {"country": "Syria",             "since": 2020},
        {"country": "Tajikistan",        "since": 2016},
        {"country": "Turkmenistan",      "since": 2014},
        {"country": "Vietnam",           "since": 2022},
    ],
    "swl": [
        {"country": "Algeria",           "since": 2020},
        {"country": "Azerbaijan",        "since": 2021},
        {"country": "Central African Republic", "since": 2021},
        {"country": "Egypt",             "since": 2018},
        {"country": "Indonesia",         "since": 2020},
        {"country": "Iraq",              "since": 2020},
        {"country": "Kazakhstan",        "since": 2020},
        {"country": "Kyrgyzstan",        "since": 2020},
        {"country": "Malaysia",          "since": 2020},
        {"country": "Sri Lanka",         "since": 2021},
        {"country": "Turkey",            "since": 2021},
        {"country": "Uzbekistan",        "since": 2020},
        {"country": "Venezuela",         "since": 2024},
    ],
    "epc": [
        {"entity": "Al-Shabaab",                          "region": "Somalia / East Africa"},
        {"entity": "Boko Haram",                          "region": "Nigeria / Lake Chad Basin"},
        {"entity": "Hayat Tahrir al-Sham",                "region": "Syria"},
        {"entity": "Houthi movement",                     "region": "Yemen"},
        {"entity": "Islamic State – Khorasan Province",   "region": "Afghanistan / Pakistan"},
        {"entity": "Islamic State – West Africa Province","region": "Nigeria"},
        {"entity": "Jamaat Nasr al-Islam wal Muslimin",   "region": "Sahel"},
        {"entity": "Taliban",                             "region": "Afghanistan"},
    ],
}


# ─── Notable new religious movements / cults watchlist ───────────────────────
# Public, well-documented groups only. Status legend:
#   active   — currently operating
#   inactive — disbanded or fully dormant
#   defunct  — leadership prosecuted / movement collapsed
# 'risk' is a qualitative tag based on publicly documented harm
# (violence, deaths, mass-suicide, criminal convictions).

CULTS_WATCHLIST = [
    {
        "name": "Peoples Temple",
        "founder": "Jim Jones",
        "founded": 1955,
        "ended": 1978,
        "country": "United States / Guyana",
        "status": "defunct",
        "category": "Christian apocalyptic",
        "risk": "extreme",
        "members_peak": 5000,
        "summary": "Apocalyptic Christian socialist movement; ended in the Jonestown mass murder-suicide of 909 people in 1978.",
    },
    {
        "name": "Heaven's Gate",
        "founder": "Marshall Applewhite & Bonnie Nettles",
        "founded": 1974,
        "ended": 1997,
        "country": "United States",
        "status": "defunct",
        "category": "UFO religion",
        "risk": "extreme",
        "members_peak": 200,
        "summary": "UFO-millenarian group; 39 members died by mass suicide in March 1997 to 'evacuate Earth' aboard a perceived spacecraft trailing comet Hale-Bopp.",
    },
    {
        "name": "Branch Davidians",
        "founder": "Benjamin Roden / David Koresh",
        "founded": 1955,
        "ended": 1993,
        "country": "United States",
        "status": "defunct",
        "category": "Adventist offshoot",
        "risk": "extreme",
        "members_peak": 130,
        "summary": "Adventist sect at Mount Carmel near Waco, TX; 51-day ATF/FBI siege ended in fire that killed 76 members including children.",
    },
    {
        "name": "Aum Shinrikyo",
        "founder": "Shoko Asahara",
        "founded": 1984,
        "ended": None,
        "country": "Japan",
        "status": "active (renamed Aleph / Hikari no Wa)",
        "category": "Buddhist / apocalyptic",
        "risk": "extreme",
        "members_peak": 40000,
        "summary": "Doomsday cult responsible for the 1995 Tokyo subway sarin attack (13 dead, 5,800 injured). Asahara executed 2018; remnants under Japanese surveillance.",
    },
    {
        "name": "The Family International",
        "founder": "David Berg",
        "founded": 1968,
        "ended": None,
        "country": "United States / global",
        "status": "active",
        "category": "Christian fringe",
        "risk": "high",
        "members_peak": 10000,
        "summary": "Originally 'Children of God'. Repeatedly investigated for institutionalised child sexual abuse documented in court cases and survivor memoirs.",
    },
    {
        "name": "Order of the Solar Temple",
        "founder": "Joseph Di Mambro & Luc Jouret",
        "founded": 1984,
        "ended": 1997,
        "country": "Switzerland / France / Quebec",
        "status": "defunct",
        "category": "Neo-Templar / New Age",
        "risk": "extreme",
        "members_peak": 600,
        "summary": "Esoteric order responsible for coordinated mass murder-suicides in 1994, 1995 and 1997; 74 members killed across three countries.",
    },
    {
        "name": "Manson Family",
        "founder": "Charles Manson",
        "founded": 1967,
        "ended": 1969,
        "country": "United States",
        "status": "defunct",
        "category": "Apocalyptic commune",
        "risk": "extreme",
        "members_peak": 100,
        "summary": "California commune around Charles Manson; convicted of the Tate–LaBianca murders (1969). Manson died in prison 2017.",
    },
    {
        "name": "Rajneesh movement",
        "founder": "Bhagwan Shree Rajneesh (Osho)",
        "founded": 1970,
        "ended": 1985,
        "country": "India / United States",
        "status": "active (as Osho International Foundation)",
        "category": "Neo-Sannyas / New Age",
        "risk": "high",
        "members_peak": 200000,
        "summary": "Neo-sannyas movement; the Rajneeshpuram commune in Oregon (1981–85) carried out the largest bioterror attack in US history (1984 Salmonella poisoning of 751 people).",
    },
    {
        "name": "NXIVM",
        "founder": "Keith Raniere",
        "founded": 1998,
        "ended": 2018,
        "country": "United States",
        "status": "defunct",
        "category": "Self-help / coercive",
        "risk": "high",
        "members_peak": 17000,
        "summary": "'Executive Success Programs' marketed as self-help; Raniere convicted 2019 of racketeering, sex trafficking and forced labour. Sentenced to 120 years.",
    },
    {
        "name": "Church of Scientology",
        "founder": "L. Ron Hubbard",
        "founded": 1953,
        "ended": None,
        "country": "United States / global",
        "status": "active",
        "category": "Self-religion",
        "risk": "moderate",
        "members_peak": 50000,
        "summary": "Hubbard-founded religion. Subject to ongoing litigation, journalistic investigations, and recognition disputes (German government refuses recognition; France classified as a sect).",
    },
    {
        "name": "Twelve Tribes",
        "founder": "Eugene Spriggs",
        "founded": 1972,
        "ended": None,
        "country": "United States / global",
        "status": "active",
        "category": "Christian communal",
        "risk": "moderate",
        "members_peak": 3000,
        "summary": "Messianic communal movement. German courts removed children from members in 2013 after documented corporal-punishment practices.",
    },
    {
        "name": "Fundamentalist LDS Church",
        "founder": "Joseph White Musser / Warren Jeffs",
        "founded": 1935,
        "ended": None,
        "country": "United States",
        "status": "active",
        "category": "Mormon offshoot",
        "risk": "high",
        "members_peak": 10000,
        "summary": "Polygamist Mormon offshoot. Warren Jeffs convicted 2011 of child sexual assault; sentenced to life plus 20 years.",
    },
    {
        "name": "Unification Church",
        "founder": "Sun Myung Moon",
        "founded": 1954,
        "ended": None,
        "country": "South Korea / global",
        "status": "active",
        "category": "Christian-derived NRM",
        "risk": "moderate",
        "members_peak": 3000000,
        "summary": "Founded in postwar Korea. Linked to assassination of Japanese PM Shinzo Abe (2022) by son of a member; subject to ongoing dissolution proceedings in Japan (2023–).",
    },
    {
        "name": "Raëlian Movement",
        "founder": "Claude Vorilhon (Raël)",
        "founded": 1974,
        "ended": None,
        "country": "France / global",
        "status": "active",
        "category": "UFO religion",
        "risk": "low",
        "members_peak": 100000,
        "summary": "UFO religion claiming humans were created by extraterrestrials. Made false 2002 claim of having cloned a human ('Eve').",
    },
    {
        "name": "Movement for the Restoration of the Ten Commandments",
        "founder": "Credonia Mwerinde & Joseph Kibweteere",
        "founded": 1989,
        "ended": 2000,
        "country": "Uganda",
        "status": "defunct",
        "category": "Catholic apocalyptic",
        "risk": "extreme",
        "members_peak": 5000,
        "summary": "Apocalyptic Catholic offshoot. At least 778 members killed in 2000 — initially believed a mass suicide, later determined to be mass murder by leadership.",
    },
]


# ─── Curated keyword config for live data fetchers ───────────────────────────
# Used by the Polymarket fetcher and news aggregator. Tuned to filter the
# noise — these markets and headlines drift across many tag slugs.

POLYMARKET_TAG_SLUGS = [
    "religion", "pope", "vatican", "catholic", "papacy",
]

POLYMARKET_KEYWORDS = [
    "pope", "papacy", "vatican", "cardinal", "conclave",
    "religion", "religious", "church", "catholic", "christian",
    "islam", "muslim", "buddhist", "hindu", "jewish",
    "dalai lama", "ayatollah", "mecca", "vatican", "mormon",
    "scientology", "cult",
]

POLYMARKET_REJECT = [
    "nfl", "nba", "nhl", "mlb", "premier league",
    "bitcoin", "ethereum", "crypto", "btc", "spacex",
    "election", "midterm", "senate",
]

NEWS_RSS_FEEDS = [
    # Long-running general religion desks. RSS only — no API keys required.
    ("BBC Religion & Ethics", "https://feeds.bbci.co.uk/news/topics/c008ql15zg6t/rss.xml"),
    ("Religion News Service",  "https://religionnews.com/feed/"),
    ("AP — Religion",          "https://apnews.com/hub/religion.rss"),
    ("Vatican News",           "https://www.vaticannews.va/en.rss.xml"),
]
