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
#
# RISK SCORING (4-axis, each 0-10). Composite is the unweighted mean.
# The framework follows the cult-studies literature (Singer's "Cults in
# Our Midst", Lalich's "Bounded Choice", and ICSA's published
# group-assessment criteria).
#
#   financial_opacity     — public filings absent, no audits, opaque revenue
#   leadership_risk       — single founder, no successor named, charismatic
#                           dependency
#   isolation             — closed compound, severance of external ties,
#                           internet / family contact restricted
#   criminal_disclosure   — documented convictions, ongoing investigations,
#                           pattern of abuse claims
#
# Composite buckets (the 'risk' label):
#   extreme : >= 8.0
#   high    : 6.0 – 8.0
#   moderate: 4.0 – 6.0
#   low     : <  4.0

CULTS_WATCHLIST = [
    {
        "name": "Peoples Temple", "founder": "Jim Jones", "founded": 1955, "ended": 1978,
        "country": "United States / Guyana", "status": "defunct", "category": "Christian apocalyptic",
        "members_peak": 5000,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 10, "isolation": 10, "criminal_disclosure": 10},
        "summary": "Apocalyptic Christian socialist movement; ended in the Jonestown mass murder-suicide of 909 people in 1978.",
    },
    {
        "name": "Heaven's Gate", "founder": "Marshall Applewhite & Bonnie Nettles", "founded": 1974, "ended": 1997,
        "country": "United States", "status": "defunct", "category": "UFO religion",
        "members_peak": 200,
        "risk_axes": {"financial_opacity": 7, "leadership_risk": 10, "isolation": 10, "criminal_disclosure": 9},
        "summary": "UFO-millenarian group; 39 members died by mass suicide in March 1997 to 'evacuate Earth' aboard a perceived spacecraft trailing comet Hale-Bopp.",
    },
    {
        "name": "Branch Davidians", "founder": "Benjamin Roden / David Koresh", "founded": 1955, "ended": 1993,
        "country": "United States", "status": "defunct", "category": "Adventist offshoot",
        "members_peak": 130,
        "risk_axes": {"financial_opacity": 6, "leadership_risk": 9, "isolation": 9, "criminal_disclosure": 10},
        "summary": "Adventist sect at Mount Carmel near Waco, TX; 51-day ATF/FBI siege ended in fire that killed 76 members including children.",
    },
    {
        "name": "Aum Shinrikyo", "founder": "Shoko Asahara", "founded": 1984, "ended": None,
        "country": "Japan", "status": "active (renamed Aleph / Hikari no Wa)", "category": "Buddhist / apocalyptic",
        "members_peak": 40000,
        "risk_axes": {"financial_opacity": 9, "leadership_risk": 10, "isolation": 9, "criminal_disclosure": 10},
        "summary": "Doomsday cult responsible for the 1995 Tokyo subway sarin attack (13 dead, 5,800 injured). Asahara executed 2018; remnants under Japanese surveillance.",
    },
    {
        "name": "The Family International", "founder": "David Berg", "founded": 1968, "ended": None,
        "country": "United States / global", "status": "active", "category": "Christian fringe",
        "members_peak": 10000,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 7, "isolation": 6, "criminal_disclosure": 9},
        "summary": "Originally 'Children of God'. Repeatedly investigated for institutionalised child sexual abuse documented in court cases and survivor memoirs.",
    },
    {
        "name": "Order of the Solar Temple", "founder": "Joseph Di Mambro & Luc Jouret", "founded": 1984, "ended": 1997,
        "country": "Switzerland / France / Quebec", "status": "defunct", "category": "Neo-Templar / New Age",
        "members_peak": 600,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 10, "isolation": 10, "criminal_disclosure": 10},
        "summary": "Esoteric order responsible for coordinated mass murder-suicides in 1994, 1995 and 1997; 74 members killed across three countries.",
    },
    {
        "name": "Manson Family", "founder": "Charles Manson", "founded": 1967, "ended": 1969,
        "country": "United States", "status": "defunct", "category": "Apocalyptic commune",
        "members_peak": 100,
        "risk_axes": {"financial_opacity": 7, "leadership_risk": 10, "isolation": 9, "criminal_disclosure": 10},
        "summary": "California commune around Charles Manson; convicted of the Tate–LaBianca murders (1969). Manson died in prison 2017.",
    },
    {
        "name": "Rajneesh movement", "founder": "Bhagwan Shree Rajneesh (Osho)", "founded": 1970, "ended": 1985,
        "country": "India / United States", "status": "active (as Osho International Foundation)", "category": "Neo-Sannyas / New Age",
        "members_peak": 200000,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 8, "isolation": 7, "criminal_disclosure": 9},
        "summary": "Neo-sannyas movement; the Rajneeshpuram commune in Oregon (1981–85) carried out the largest bioterror attack in US history (1984 Salmonella poisoning of 751 people).",
    },
    {
        "name": "NXIVM", "founder": "Keith Raniere", "founded": 1998, "ended": 2018,
        "country": "United States", "status": "defunct", "category": "Self-help / coercive",
        "members_peak": 17000,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 9, "isolation": 7, "criminal_disclosure": 10},
        "summary": "'Executive Success Programs' marketed as self-help; Raniere convicted 2019 of racketeering, sex trafficking and forced labour. Sentenced to 120 years.",
    },
    {
        "name": "Church of Scientology", "founder": "L. Ron Hubbard", "founded": 1953, "ended": None,
        "country": "United States / global", "status": "active", "category": "Self-religion",
        "members_peak": 50000,
        "risk_axes": {"financial_opacity": 9, "leadership_risk": 7, "isolation": 6, "criminal_disclosure": 4},
        "summary": "Hubbard-founded religion. Subject to ongoing litigation, journalistic investigations, and recognition disputes (German government refuses recognition; France classified as a sect).",
    },
    {
        "name": "Twelve Tribes", "founder": "Eugene Spriggs", "founded": 1972, "ended": None,
        "country": "United States / global", "status": "active", "category": "Christian communal",
        "members_peak": 3000,
        "risk_axes": {"financial_opacity": 7, "leadership_risk": 6, "isolation": 7, "criminal_disclosure": 5},
        "summary": "Messianic communal movement. German courts removed children from members in 2013 after documented corporal-punishment practices.",
    },
    {
        "name": "Fundamentalist LDS Church", "founder": "Joseph White Musser / Warren Jeffs", "founded": 1935, "ended": None,
        "country": "United States", "status": "active", "category": "Mormon offshoot",
        "members_peak": 10000,
        "risk_axes": {"financial_opacity": 8, "leadership_risk": 8, "isolation": 9, "criminal_disclosure": 10},
        "summary": "Polygamist Mormon offshoot. Warren Jeffs convicted 2011 of child sexual assault; sentenced to life plus 20 years.",
    },
    {
        "name": "Unification Church", "founder": "Sun Myung Moon", "founded": 1954, "ended": None,
        "country": "South Korea / global", "status": "active", "category": "Christian-derived NRM",
        "members_peak": 3000000,
        "risk_axes": {"financial_opacity": 6, "leadership_risk": 4, "isolation": 3, "criminal_disclosure": 6},
        "summary": "Founded in postwar Korea. Linked to assassination of Japanese PM Shinzo Abe (2022) by son of a member; subject to ongoing dissolution proceedings in Japan (2023–).",
    },
    {
        "name": "Raëlian Movement", "founder": "Claude Vorilhon (Raël)", "founded": 1974, "ended": None,
        "country": "France / global", "status": "active", "category": "UFO religion",
        "members_peak": 100000,
        "risk_axes": {"financial_opacity": 4, "leadership_risk": 5, "isolation": 2, "criminal_disclosure": 2},
        "summary": "UFO religion claiming humans were created by extraterrestrials. Made false 2002 claim of having cloned a human ('Eve').",
    },
    {
        "name": "Movement for the Restoration of the Ten Commandments",
        "founder": "Credonia Mwerinde & Joseph Kibweteere", "founded": 1989, "ended": 2000,
        "country": "Uganda", "status": "defunct", "category": "Catholic apocalyptic",
        "members_peak": 5000,
        "risk_axes": {"financial_opacity": 9, "leadership_risk": 10, "isolation": 10, "criminal_disclosure": 10},
        "summary": "Apocalyptic Catholic offshoot. At least 778 members killed in 2000 — initially believed a mass suicide, later determined to be mass murder by leadership.",
    },
]


