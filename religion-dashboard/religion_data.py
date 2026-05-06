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