def cult_risk_score(c: dict) -> tuple[float, str]:
    """Composite risk score (mean of 4 axes) and bucket label."""
    ax = c.get("risk_axes") or {}
    vals = [ax.get(k, 0) for k in ("financial_opacity", "leadership_risk", "isolation", "criminal_disclosure")]
    score = sum(vals) / 4
    if score >= 8.0:   bucket = "extreme"
    elif score >= 6.0: bucket = "high"
    elif score >= 4.0: bucket = "moderate"
    else:              bucket = "low"
    return round(score, 2), bucket


# ─── Full religions registry (100 traditions) ────────────────────────────────
# One entry per major denomination, sect, school, or movement that meets at
# least one of: (a) ≥100k current adherents with a continuous institutional
# presence, (b) UN-recognised state confession, or (c) inclusion in Pew /
# WRD / Britannica survey of world religions.
#
# Adherents are mid-range public estimates in millions. Sources: Pew
# Research, World Religion Database (Boston Univ.), ARDA, the Britannica
# Yearbook, and official church / community censuses where available.
#
# CAVEATS (read before reasoning over totals):
#   - Hindu denominations (Vaishnavism / Shaivism / Shaktism / Smartism)
#     are not always exclusive — many Hindus venerate deities across
#     traditions. Numbers reflect *primary* identification.
#   - Pentecostal and Evangelical overlap; Anglican overlaps with
#     Evangelical for many Anglicans worldwide. Do not sum.
#   - Folk and indigenous numbers are squishy by definition.
#   - 'family' is a rough taxonomic bucket for filtering, not a
#     theological claim.

RELIGIONS_FULL = [
    # ─ Christianity (18) ─
    {"name": "Roman Catholic Church",                     "family": "Christian",  "adherents_m": 1390.0, "founded": "c. 30 CE", "origin": "Levant",                "summary": "Largest Christian communion; centred on the Bishop of Rome."},
    {"name": "Eastern Orthodoxy",                         "family": "Christian",  "adherents_m":  260.0, "founded": "1054",     "origin": "Eastern Mediterranean", "summary": "Conciliar communion of autocephalous churches (Russian, Greek, Serbian, Romanian, Bulgarian)."},
    {"name": "Oriental Orthodoxy",                        "family": "Christian",  "adherents_m":   62.0, "founded": "451",      "origin": "Egypt / Ethiopia / Armenia", "summary": "Non-Chalcedonian churches: Coptic, Ethiopian, Armenian, Syriac, Eritrean, Indian Malankara."},
    {"name": "Pentecostalism",                            "family": "Christian",  "adherents_m":  280.0, "founded": "1906",     "origin": "USA",                   "summary": "Charismatic Protestant movement emphasising baptism in the Spirit and spiritual gifts."},
    {"name": "Evangelical Christianity",                  "family": "Christian",  "adherents_m":  450.0, "founded": "1730s",    "origin": "UK / USA",              "summary": "Cross-denominational Protestant movement centred on personal conversion (overlap with Pentecostal/Anglican)."},
    {"name": "Anglican Communion",                        "family": "Christian",  "adherents_m":   85.0, "founded": "1534",     "origin": "England",               "summary": "Communion of churches in fellowship with the Archbishop of Canterbury."},
    {"name": "Lutheranism",                               "family": "Christian",  "adherents_m":   74.0, "founded": "1517",     "origin": "Germany",               "summary": "Reformation church founded on Martin Luther's theology."},
    {"name": "Reformed / Presbyterian",                   "family": "Christian",  "adherents_m":   75.0, "founded": "1536",     "origin": "Switzerland",           "summary": "Reformation tradition rooted in Calvin and Knox."},
    {"name": "Methodism",                                 "family": "Christian",  "adherents_m":   80.0, "founded": "1739",     "origin": "England",               "summary": "Wesleyan Protestant tradition emphasising personal holiness."},
    {"name": "Baptist",                                   "family": "Christian",  "adherents_m":  100.0, "founded": "1609",     "origin": "England / Netherlands", "summary": "Believer's-baptism Protestant tradition."},
    {"name": "Seventh-day Adventism",                     "family": "Christian",  "adherents_m":   22.0, "founded": "1863",     "origin": "USA",                   "summary": "Adventist Protestant church observing Saturday Sabbath; founded around the visions of Ellen G. White."},
    {"name": "Latter-day Saints (Mormonism)",             "family": "Christian",  "adherents_m":   17.0, "founded": "1830",     "origin": "USA",                   "summary": "Restorationist movement founded by Joseph Smith; based in Utah."},
    {"name": "Jehovah's Witnesses",                       "family": "Christian",  "adherents_m":    9.0, "founded": "1872",     "origin": "USA",                   "summary": "Restorationist millenarian movement; rejects Trinity and refuses blood transfusions."},
    {"name": "Anabaptism (Mennonite / Amish / Hutterite)","family": "Christian",  "adherents_m":    2.1, "founded": "1525",     "origin": "Switzerland",           "summary": "Radical Reformation pacifist tradition."},
    {"name": "Quakerism (Religious Society of Friends)",  "family": "Christian",  "adherents_m":    0.4, "founded": "1647",     "origin": "England",               "summary": "Inner-light Christian tradition founded by George Fox; pacifist."},
    {"name": "Iglesia ni Cristo",                         "family": "Christian",  "adherents_m":    3.0, "founded": "1914",     "origin": "Philippines",           "summary": "Filipino restorationist church founded by Felix Manalo."},
    {"name": "African Independent Churches",              "family": "Christian",  "adherents_m":   80.0, "founded": "20th c.",  "origin": "Sub-Saharan Africa",    "summary": "Family of indigenous African Christian movements (Kimbanguist, Aladura, Zionist, Cherubim & Seraphim)."},
    {"name": "Old Catholic Church",                       "family": "Christian",  "adherents_m":    1.0, "founded": "1870",     "origin": "Netherlands / Germany", "summary": "Communion of churches that rejected Vatican I papal infallibility."},

    # ─ Islam (8) ─
    {"name": "Sunni Islam",                               "family": "Islamic",    "adherents_m": 1700.0, "founded": "632",      "origin": "Arabia",                "summary": "Largest Islamic branch; follows the consensus of the four Rashidun caliphs."},
    {"name": "Twelver Shia Islam",                        "family": "Islamic",    "adherents_m":  170.0, "founded": "c. 680",   "origin": "Iraq / Persia",         "summary": "Largest Shia tradition; awaits the return of the 12th Imam (Mahdi). Iran's state religion."},
    {"name": "Ismailism",                                 "family": "Islamic",    "adherents_m":   15.0, "founded": "765",      "origin": "Persia / Egypt",        "summary": "Sevener Shia tradition; Nizari branch led today by the Aga Khan."},
    {"name": "Zaidism",                                   "family": "Islamic",    "adherents_m":   10.0, "founded": "740",      "origin": "Yemen",                 "summary": "Fiver Shia tradition centred in Yemen; Houthi movement is Zaidi."},
    {"name": "Ibadism",                                   "family": "Islamic",    "adherents_m":    3.0, "founded": "c. 657",   "origin": "Oman / Algeria",        "summary": "Earliest non-Sunni / non-Shia branch; Oman's state confession."},
    {"name": "Ahmadiyya",                                 "family": "Islamic",    "adherents_m":   12.0, "founded": "1889",     "origin": "Punjab",                "summary": "Reform movement of Mirza Ghulam Ahmad; declared non-Muslim by Pakistan."},
    {"name": "Alawism",                                   "family": "Islamic",    "adherents_m":    3.0, "founded": "c. 1000",  "origin": "Syria",                 "summary": "Esoteric Shia offshoot; the Assad family's confession."},
    {"name": "Druze",                                     "family": "Islamic",    "adherents_m":    1.0, "founded": "1017",     "origin": "Egypt / Levant",        "summary": "Esoteric Ismaili-derived tradition; closed community in Lebanon, Syria, Israel."},

    # ─ Hinduism (10) ─
    {"name": "Vaishnavism",                               "family": "Hindu",      "adherents_m":  580.0, "founded": "ancient",  "origin": "India",                 "summary": "Hindu denomination centred on Vishnu and his avatars (Krishna, Rama)."},
    {"name": "Shaivism",                                  "family": "Hindu",      "adherents_m":  280.0, "founded": "ancient",  "origin": "India",                 "summary": "Hindu denomination centred on Shiva."},
    {"name": "Shaktism",                                  "family": "Hindu",      "adherents_m":  140.0, "founded": "ancient",  "origin": "India",                 "summary": "Hindu denomination centred on the Goddess (Devi / Durga / Kali)."},
    {"name": "Smartism",                                  "family": "Hindu",      "adherents_m":   70.0, "founded": "c. 8th c.","origin": "India",                 "summary": "Adi Shankara's panentheistic synthesis tradition."},
    {"name": "Lingayatism (Veerashaivism)",               "family": "Hindu",      "adherents_m":   10.0, "founded": "1160",     "origin": "Karnataka",             "summary": "Basava-founded reform movement; sometimes claimed as a separate religion."},
    {"name": "Arya Samaj",                                "family": "Hindu",      "adherents_m":    4.0, "founded": "1875",     "origin": "Punjab",                "summary": "Vedic-revivalist reform movement of Dayananda Saraswati."},
    {"name": "ISKCON (Hare Krishna)",                     "family": "Hindu",      "adherents_m":    1.0, "founded": "1966",     "origin": "USA",                   "summary": "International Vaishnavite movement founded by A. C. Bhaktivedanta Swami Prabhupada."},
    {"name": "Swaminarayan Sampradaya",                   "family": "Hindu",      "adherents_m":   20.0, "founded": "1801",     "origin": "Gujarat",               "summary": "Vaishnava tradition founded by Sahajanand Swami; BAPS is its largest branch."},
    {"name": "Sathya Sai Baba movement",                  "family": "Hindu",      "adherents_m":   30.0, "founded": "1940s",    "origin": "Andhra Pradesh",        "summary": "Devotional movement around Sathya Sai Baba (1926-2011)."},
    {"name": "Ravidassia",                                "family": "Hindu",      "adherents_m":    4.0, "founded": "c. 15th c.","origin": "Punjab",               "summary": "Sant tradition centred on Guru Ravidas; some adherents claim distinct-religion status."},

    # ─ Buddhism (7) ─
    {"name": "Mahayana Buddhism (East Asian)",            "family": "Buddhist",   "adherents_m":  300.0, "founded": "c. 1st c. CE", "origin": "India",             "summary": "'Great Vehicle' tradition; dominant in China, Korea, Vietnam."},
    {"name": "Theravada Buddhism",                        "family": "Buddhist",   "adherents_m":  150.0, "founded": "c. 3rd c. BCE","origin": "Sri Lanka / SE Asia","summary": "'Way of the Elders'; preserves the Pali canon. Sri Lanka, Burma, Thailand, Laos, Cambodia."},
    {"name": "Pure Land Buddhism",                        "family": "Buddhist",   "adherents_m":  200.0, "founded": "c. 5th c.","origin": "China / Japan",         "summary": "Amitabha-focused Mahayana school; the largest single school in East Asia."},
    {"name": "Vajrayana / Tibetan Buddhism",              "family": "Buddhist",   "adherents_m":   20.0, "founded": "c. 7th c.","origin": "Tibet",                 "summary": "Tantric Mahayana under the Dalai Lama, Karmapa, and other tulku lineages."},
    {"name": "Zen Buddhism",                              "family": "Buddhist",   "adherents_m":    9.0, "founded": "c. 6th c.","origin": "China / Japan",         "summary": "Meditation-focused Mahayana school (Chan in Chinese, Seon in Korean)."},
    {"name": "Nichiren Buddhism (incl. Soka Gakkai)",     "family": "Buddhist",   "adherents_m":   12.0, "founded": "1253",     "origin": "Japan",                 "summary": "Lotus-Sutra-focused Mahayana school; Soka Gakkai is its largest lay organisation."},
    {"name": "Triratna Buddhist Community",               "family": "Buddhist",   "adherents_m":    0.1, "founded": "1967",     "origin": "UK",                    "summary": "Western lay Buddhist movement founded by Sangharakshita."},

    # ─ Sikhism + Jainism (5) ─
    {"name": "Sikhism (Khalsa Panth)",                    "family": "Sikh / Jain","adherents_m":   30.0, "founded": "1469",     "origin": "Punjab",                "summary": "Founded by Guru Nanak; world's fifth-largest organised religion."},
    {"name": "Namdhari Sikhism",                          "family": "Sikh / Jain","adherents_m":    0.5, "founded": "1857",     "origin": "Punjab",                "summary": "Reform Sikh sect that recognises a living guru."},
    {"name": "Sant Nirankari Mission",                    "family": "Sikh / Jain","adherents_m":    1.0, "founded": "1929",     "origin": "Punjab",                "summary": "Reform movement; recognises a living spiritual master (regarded as heretical by mainstream Sikhi)."},
    {"name": "Svetambara Jainism",                        "family": "Sikh / Jain","adherents_m":    4.0, "founded": "c. 5th c. CE","origin": "India",              "summary": "White-clad Jain branch; permits monks to wear robes."},
    {"name": "Digambara Jainism",                         "family": "Sikh / Jain","adherents_m":    0.5, "founded": "c. 5th c. CE","origin": "India",              "summary": "Sky-clad Jain branch; senior monks practise nudity as renunciation."},

    # ─ Judaism (7) ─
    {"name": "Orthodox Judaism",                          "family": "Jewish",     "adherents_m":    4.0, "founded": "1851",     "origin": "Europe",                "summary": "Tradition-observant Judaism upholding rabbinic halakha."},
    {"name": "Haredi Judaism",                            "family": "Jewish",     "adherents_m":    2.0, "founded": "18th c.",  "origin": "Eastern Europe",        "summary": "Strict-Orthodox Judaism; encompasses Hasidic and Litvish communities."},
    {"name": "Conservative / Masorti Judaism",            "family": "Jewish",     "adherents_m":    2.0, "founded": "1860s",    "origin": "USA / Germany",         "summary": "Tradition-positive non-Orthodox movement; respects halakha while accepting historical change."},
    {"name": "Reform Judaism",                            "family": "Jewish",     "adherents_m":    3.0, "founded": "1810",     "origin": "Germany",               "summary": "Liberal modernising movement; the largest Jewish stream in the US."},
    {"name": "Reconstructionist Judaism",                 "family": "Jewish",     "adherents_m":    0.2, "founded": "1922",     "origin": "USA",                   "summary": "Mordecai Kaplan's civilisation-based movement."},
    {"name": "Karaite Judaism",                           "family": "Jewish",     "adherents_m":    0.04,"founded": "c. 8th c.","origin": "Babylon",               "summary": "Scripture-only non-rabbinic Judaism; rejects the Oral Torah."},
    {"name": "Samaritanism",                              "family": "Jewish",     "adherents_m":    0.001,"founded": "ancient", "origin": "Levant",                "summary": "Mount-Gerizim-centred Israelite religion; ~850 surviving adherents."},

    # ─ Iranian / Near-Eastern minorities (5) ─
    {"name": "Bahá'í Faith",                              "family": "Iranian",    "adherents_m":    7.5, "founded": "1863",     "origin": "Persia",                "summary": "Universalist Abrahamic religion of Bahá'u'lláh; persecuted in Iran."},
    {"name": "Zoroastrianism",                            "family": "Iranian",    "adherents_m":    0.2, "founded": "c. 1500 BCE","origin": "Persia",              "summary": "Pre-Islamic religion of Zarathustra; survives in Parsi communities of Mumbai and in Iran."},
    {"name": "Yazidism",                                  "family": "Iranian",    "adherents_m":    0.7, "founded": "c. 12th c.","origin": "Iraq / Kurdistan",     "summary": "Kurdish-speaking syncretic religion centred on Tawûsê Melek; targeted by ISIS in 2014 (genocide recognition)."},
    {"name": "Yarsanism (Ahl-e Haqq)",                    "family": "Iranian",    "adherents_m":    1.0, "founded": "14th c.",  "origin": "Iran",                  "summary": "Kurdish esoteric religion of Sultan Sahak."},
    {"name": "Mandaeism",                                 "family": "Iranian",    "adherents_m":    0.06,"founded": "c. 1st c. CE","origin": "Iraq",               "summary": "Gnostic religion revering John the Baptist; mostly displaced from Iraq since 2003."},

    # ─ East Asian (12) ─
    {"name": "Chinese folk religion",                     "family": "East Asian", "adherents_m":  430.0, "founded": "ancient",  "origin": "China",                 "summary": "Syncretic local-deity tradition; the de facto religion of much of China."},
    {"name": "Taoism",                                    "family": "East Asian", "adherents_m":   12.0, "founded": "c. 4th c. BCE","origin": "China",             "summary": "Religion of the Dao; Laozi's Tao Te Ching."},
    {"name": "Confucianism",                              "family": "East Asian", "adherents_m":    6.0, "founded": "c. 5th c. BCE","origin": "China",             "summary": "Ethical-philosophical tradition; widespread cultural influence across East Asia."},
    {"name": "Shinto",                                    "family": "East Asian", "adherents_m":    4.0, "founded": "ancient",  "origin": "Japan",                 "summary": "Indigenous polytheistic religion of Japan; ~80M cultural participants."},
    {"name": "Korean Shamanism (Muism)",                  "family": "East Asian", "adherents_m":    8.0, "founded": "ancient",  "origin": "Korea",                 "summary": "Indigenous Korean shamanic religion."},
    {"name": "Cao Đài",                                   "family": "East Asian", "adherents_m":    4.0, "founded": "1926",     "origin": "Vietnam",               "summary": "Syncretic monotheistic NRM (Buddhism + Catholicism + Taoism + Confucianism)."},
    {"name": "Hoa Hao Buddhism",                          "family": "East Asian", "adherents_m":    1.5, "founded": "1939",     "origin": "Vietnam",               "summary": "Reformist lay Buddhist movement of Huỳnh Phú Sổ."},
    {"name": "Tenrikyo",                                  "family": "East Asian", "adherents_m":    1.2, "founded": "1838",     "origin": "Japan",                 "summary": "Japanese new religion centred on the revelations of Nakayama Miki (Oyasama)."},
    {"name": "Cheondoism",                                "family": "East Asian", "adherents_m":    1.0, "founded": "1860",     "origin": "Korea",                 "summary": "Korean monotheistic religion (Donghak heritage); state-recognised in DPRK."},
    {"name": "Won Buddhism",                              "family": "East Asian", "adherents_m":    1.0, "founded": "1916",     "origin": "Korea",                 "summary": "Reformed Korean Buddhism founded by Sotaesan."},
    {"name": "Bön",                                       "family": "East Asian", "adherents_m":    0.4, "founded": "pre-Buddhist","origin": "Tibet",              "summary": "Pre-Buddhist Tibetan religion; survives alongside Tibetan Buddhism."},
    {"name": "Tengrism",                                  "family": "East Asian", "adherents_m":    0.5, "founded": "ancient",  "origin": "Central Asia",          "summary": "Sky-god religion of Turkic and Mongolic peoples; revival movements in Mongolia, Tuva, Kyrgyzstan."},

    # ─ Indigenous / African-diasporic (8) ─
    {"name": "Yoruba religion (Ifá)",                     "family": "Indigenous", "adherents_m":   40.0, "founded": "ancient",  "origin": "West Africa",           "summary": "Yoruba-rooted Òrìṣà tradition; root of Atlantic-diaspora religions."},
    {"name": "Haitian Vodou",                             "family": "Indigenous", "adherents_m":   50.0, "founded": "c. 16th c.","origin": "Haiti / W. Africa",    "summary": "Syncretic Vodun-rooted religion; widely practised in Haiti and the Haitian diaspora."},
    {"name": "Santería (Lucumí)",                         "family": "Indigenous", "adherents_m":    1.0, "founded": "19th c.",  "origin": "Cuba",                  "summary": "Yoruba-derived African-diasporic religion; recognised in Cuba and the US."},
    {"name": "Candomblé",                                 "family": "Indigenous", "adherents_m":    3.0, "founded": "19th c.",  "origin": "Brazil",                "summary": "Yoruba- and Bantu-derived African-diasporic religion."},
    {"name": "Umbanda",                                   "family": "Indigenous", "adherents_m":    0.5, "founded": "1908",     "origin": "Brazil",                "summary": "Brazilian syncretism (Candomblé + Spiritism + Catholicism)."},
    {"name": "Rastafari",                                 "family": "Indigenous", "adherents_m":    1.0, "founded": "1930s",    "origin": "Jamaica",               "summary": "Afrocentric Abrahamic NRM venerating Haile Selassie I."},
    {"name": "Native American religions",                 "family": "Indigenous", "adherents_m":    0.6, "founded": "ancient",  "origin": "Americas",              "summary": "Family of indigenous traditions of the Americas (Lakota, Navajo, Iroquois, Quechua, etc.)."},
    {"name": "Australian Aboriginal Dreaming",            "family": "Indigenous", "adherents_m":    0.1, "founded": "ancient",  "origin": "Australia",             "summary": "Indigenous traditions rooted in Dreamtime cosmology."},

    # ─ Modern / new religious movements (20) ─
    {"name": "Spiritism (Kardecism)",                     "family": "NRM",        "adherents_m":   15.0, "founded": "1857",     "origin": "France / Brazil",       "summary": "Allan Kardec's Spiritualist religion; very large in Brazil."},
    {"name": "Wicca",                                     "family": "NRM",        "adherents_m":    1.0, "founded": "1954",     "origin": "England",               "summary": "Modern witchcraft tradition founded by Gerald Gardner."},
    {"name": "Neopaganism (general)",                     "family": "NRM",        "adherents_m":    1.5, "founded": "1930s",    "origin": "UK / USA",              "summary": "Modern reconstructionist polytheism (umbrella term)."},
    {"name": "Heathenry / Ásatrú",                        "family": "NRM",        "adherents_m":    0.1, "founded": "1972",     "origin": "Iceland / UK",          "summary": "Norse-revivalist neopagan tradition; Iceland's fastest-growing religion."},
    {"name": "Druidry (modern)",                          "family": "NRM",        "adherents_m":    0.05,"founded": "18th c.",  "origin": "UK",                    "summary": "Romantic Celtic-revival tradition."},
    {"name": "Hellenism (modern)",                        "family": "NRM",        "adherents_m":    0.05,"founded": "1990s",    "origin": "Greece",                "summary": "Reconstructed Greek polytheism (Hellenismos); recognised in Greece in 2017."},
    {"name": "Kemetism",                                  "family": "NRM",        "adherents_m":    0.02,"founded": "1980s",    "origin": "USA",                   "summary": "Reconstructed Egyptian polytheism."},
    {"name": "Theosophy",                                 "family": "NRM",        "adherents_m":    0.04,"founded": "1875",     "origin": "USA",                   "summary": "Blavatsky's syncretic esoteric movement; root of much later New Age thought."},
    {"name": "Anthroposophy",                             "family": "NRM",        "adherents_m":    0.05,"founded": "1912",     "origin": "Austria",               "summary": "Rudolf Steiner's esoteric Christian system; sponsors Waldorf schools and biodynamic farming."},
    {"name": "Eckankar",                                  "family": "NRM",        "adherents_m":    0.05,"founded": "1965",     "origin": "USA",                   "summary": "Soul-travel movement of Paul Twitchell."},
    {"name": "Unitarian Universalism",                    "family": "NRM",        "adherents_m":    0.8, "founded": "1961",     "origin": "USA",                   "summary": "Liberal non-creedal religious tradition (1961 merger of Unitarian and Universalist denominations)."},
    {"name": "New Thought (Unity, Religious Science)",    "family": "NRM",        "adherents_m":    0.4, "founded": "19th c.",  "origin": "USA",                   "summary": "Mind-cure metaphysical movement; includes Unity, Religious Science, Divine Science."},
    {"name": "Christian Science",                         "family": "NRM",        "adherents_m":    0.1, "founded": "1879",     "origin": "USA",                   "summary": "Mary Baker Eddy's Bible-centric metaphysical religion."},
    {"name": "Swedenborgianism",                          "family": "NRM",        "adherents_m":    0.05,"founded": "1787",     "origin": "Sweden / UK",           "summary": "Emanuel Swedenborg's mystical Christianity (the New Church)."},
    {"name": "Falun Gong",                                "family": "NRM",        "adherents_m":   10.0, "founded": "1992",     "origin": "China",                 "summary": "Qigong-based spiritual movement; banned in PRC since 1999."},
    {"name": "Sahaja Yoga",                               "family": "NRM",        "adherents_m":    0.2, "founded": "1970",     "origin": "India",                 "summary": "Nirmala Srivastava's kundalini-meditation movement."},
    {"name": "Brahma Kumaris",                            "family": "NRM",        "adherents_m":    1.0, "founded": "1936",     "origin": "India / Pakistan",      "summary": "Female-led Raja Yoga movement; UN-accredited NGO."},
    {"name": "Ananda Marga",                              "family": "NRM",        "adherents_m":    1.0, "founded": "1955",     "origin": "India",                 "summary": "P. R. Sarkar's tantric yoga + social-service movement."},
    {"name": "Sant Mat / Radha Soami",                    "family": "NRM",        "adherents_m":    5.0, "founded": "1861",     "origin": "India",                 "summary": "Sound-current (Surat Shabd Yoga) meditation tradition."},
    {"name": "Self-Realization Fellowship",               "family": "NRM",        "adherents_m":    1.0, "founded": "1920",     "origin": "India / USA",           "summary": "Paramahansa Yogananda's Kriya Yoga organisation."},
]


# ─── Religious leaders (25 living office-holders) ────────────────────────────
# Public office-holders of the world's largest religious institutions, plus a
# small selection of charismatic figures whose deaths or successions are
# tracked on prediction markets. Status reflects the most recent verifiable
# public source as of curation; bump when a transition occurs. Dates ISO.
#
# Inclusion criteria: (a) leads an institution with ≥1M adherents OR
# ≥100k members and significant prediction-market activity; (b) office is
# personally held (not collective like Bahá'í UHJ or JW Governing Body).
#
# Sex is needed for the life-table lookup. Where unknown or non-applicable,
# defaults to 'M'.

RELIGIOUS_LEADERS = [
    # ─ Catholic ─
    {"name": "Pope Francis", "given_name": "Jorge Mario Bergoglio",
     "role": "Bishop of Rome, Pope of the Catholic Church", "religion": "Roman Catholic",
     "born": "1936-12-17", "sex": "M", "country": "Vatican City",
     "took_office": "2013-03-13", "predecessor": "Benedict XVI (resigned)",
     "succession": "Conclave of cardinal electors under 80",
     "summary": "266th Pope. First Jesuit and first Latin American pope. Hospitalised Feb–Mar 2025 for double pneumonia."},

    # ─ Eastern Orthodox ─
    {"name": "Bartholomew I", "given_name": "Dimitrios Archondonis",
     "role": "Ecumenical Patriarch of Constantinople", "religion": "Eastern Orthodox",
     "born": "1940-02-29", "sex": "M", "country": "Türkiye",
     "took_office": "1991-11-02", "predecessor": "Demetrios I",
     "succession": "Holy Synod of the Ecumenical Patriarchate",
     "summary": "First-among-equals of the Eastern Orthodox communion. Drove the 2018 Tomos granting autocephaly to Ukraine."},

    {"name": "Patriarch Kirill", "given_name": "Vladimir Gundyayev",
     "role": "Patriarch of Moscow and All Rus'", "religion": "Eastern Orthodox",
     "born": "1946-11-20", "sex": "M", "country": "Russia",
     "took_office": "2009-02-01", "predecessor": "Alexy II",
     "succession": "Local Council of the Russian Orthodox Church",
     "summary": "Head of the Russian Orthodox Church. Sanctioned by UK and Canada for support of the invasion of Ukraine."},

    {"name": "Patriarch Theodoros II", "given_name": "Nikolaos Choreftakis",
     "role": "Patriarch of Alexandria and All Africa", "religion": "Eastern Orthodox",
     "born": "1954-11-25", "sex": "M", "country": "Egypt",
     "took_office": "2004-10-09", "predecessor": "Petros VII",
     "succession": "Holy Synod of the Patriarchate of Alexandria",
     "summary": "Greek Orthodox Patriarch of Alexandria; second-ranking see in Eastern Orthodoxy."},

    {"name": "Patriarch Daniel", "given_name": "Dan Ilie Ciobotea",
     "role": "Patriarch of Romania", "religion": "Eastern Orthodox",
     "born": "1951-07-22", "sex": "M", "country": "Romania",
     "took_office": "2007-09-30", "predecessor": "Teoctist",
     "succession": "Holy Synod of the Romanian Orthodox Church",
     "summary": "Sixth Patriarch of the Romanian Orthodox Church, the largest autocephalous Orthodox church after Russia."},

    # ─ Oriental Orthodox ─
    {"name": "Pope Tawadros II", "given_name": "Wagih Sobhy Baky Soliman",
     "role": "Pope of Alexandria, Patriarch of the Coptic Orthodox Church", "religion": "Oriental Orthodox",
     "born": "1952-11-04", "sex": "M", "country": "Egypt",
     "took_office": "2012-11-18", "predecessor": "Shenouda III",
     "succession": "Coptic Holy Synod + altar lottery (qura)",
     "summary": "118th Pope of Alexandria; leads the Coptic Orthodox Church (~10M)."},

    {"name": "Catholicos Karekin II", "given_name": "Ktrij Nersissian",
     "role": "Catholicos of All Armenians", "religion": "Oriental Orthodox",
     "born": "1951-08-21", "sex": "M", "country": "Armenia",
     "took_office": "1999-11-04", "predecessor": "Karekin I",
     "succession": "National Ecclesiastical Assembly",
     "summary": "Supreme Patriarch of the Armenian Apostolic Church."},

    {"name": "Patriarch Mathias", "given_name": "Teklemariam Asrat",
     "role": "Patriarch of Ethiopia", "religion": "Oriental Orthodox",
     "born": "1941-09-11", "sex": "M", "country": "Ethiopia",
     "took_office": "2013-03-03", "predecessor": "Paulos",
     "succession": "Ethiopian Orthodox Holy Synod",
     "summary": "Sixth Patriarch of the Ethiopian Orthodox Tewahedo Church (~36M)."},

    # ─ Anglican (interregnum) ─
    {"name": "Stephen Cottrell", "given_name": "Stephen Geoffrey Cottrell",
     "role": "Archbishop of York (acting senior bishop)", "religion": "Anglican",
     "born": "1958-08-31", "sex": "M", "country": "United Kingdom",
     "took_office": "2020-07-09", "predecessor": "John Sentamu (as Abp of York)",
     "succession": "Crown Nominations Commission (for Canterbury)",
     "summary": "Senior bishop after Archbishop Justin Welby resigned in November 2024 over the Smyth-abuse review. Canterbury seat vacant pending CNC."},

    # ─ LDS / Mormonism ─
    {"name": "Russell M. Nelson", "given_name": "Russell Marion Nelson",
     "role": "President, The Church of Jesus Christ of Latter-day Saints", "religion": "Latter-day Saints",
     "born": "1924-09-09", "sex": "M", "country": "United States",
     "took_office": "2018-01-14", "predecessor": "Thomas S. Monson",
     "succession": "Senior apostle of the Quorum of the Twelve",
     "summary": "17th President of the LDS Church. Centenarian (b. 1924)."},

    # ─ Sunni Islam ─
    {"name": "Ahmed el-Tayeb", "given_name": "Ahmed Mohamed Ahmed el-Tayeb",
     "role": "Grand Imam of al-Azhar", "religion": "Sunni Islam",
     "born": "1946-01-06", "sex": "M", "country": "Egypt",
     "took_office": "2010-03-19", "predecessor": "Mohamed Sayed Tantawy",
     "succession": "Council of Senior Scholars",
     "summary": "Senior Sunni cleric; leads al-Azhar, the most influential Sunni religious institution."},

    {"name": "Abdul Aziz Al ash-Sheikh", "given_name": "Abdul Aziz ibn Abdullah Al ash-Sheikh",
     "role": "Grand Mufti of Saudi Arabia", "religion": "Sunni Islam",
     "born": "1941-12-03", "sex": "M", "country": "Saudi Arabia",
     "took_office": "1999-05-25", "predecessor": "Abd al-Aziz ibn Baz",
     "succession": "Royal appointment (King of Saudi Arabia)",
     "summary": "Senior religious authority of Saudi Arabia; head of the Council of Senior Scholars."},

    # ─ Twelver Shia Islam ─
    {"name": "Ali Khamenei", "given_name": "Sayyid Ali Hosseini Khamenei",
     "role": "Supreme Leader of Iran", "religion": "Twelver Shia",
     "born": "1939-04-19", "sex": "M", "country": "Iran",
     "took_office": "1989-06-04", "predecessor": "Ruhollah Khomeini",
     "succession": "Assembly of Experts",
     "summary": "Head of state of the Islamic Republic of Iran since 1989."},

    {"name": "Ali al-Sistani", "given_name": "Sayyid Ali al-Husayni al-Sistani",
     "role": "Marja' (highest-ranking Shia cleric)", "religion": "Twelver Shia",
     "born": "1930-08-04", "sex": "M", "country": "Iraq",
     "took_office": "1992-08-21", "predecessor": "Abu al-Qasim al-Khoei",
     "succession": "Recognition by hawza scholars",
     "summary": "Most senior marja' in the Twelver Shia world. Quietist counterweight to Iran's Khamenei."},

    # ─ Ismaili ─
    {"name": "Aga Khan V", "given_name": "Prince Rahim Aga Khan",
     "role": "49th Imam of the Nizari Ismailis", "religion": "Ismaili",
     "born": "1971-10-12", "sex": "M", "country": "Switzerland / global",
     "took_office": "2025-02-04", "predecessor": "Aga Khan IV (Karim Aga Khan)",
     "succession": "Hereditary (eldest son of preceding Imam)",
     "summary": "Imam of the Nizari Ismailis since the death of his father in February 2025."},

    # ─ Druze ─
    {"name": "Sheikh Moafak Tarif", "given_name": "Moafak Tarif",
     "role": "Spiritual leader of the Druze in Israel", "religion": "Druze",
     "born": "1963-04-09", "sex": "M", "country": "Israel",
     "took_office": "1993-09-01", "predecessor": "Amin Tarif",
     "succession": "Tarif family hereditary line",
     "summary": "Hereditary Druze religious leader in Israel."},

    # ─ Buddhism ─
    {"name": "Dalai Lama (14th)", "given_name": "Tenzin Gyatso",
     "role": "14th Dalai Lama", "religion": "Tibetan Buddhism",
     "born": "1935-07-06", "sex": "M", "country": "India (exile)",
     "took_office": "1940-02-22", "predecessor": "Thubten Gyatso (13th)",
     "succession": "Tulku reincarnation; CCP claims authority over recognition",
     "summary": "Spiritual leader of Tibetan Buddhism in exile since 1959. Has stated the institution may end with him; CCP-China dispute over succession is a likely Polymarket subject."},

    {"name": "17th Karmapa (Ogyen Trinley Dorje)", "given_name": "Ogyen Trinley Dorje",
     "role": "Head of the Karma Kagyu lineage", "religion": "Tibetan Buddhism",
     "born": "1985-06-26", "sex": "M", "country": "India / USA",
     "took_office": "1992-06-27", "predecessor": "16th Karmapa (Rangjung Rigpe Dorje)",
     "succession": "Disputed (rival Karmapa: Trinley Thaye Dorje)",
     "summary": "Head of one of four major Tibetan Buddhist schools. Recognition is contested between two claimants."},

    {"name": "Minoru Harada", "given_name": "Minoru Harada",
     "role": "President, Soka Gakkai", "religion": "Nichiren Buddhism",
     "born": "1941-12-15", "sex": "M", "country": "Japan",
     "took_office": "2006-11-09", "predecessor": "Einosuke Akiya",
     "succession": "Soka Gakkai Board",
     "summary": "Sixth president of Soka Gakkai, the world's largest Nichiren Buddhist lay organisation. Daisaku Ikeda (honorary president) died Nov 2023."},

    # ─ Sikhism ─
    {"name": "Giani Raghbir Singh", "given_name": "Raghbir Singh",
     "role": "Jathedar of the Akal Takht", "religion": "Sikhism",
     "born": "1956-04-01", "sex": "M", "country": "India",
     "took_office": "2023-08-08", "predecessor": "Harpreet Singh",
     "succession": "Shiromani Gurdwara Parbandhak Committee",
     "summary": "Highest temporal seat of Sikh authority; head of the Akal Takht in Amritsar."},

    # ─ Jainism ─
    {"name": "Acharya Mahashraman", "given_name": "Mohan Lal Dugar",
     "role": "11th Acharya, Shvetambara Terapanth", "religion": "Jainism",
     "born": "1962-05-13", "sex": "M", "country": "India",
     "took_office": "2010-05-09", "predecessor": "Acharya Mahapragya",
     "succession": "Predecessor's appointment",
     "summary": "Spiritual head of the Shvetambara Terapanth Jain order."},

    # ─ Judaism (Israel) ─
    {"name": "Yitzhak Yosef", "given_name": "Yitzhak Yosef",
     "role": "Sephardi Chief Rabbi of Israel (Rishon LeZion)", "religion": "Orthodox Judaism",
     "born": "1952-02-16", "sex": "M", "country": "Israel",
     "took_office": "2013-08-14", "predecessor": "Shlomo Amar",
     "succession": "Chief Rabbinate Election Assembly",
     "summary": "Sephardi Chief Rabbi; son of former Chief Rabbi Ovadia Yosef."},

    {"name": "David Lau", "given_name": "David Baruch Lau",
     "role": "Ashkenazi Chief Rabbi of Israel", "religion": "Orthodox Judaism",
     "born": "1966-01-31", "sex": "M", "country": "Israel",
     "took_office": "2013-08-14", "predecessor": "Yona Metzger",
     "succession": "Chief Rabbinate Election Assembly",
     "summary": "Ashkenazi Chief Rabbi of Israel; son of former Chief Rabbi Yisrael Meir Lau."},

    # ─ Bahá'í (note: collective body, but listed for reference) ─
    # (Universal House of Justice — collective; intentionally omitted.)

    # ─ Hindu (decentralised — including a high-profile guru) ─
    {"name": "Mata Amritanandamayi (Amma)", "given_name": "Sudhamani Idamannel",
     "role": "Founder, Mata Amritanandamayi Math", "religion": "Hinduism",
     "born": "1953-09-27", "sex": "F", "country": "India",
     "took_office": "1981-05-06", "predecessor": "—",
     "succession": "Charismatic; no named successor",
     "summary": "Globally followed Hindu spiritual leader (~30M followers via Mata Amritanandamayi Math humanitarian network)."},

    # ─ Assyrian Church of the East ─
    {"name": "Mar Awa III", "given_name": "Awa Royel",
     "role": "Catholicos-Patriarch of the Assyrian Church of the East", "religion": "Church of the East",
     "born": "1975-08-29", "sex": "M", "country": "USA / Iraq",
     "took_office": "2021-09-13", "predecessor": "Gewargis III",
     "succession": "Holy Synod of the Assyrian Church of the East",
     "summary": "122nd Catholicos-Patriarch; first to be elected in the United States."},
]


# ─── Country-level religion composition (top 30 by population) ───────────────
# Pew Research Center "Religious Composition by Country" (2010-2050 series),
# Pew 2020 baseline. Percentages of national population. Rounded.
# Categories follow Pew's mutually-exclusive top-level taxonomy.
#
# These are simplified for cross-tradition comparison; sect-level
# breakdowns within (e.g.) Christian or Muslim live in RELIGIONS_FULL.

COUNTRY_RELIGION = [
    {"country": "India",            "pop_m": 1428, "majority": "Hindu",       "religion_pct": {"Hindu": 79.8, "Muslim": 14.2, "Christian": 2.3, "Sikh": 1.7, "Buddhist": 0.7, "Jain": 0.4, "Other": 0.9}},
    {"country": "China",            "pop_m": 1410, "majority": "Unaffiliated","religion_pct": {"Unaffiliated": 51.8, "Folk": 21.9, "Buddhist": 18.2, "Christian": 5.1, "Muslim": 1.8, "Other": 1.2}},
    {"country": "United States",    "pop_m":  335, "majority": "Christian",   "religion_pct": {"Christian": 63.0, "Unaffiliated": 29.0, "Jewish": 2.0, "Muslim": 1.1, "Buddhist": 1.1, "Hindu": 0.9, "Other": 2.9}},
    {"country": "Indonesia",        "pop_m":  278, "majority": "Muslim",      "religion_pct": {"Muslim": 87.2, "Christian": 9.9, "Hindu": 1.7, "Buddhist": 0.7, "Folk": 0.4, "Other": 0.1}},
    {"country": "Pakistan",         "pop_m":  240, "majority": "Muslim",      "religion_pct": {"Muslim": 96.4, "Hindu": 1.9, "Christian": 1.6, "Other": 0.1}},
    {"country": "Nigeria",          "pop_m":  223, "majority": "Christian",   "religion_pct": {"Christian": 50.0, "Muslim": 47.1, "Folk": 2.0, "Other": 0.9}},
    {"country": "Brazil",           "pop_m":  216, "majority": "Christian",   "religion_pct": {"Christian": 88.9, "Unaffiliated": 7.9, "Folk": 2.0, "Other": 1.2}},
    {"country": "Bangladesh",       "pop_m":  173, "majority": "Muslim",      "religion_pct": {"Muslim": 89.8, "Hindu": 9.1, "Christian": 0.5, "Buddhist": 0.5, "Other": 0.1}},
    {"country": "Russia",           "pop_m":  144, "majority": "Christian",   "religion_pct": {"Christian": 73.3, "Unaffiliated": 16.2, "Muslim": 10.0, "Other": 0.5}},
    {"country": "Mexico",           "pop_m":  128, "majority": "Christian",   "religion_pct": {"Christian": 95.1, "Unaffiliated": 4.7, "Other": 0.2}},
    {"country": "Japan",            "pop_m":  124, "majority": "Buddhist",    "religion_pct": {"Buddhist": 36.2, "Unaffiliated": 57.0, "Folk": 6.0, "Christian": 1.6, "Other": 0.2}},
    {"country": "Ethiopia",         "pop_m":  126, "majority": "Christian",   "religion_pct": {"Christian": 62.8, "Muslim": 33.9, "Folk": 2.6, "Other": 0.7}},
    {"country": "Philippines",      "pop_m":  117, "majority": "Christian",   "religion_pct": {"Christian": 92.6, "Muslim": 5.6, "Folk": 1.5, "Other": 0.3}},
    {"country": "Egypt",            "pop_m":  112, "majority": "Muslim",      "religion_pct": {"Muslim": 94.9, "Christian": 5.1, "Other": 0.1}},
    {"country": "Vietnam",          "pop_m":   99, "majority": "Folk",        "religion_pct": {"Folk": 45.3, "Buddhist": 16.4, "Christian": 8.2, "Unaffiliated": 29.6, "Other": 0.5}},
    {"country": "DR Congo",         "pop_m":  102, "majority": "Christian",   "religion_pct": {"Christian": 95.7, "Muslim": 1.5, "Folk": 2.6, "Other": 0.2}},
    {"country": "Türkiye",          "pop_m":   85, "majority": "Muslim",      "religion_pct": {"Muslim": 98.0, "Unaffiliated": 1.2, "Christian": 0.4, "Other": 0.4}},
    {"country": "Iran",             "pop_m":   89, "majority": "Muslim",      "religion_pct": {"Muslim": 99.5, "Other": 0.5}},
    {"country": "Germany",          "pop_m":   84, "majority": "Christian",   "religion_pct": {"Christian": 67.3, "Unaffiliated": 25.0, "Muslim": 5.8, "Other": 1.9}},
    {"country": "Thailand",         "pop_m":   72, "majority": "Buddhist",    "religion_pct": {"Buddhist": 93.2, "Muslim": 5.5, "Christian": 0.9, "Other": 0.4}},
    {"country": "United Kingdom",   "pop_m":   68, "majority": "Christian",   "religion_pct": {"Christian": 59.5, "Unaffiliated": 27.8, "Muslim": 6.5, "Hindu": 1.7, "Sikh": 0.9, "Jewish": 0.5, "Other": 3.1}},
    {"country": "France",           "pop_m":   65, "majority": "Christian",   "religion_pct": {"Christian": 63.2, "Unaffiliated": 28.0, "Muslim": 7.5, "Jewish": 0.5, "Other": 0.8}},
    {"country": "Italy",            "pop_m":   59, "majority": "Christian",   "religion_pct": {"Christian": 83.3, "Unaffiliated": 12.4, "Muslim": 3.7, "Other": 0.6}},
    {"country": "South Africa",     "pop_m":   60, "majority": "Christian",   "religion_pct": {"Christian": 81.2, "Folk": 11.4, "Unaffiliated": 5.9, "Muslim": 1.7, "Hindu": 1.1, "Other": 0.2}},
    {"country": "Myanmar (Burma)",  "pop_m":   55, "majority": "Buddhist",    "religion_pct": {"Buddhist": 80.1, "Christian": 7.8, "Muslim": 4.3, "Folk": 5.8, "Hindu": 1.7, "Other": 0.3}},
    {"country": "South Korea",      "pop_m":   52, "majority": "Unaffiliated","religion_pct": {"Unaffiliated": 56.9, "Christian": 27.6, "Buddhist": 15.5, "Other": 0.0}},
    {"country": "Spain",            "pop_m":   48, "majority": "Christian",   "religion_pct": {"Christian": 78.6, "Unaffiliated": 19.0, "Muslim": 2.1, "Other": 0.3}},
    {"country": "Saudi Arabia",     "pop_m":   37, "majority": "Muslim",      "religion_pct": {"Muslim": 93.0, "Christian": 4.4, "Hindu": 1.1, "Other": 1.5}},
    {"country": "Israel",           "pop_m":   10, "majority": "Jewish",      "religion_pct": {"Jewish": 76.1, "Muslim": 17.7, "Christian": 2.0, "Druze": 1.6, "Unaffiliated": 2.6, "Other": 0.0}},
    {"country": "Vatican City",     "pop_m":  0.001,"majority": "Christian",  "religion_pct": {"Christian": 100.0}},
]


# ─── Religious calendar (top 30 observances, 2026) ───────────────────────────
# Dates in ISO format. Movable feasts are computed for 2026 specifically;
# update annually. Used to:
#   - flag context for news/markets ("Pope speaks during Holy Week")
#   - render an upcoming-events strip on the dashboard
#
# Where an observance spans multiple days, only the principal day is shown;
# the duration field gives length in days for display.

RELIGIOUS_CALENDAR_2026 = [
    {"date": "2026-01-06", "name": "Epiphany",                       "religion": "Christian",  "duration": 1, "summary": "Manifestation of Christ to the gentiles (Western and Armenian)."},
    {"date": "2026-01-07", "name": "Orthodox Christmas",             "religion": "Christian",  "duration": 1, "summary": "Christmas observed by churches following the Julian calendar."},
    {"date": "2026-02-15", "name": "Maha Shivaratri",                "religion": "Hindu",      "duration": 1, "summary": "Great Night of Shiva."},
    {"date": "2026-02-17", "name": "Lunar New Year",                 "religion": "East Asian", "duration": 7, "summary": "Year of the Fire Horse begins; widely observed in folk + Buddhist practice."},
    {"date": "2026-02-18", "name": "Ash Wednesday",                  "religion": "Christian",  "duration": 1, "summary": "Beginning of Western Christian Lent."},
    {"date": "2026-03-03", "name": "Holi",                           "religion": "Hindu",      "duration": 2, "summary": "Festival of colours marking the arrival of spring."},
    {"date": "2026-03-17", "name": "Ramadan begins",                 "religion": "Islamic",    "duration": 30, "summary": "Month of fasting from dawn to sunset for Muslims worldwide."},
    {"date": "2026-03-21", "name": "Naw-Rúz",                        "religion": "Iranian",    "duration": 1, "summary": "Bahá'í + Zoroastrian + Persian New Year."},
    {"date": "2026-04-01", "name": "Passover (Pesach) begins",       "religion": "Jewish",     "duration": 7, "summary": "Commemoration of the Exodus from Egypt."},
    {"date": "2026-04-02", "name": "Maundy Thursday",                "religion": "Christian",  "duration": 1, "summary": "Last Supper commemoration; start of the Easter Triduum."},
    {"date": "2026-04-03", "name": "Good Friday",                    "religion": "Christian",  "duration": 1, "summary": "Crucifixion of Christ (Western)."},
    {"date": "2026-04-05", "name": "Easter (Western)",               "religion": "Christian",  "duration": 1, "summary": "Resurrection of Christ — most important Christian feast."},
    {"date": "2026-04-12", "name": "Easter (Orthodox)",              "religion": "Christian",  "duration": 1, "summary": "Pascha — Eastern Orthodox Easter."},
    {"date": "2026-04-14", "name": "Vaisakhi",                       "religion": "Sikh",       "duration": 1, "summary": "Punjabi spring harvest; founding of the Khalsa (1699)."},
    {"date": "2026-04-15", "name": "Yom HaShoah",                    "religion": "Jewish",     "duration": 1, "summary": "Holocaust Remembrance Day in Israel."},
    {"date": "2026-04-17", "name": "Eid al-Fitr",                    "religion": "Islamic",    "duration": 3, "summary": "Festival concluding Ramadan."},
    {"date": "2026-04-21", "name": "Ridván",                         "religion": "Iranian",    "duration": 12, "summary": "Most holy Bahá'í festival; commemorates Bahá'u'lláh's 1863 declaration."},
    {"date": "2026-05-01", "name": "Vesak (Buddha Day)",             "religion": "Buddhist",   "duration": 1, "summary": "Birth, enlightenment and parinirvana of the Buddha (Theravada calendar)."},
    {"date": "2026-05-24", "name": "Pentecost (Western)",            "religion": "Christian",  "duration": 1, "summary": "Descent of the Holy Spirit; 50 days after Easter."},
    {"date": "2026-05-26", "name": "Eid al-Adha",                    "religion": "Islamic",    "duration": 4, "summary": "Festival of the Sacrifice; concludes the Hajj."},
    {"date": "2026-05-26", "name": "Hajj",                           "religion": "Islamic",    "duration": 5, "summary": "Annual Muslim pilgrimage to Mecca; obligatory once for those able."},
    {"date": "2026-07-15", "name": "Ashura",                         "religion": "Islamic",    "duration": 1, "summary": "10th of Muharram; commemoration of Imam Husayn's martyrdom (Shia)."},
    {"date": "2026-08-25", "name": "Mawlid an-Nabi",                 "religion": "Islamic",    "duration": 1, "summary": "Prophet Muhammad's birthday."},
    {"date": "2026-09-12", "name": "Rosh Hashanah",                  "religion": "Jewish",     "duration": 2, "summary": "Jewish New Year; start of the High Holy Days."},
    {"date": "2026-09-21", "name": "Yom Kippur",                     "religion": "Jewish",     "duration": 1, "summary": "Day of Atonement — holiest day in Judaism."},
    {"date": "2026-10-20", "name": "Dussehra (Vijayadashami)",       "religion": "Hindu",      "duration": 1, "summary": "Victory of good over evil; Rama over Ravana."},
    {"date": "2026-11-08", "name": "Diwali",                         "religion": "Hindu",      "duration": 5, "summary": "Festival of lights; widely observed across Hindu, Sikh, Jain traditions."},
    {"date": "2026-11-24", "name": "Guru Nanak Jayanti",             "religion": "Sikh",       "duration": 1, "summary": "Birth anniversary of the founder of Sikhism."},
    {"date": "2026-12-04", "name": "Hanukkah begins",                "religion": "Jewish",     "duration": 8, "summary": "Festival of Lights; rededication of the Second Temple."},
    {"date": "2026-12-25", "name": "Christmas",                      "religion": "Christian",  "duration": 1, "summary": "Birth of Jesus (Western and most Eastern churches)."},
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
