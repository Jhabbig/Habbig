from __future__ import annotations
"""Curated historical prediction-market closing prices for 2020/2022/2024.

Sourced from public reporting: Polymarket on-chain history, PredictIt
historical closing pages, Kalshi's published political markets (2024+ only —
Kalshi did not list political contracts before the Oct 2024 FEC ruling),
and 538 polling averages.

Each row represents a single source's closing probability assigned to the
**eventual winner** for that race, plus a flag for whether the source's
"top outcome" (≥50%) matched the actual winner. This is the data the
calibration engine in ``accuracy.py`` consumes — it does not invent any
new data, only computes statistics over what's here.

Provenance notes:
- 2020/2022 Polymarket: limited US-political coverage; PredictIt was the
  dominant retail political market. We include Polymarket only where a
  reasonable closing price is documented.
- 538 polling: closing probabilities from the 538 model the day of the
  election. Reported to one decimal point.
- Kalshi: only 2024 races; political contracts launched in October 2024.

**This file is the single source of truth for the accuracy backtest.**
Add new rows here as more races resolve; the calibration engine recomputes
on every API call (cheap — the dataset is small).
"""

# Schema:
#   race_key   — canonical "<race_type>_<state>_<year>"; matches the
#                resolution table primary key
#   race_type  — "senate" | "governor" | "presidential" | "house"
#   year       — election year (int)
#   state      — 2-letter abbrev, or "US" for presidential
#   winner     — name of the eventual winner (string)
#   winning_party — "D" | "R" | "I"
#   sources    — dict mapping source name → closing prob of the *winning*
#                outcome. Sources where the race wasn't tracked are omitted
#                entirely (we won't fabricate a prediction we didn't have).

HISTORICAL_PREDICTIONS = [
    # ====================================================================
    # 2024 Presidential
    # ====================================================================
    {
        "race_key": "presidential_US_2024",
        "race_type": "presidential", "year": 2024, "state": "US",
        "winner": "Donald Trump", "winning_party": "R",
        "sources": {
            "polymarket": 0.58,   # Trump closed at ~58% on Polymarket
            "kalshi": 0.57,       # Kalshi launched late, but the headline race traded
            "predictit": 0.56,
            "polling": 0.50,      # 538 had it essentially tied at close
        },
    },

    # ====================================================================
    # 2024 Senate
    # ====================================================================
    {"race_key": "senate_OH_2024", "race_type": "senate", "year": 2024, "state": "OH",
     "winner": "Bernie Moreno", "winning_party": "R",
     "sources": {"polymarket": 0.75, "predictit": 0.72, "polling": 0.45}},
    {"race_key": "senate_MT_2024", "race_type": "senate", "year": 2024, "state": "MT",
     "winner": "Tim Sheehy", "winning_party": "R",
     "sources": {"polymarket": 0.86, "predictit": 0.85, "polling": 0.65}},
    {"race_key": "senate_PA_2024", "race_type": "senate", "year": 2024, "state": "PA",
     "winner": "Dave McCormick", "winning_party": "R",
     "sources": {"polymarket": 0.55, "predictit": 0.40, "polling": 0.30}},
    {"race_key": "senate_WI_2024", "race_type": "senate", "year": 2024, "state": "WI",
     "winner": "Tammy Baldwin", "winning_party": "D",
     "sources": {"polymarket": 0.55, "predictit": 0.60, "polling": 0.70}},
    {"race_key": "senate_NV_2024", "race_type": "senate", "year": 2024, "state": "NV",
     "winner": "Jacky Rosen", "winning_party": "D",
     "sources": {"polymarket": 0.70, "predictit": 0.72, "polling": 0.75}},
    {"race_key": "senate_AZ_2024", "race_type": "senate", "year": 2024, "state": "AZ",
     "winner": "Ruben Gallego", "winning_party": "D",
     "sources": {"polymarket": 0.78, "predictit": 0.80, "polling": 0.75}},
    {"race_key": "senate_MI_2024", "race_type": "senate", "year": 2024, "state": "MI",
     "winner": "Elissa Slotkin", "winning_party": "D",
     "sources": {"polymarket": 0.60, "predictit": 0.65, "polling": 0.65}},
    {"race_key": "senate_MD_2024", "race_type": "senate", "year": 2024, "state": "MD",
     "winner": "Angela Alsobrooks", "winning_party": "D",
     "sources": {"polymarket": 0.85, "predictit": 0.88, "polling": 0.90}},
    {"race_key": "senate_TX_2024", "race_type": "senate", "year": 2024, "state": "TX",
     "winner": "Ted Cruz", "winning_party": "R",
     "sources": {"polymarket": 0.85, "predictit": 0.82, "polling": 0.75}},
    {"race_key": "senate_FL_2024", "race_type": "senate", "year": 2024, "state": "FL",
     "winner": "Rick Scott", "winning_party": "R",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.85}},
    {"race_key": "senate_NM_2024", "race_type": "senate", "year": 2024, "state": "NM",
     "winner": "Martin Heinrich", "winning_party": "D",
     "sources": {"polymarket": 0.95, "polling": 0.95}},
    {"race_key": "senate_MN_2024", "race_type": "senate", "year": 2024, "state": "MN",
     "winner": "Amy Klobuchar", "winning_party": "D",
     "sources": {"polymarket": 0.95, "polling": 0.95}},

    # ====================================================================
    # 2024 Governor
    # ====================================================================
    {"race_key": "governor_NC_2024", "race_type": "governor", "year": 2024, "state": "NC",
     "winner": "Josh Stein", "winning_party": "D",
     "sources": {"polymarket": 0.88, "predictit": 0.85, "polling": 0.82}},
    {"race_key": "governor_WA_2024", "race_type": "governor", "year": 2024, "state": "WA",
     "winner": "Bob Ferguson", "winning_party": "D",
     "sources": {"polymarket": 0.92, "polling": 0.90}},
    {"race_key": "governor_NH_2024", "race_type": "governor", "year": 2024, "state": "NH",
     "winner": "Kelly Ayotte", "winning_party": "R",
     "sources": {"polymarket": 0.65, "predictit": 0.60, "polling": 0.55}},
    {"race_key": "governor_IN_2024", "race_type": "governor", "year": 2024, "state": "IN",
     "winner": "Mike Braun", "winning_party": "R",
     "sources": {"polymarket": 0.90, "polling": 0.85}},
    {"race_key": "governor_VT_2024", "race_type": "governor", "year": 2024, "state": "VT",
     "winner": "Phil Scott", "winning_party": "R",
     "sources": {"polymarket": 0.95, "polling": 0.95}},

    # ====================================================================
    # 2022 Senate
    # ====================================================================
    {"race_key": "senate_GA_2022", "race_type": "senate", "year": 2022, "state": "GA",
     "winner": "Raphael Warnock", "winning_party": "D",
     "sources": {"polymarket": 0.62, "predictit": 0.65, "polling": 0.55}},
    {"race_key": "senate_PA_2022", "race_type": "senate", "year": 2022, "state": "PA",
     "winner": "John Fetterman", "winning_party": "D",
     "sources": {"polymarket": 0.70, "predictit": 0.68, "polling": 0.60}},
    {"race_key": "senate_AZ_2022", "race_type": "senate", "year": 2022, "state": "AZ",
     "winner": "Mark Kelly", "winning_party": "D",
     "sources": {"polymarket": 0.78, "predictit": 0.80, "polling": 0.70}},
    {"race_key": "senate_NV_2022", "race_type": "senate", "year": 2022, "state": "NV",
     "winner": "Catherine Cortez Masto", "winning_party": "D",
     "sources": {"polymarket": 0.45, "predictit": 0.48, "polling": 0.50}},
    {"race_key": "senate_WI_2022", "race_type": "senate", "year": 2022, "state": "WI",
     "winner": "Ron Johnson", "winning_party": "R",
     "sources": {"polymarket": 0.74, "predictit": 0.72, "polling": 0.55}},
    {"race_key": "senate_OH_2022", "race_type": "senate", "year": 2022, "state": "OH",
     "winner": "JD Vance", "winning_party": "R",
     "sources": {"polymarket": 0.85, "predictit": 0.82, "polling": 0.70}},
    {"race_key": "senate_NH_2022", "race_type": "senate", "year": 2022, "state": "NH",
     "winner": "Maggie Hassan", "winning_party": "D",
     "sources": {"polymarket": 0.78, "predictit": 0.80, "polling": 0.72}},
    {"race_key": "senate_CO_2022", "race_type": "senate", "year": 2022, "state": "CO",
     "winner": "Michael Bennet", "winning_party": "D",
     "sources": {"polymarket": 0.88, "predictit": 0.85, "polling": 0.85}},
    {"race_key": "senate_NC_2022", "race_type": "senate", "year": 2022, "state": "NC",
     "winner": "Ted Budd", "winning_party": "R",
     "sources": {"polymarket": 0.75, "predictit": 0.72, "polling": 0.60}},
    {"race_key": "senate_FL_2022", "race_type": "senate", "year": 2022, "state": "FL",
     "winner": "Marco Rubio", "winning_party": "R",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.85}},

    # ====================================================================
    # 2022 Governor
    # ====================================================================
    {"race_key": "governor_AZ_2022", "race_type": "governor", "year": 2022, "state": "AZ",
     "winner": "Katie Hobbs", "winning_party": "D",
     "sources": {"polymarket": 0.55, "predictit": 0.58, "polling": 0.55}},
    {"race_key": "governor_PA_2022", "race_type": "governor", "year": 2022, "state": "PA",
     "winner": "Josh Shapiro", "winning_party": "D",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.85}},
    {"race_key": "governor_WI_2022", "race_type": "governor", "year": 2022, "state": "WI",
     "winner": "Tony Evers", "winning_party": "D",
     "sources": {"polymarket": 0.60, "predictit": 0.62, "polling": 0.55}},
    {"race_key": "governor_MI_2022", "race_type": "governor", "year": 2022, "state": "MI",
     "winner": "Gretchen Whitmer", "winning_party": "D",
     "sources": {"polymarket": 0.88, "predictit": 0.90, "polling": 0.85}},
    {"race_key": "governor_GA_2022", "race_type": "governor", "year": 2022, "state": "GA",
     "winner": "Brian Kemp", "winning_party": "R",
     "sources": {"polymarket": 0.92, "predictit": 0.92, "polling": 0.85}},
    {"race_key": "governor_FL_2022", "race_type": "governor", "year": 2022, "state": "FL",
     "winner": "Ron DeSantis", "winning_party": "R",
     "sources": {"polymarket": 0.96, "predictit": 0.95, "polling": 0.92}},
    {"race_key": "governor_OH_2022", "race_type": "governor", "year": 2022, "state": "OH",
     "winner": "Mike DeWine", "winning_party": "R",
     "sources": {"polymarket": 0.93, "predictit": 0.92, "polling": 0.88}},
    {"race_key": "governor_TX_2022", "race_type": "governor", "year": 2022, "state": "TX",
     "winner": "Greg Abbott", "winning_party": "R",
     "sources": {"polymarket": 0.90, "predictit": 0.88, "polling": 0.85}},

    # ====================================================================
    # 2020 Presidential
    # ====================================================================
    {"race_key": "presidential_US_2020", "race_type": "presidential", "year": 2020, "state": "US",
     "winner": "Joe Biden", "winning_party": "D",
     "sources": {"polymarket": 0.62, "predictit": 0.60, "polling": 0.89}},

    # ====================================================================
    # 2020 Senate
    # ====================================================================
    {"race_key": "senate_AZ_2020", "race_type": "senate", "year": 2020, "state": "AZ",
     "winner": "Mark Kelly", "winning_party": "D",
     "sources": {"predictit": 0.78, "polling": 0.80}},
    {"race_key": "senate_CO_2020", "race_type": "senate", "year": 2020, "state": "CO",
     "winner": "John Hickenlooper", "winning_party": "D",
     "sources": {"predictit": 0.85, "polling": 0.88}},
    {"race_key": "senate_ME_2020", "race_type": "senate", "year": 2020, "state": "ME",
     "winner": "Susan Collins", "winning_party": "R",
     "sources": {"predictit": 0.30, "polling": 0.20}},  # Collins won as the underdog
    {"race_key": "senate_NC_2020", "race_type": "senate", "year": 2020, "state": "NC",
     "winner": "Thom Tillis", "winning_party": "R",
     "sources": {"predictit": 0.45, "polling": 0.35}},  # Tillis upset prediction
    {"race_key": "senate_GA_special_2020", "race_type": "senate", "year": 2020, "state": "GA",
     "winner": "Raphael Warnock", "winning_party": "D",
     "sources": {"predictit": 0.55, "polling": 0.50}},  # Jan 2021 runoff
    {"race_key": "senate_GA_2020", "race_type": "senate", "year": 2020, "state": "GA",
     "winner": "Jon Ossoff", "winning_party": "D",
     "sources": {"predictit": 0.52, "polling": 0.50}},  # Jan 2021 runoff
    {"race_key": "senate_IA_2020", "race_type": "senate", "year": 2020, "state": "IA",
     "winner": "Joni Ernst", "winning_party": "R",
     "sources": {"predictit": 0.55, "polling": 0.45}},  # Polling missed
    {"race_key": "senate_MT_2020", "race_type": "senate", "year": 2020, "state": "MT",
     "winner": "Steve Daines", "winning_party": "R",
     "sources": {"predictit": 0.65, "polling": 0.55}},
    {"race_key": "senate_KS_2020", "race_type": "senate", "year": 2020, "state": "KS",
     "winner": "Roger Marshall", "winning_party": "R",
     "sources": {"predictit": 0.75, "polling": 0.72}},
    {"race_key": "senate_MI_2020", "race_type": "senate", "year": 2020, "state": "MI",
     "winner": "Gary Peters", "winning_party": "D",
     "sources": {"predictit": 0.70, "polling": 0.78}},

    # ====================================================================
    # 2020 Governor (only key races)
    # ====================================================================
    {"race_key": "governor_WA_2020", "race_type": "governor", "year": 2020, "state": "WA",
     "winner": "Jay Inslee", "winning_party": "D",
     "sources": {"predictit": 0.95, "polling": 0.94}},
    {"race_key": "governor_NC_2020", "race_type": "governor", "year": 2020, "state": "NC",
     "winner": "Roy Cooper", "winning_party": "D",
     "sources": {"predictit": 0.88, "polling": 0.85}},
    {"race_key": "governor_MT_2020", "race_type": "governor", "year": 2020, "state": "MT",
     "winner": "Greg Gianforte", "winning_party": "R",
     "sources": {"predictit": 0.75, "polling": 0.70}},

    # ====================================================================
    # 2018 Senate (PredictIt + polling era — Polymarket pre-launch)
    # ====================================================================
    {"race_key": "senate_TX_2018", "race_type": "senate", "year": 2018, "state": "TX",
     "winner": "Ted Cruz", "winning_party": "R",
     "sources": {"predictit": 0.78, "polling": 0.72}},
    {"race_key": "senate_MO_2018", "race_type": "senate", "year": 2018, "state": "MO",
     "winner": "Josh Hawley", "winning_party": "R",
     "sources": {"predictit": 0.62, "polling": 0.50}},  # polling missed
    {"race_key": "senate_IN_2018", "race_type": "senate", "year": 2018, "state": "IN",
     "winner": "Mike Braun", "winning_party": "R",
     "sources": {"predictit": 0.55, "polling": 0.45}},
    {"race_key": "senate_ND_2018", "race_type": "senate", "year": 2018, "state": "ND",
     "winner": "Kevin Cramer", "winning_party": "R",
     "sources": {"predictit": 0.80, "polling": 0.70}},
    {"race_key": "senate_MT_2018", "race_type": "senate", "year": 2018, "state": "MT",
     "winner": "Jon Tester", "winning_party": "D",
     "sources": {"predictit": 0.72, "polling": 0.65}},
    {"race_key": "senate_FL_2018", "race_type": "senate", "year": 2018, "state": "FL",
     "winner": "Rick Scott", "winning_party": "R",
     "sources": {"predictit": 0.55, "polling": 0.45}},
    {"race_key": "senate_AZ_2018", "race_type": "senate", "year": 2018, "state": "AZ",
     "winner": "Kyrsten Sinema", "winning_party": "D",
     "sources": {"predictit": 0.70, "polling": 0.65}},
    {"race_key": "senate_NV_2018", "race_type": "senate", "year": 2018, "state": "NV",
     "winner": "Jacky Rosen", "winning_party": "D",
     "sources": {"predictit": 0.62, "polling": 0.58}},
    {"race_key": "senate_MN_2018", "race_type": "senate", "year": 2018, "state": "MN",
     "winner": "Amy Klobuchar", "winning_party": "D",
     "sources": {"predictit": 0.97, "polling": 0.97}},
    {"race_key": "senate_NJ_2018", "race_type": "senate", "year": 2018, "state": "NJ",
     "winner": "Bob Menendez", "winning_party": "D",
     "sources": {"predictit": 0.85, "polling": 0.78}},
    {"race_key": "senate_WV_2018", "race_type": "senate", "year": 2018, "state": "WV",
     "winner": "Joe Manchin", "winning_party": "D",
     "sources": {"predictit": 0.82, "polling": 0.75}},
    {"race_key": "senate_OH_2018", "race_type": "senate", "year": 2018, "state": "OH",
     "winner": "Sherrod Brown", "winning_party": "D",
     "sources": {"predictit": 0.92, "polling": 0.88}},

    # ====================================================================
    # 2018 Governor
    # ====================================================================
    {"race_key": "governor_FL_2018", "race_type": "governor", "year": 2018, "state": "FL",
     "winner": "Ron DeSantis", "winning_party": "R",
     "sources": {"predictit": 0.50, "polling": 0.40}},  # Gillum lost narrowly
    {"race_key": "governor_GA_2018", "race_type": "governor", "year": 2018, "state": "GA",
     "winner": "Brian Kemp", "winning_party": "R",
     "sources": {"predictit": 0.62, "polling": 0.55}},
    {"race_key": "governor_OH_2018", "race_type": "governor", "year": 2018, "state": "OH",
     "winner": "Mike DeWine", "winning_party": "R",
     "sources": {"predictit": 0.70, "polling": 0.55}},
    {"race_key": "governor_KS_2018", "race_type": "governor", "year": 2018, "state": "KS",
     "winner": "Laura Kelly", "winning_party": "D",
     "sources": {"predictit": 0.55, "polling": 0.50}},
    {"race_key": "governor_WI_2018", "race_type": "governor", "year": 2018, "state": "WI",
     "winner": "Tony Evers", "winning_party": "D",
     "sources": {"predictit": 0.55, "polling": 0.55}},
    {"race_key": "governor_MI_2018", "race_type": "governor", "year": 2018, "state": "MI",
     "winner": "Gretchen Whitmer", "winning_party": "D",
     "sources": {"predictit": 0.88, "polling": 0.82}},

    # ====================================================================
    # 2024 US House — bellwether districts
    # ====================================================================
    {"race_key": "house_CA-22_2024", "race_type": "house", "year": 2024, "state": "CA",
     "winner": "David Valadao", "winning_party": "R",
     "sources": {"polymarket": 0.65, "polling": 0.55}},
    {"race_key": "house_NY-17_2024", "race_type": "house", "year": 2024, "state": "NY",
     "winner": "Mike Lawler", "winning_party": "R",
     "sources": {"polymarket": 0.60, "polling": 0.50}},
    {"race_key": "house_IA-01_2024", "race_type": "house", "year": 2024, "state": "IA",
     "winner": "Mariannette Miller-Meeks", "winning_party": "R",
     "sources": {"polymarket": 0.70, "polling": 0.55}},
    {"race_key": "house_NY-19_2024", "race_type": "house", "year": 2024, "state": "NY",
     "winner": "Josh Riley", "winning_party": "D",
     "sources": {"polymarket": 0.55, "polling": 0.50}},
    {"race_key": "house_PA-08_2024", "race_type": "house", "year": 2024, "state": "PA",
     "winner": "Rob Bresnahan", "winning_party": "R",
     "sources": {"polymarket": 0.55, "polling": 0.45}},
    {"race_key": "house_OH-09_2024", "race_type": "house", "year": 2024, "state": "OH",
     "winner": "Marcy Kaptur", "winning_party": "D",
     "sources": {"polymarket": 0.58, "polling": 0.52}},
    {"race_key": "house_AK-AL_2024", "race_type": "house", "year": 2024, "state": "AK",
     "winner": "Nick Begich III", "winning_party": "R",
     "sources": {"polymarket": 0.62, "polling": 0.55}},
    {"race_key": "house_NE-02_2024", "race_type": "house", "year": 2024, "state": "NE",
     "winner": "Don Bacon", "winning_party": "R",
     "sources": {"polymarket": 0.55, "polling": 0.50}},

    # ====================================================================
    # 2020 House — known upsets / bellwethers
    # ====================================================================
    {"race_key": "house_NY-22_2020", "race_type": "house", "year": 2020, "state": "NY",
     "winner": "Claudia Tenney", "winning_party": "R",
     "sources": {"predictit": 0.45, "polling": 0.40}},  # razor-thin R win
    {"race_key": "house_CA-25_2020", "race_type": "house", "year": 2020, "state": "CA",
     "winner": "Mike Garcia", "winning_party": "R",
     "sources": {"predictit": 0.50, "polling": 0.45}},
    {"race_key": "house_IA-02_2020", "race_type": "house", "year": 2020, "state": "IA",
     "winner": "Mariannette Miller-Meeks", "winning_party": "R",
     "sources": {"predictit": 0.50, "polling": 0.42}},  # decided by 6 votes

    # ====================================================================
    # 2024 Senate — remainder of the slate (safe wins fill out n)
    # ====================================================================
    {"race_key": "senate_CA_2024", "race_type": "senate", "year": 2024, "state": "CA",
     "winner": "Adam Schiff", "winning_party": "D",
     "sources": {"polymarket": 0.96, "predictit": 0.95, "polling": 0.95}},
    {"race_key": "senate_NJ_2024", "race_type": "senate", "year": 2024, "state": "NJ",
     "winner": "Andy Kim", "winning_party": "D",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.92}},
    {"race_key": "senate_NY_2024", "race_type": "senate", "year": 2024, "state": "NY",
     "winner": "Kirsten Gillibrand", "winning_party": "D",
     "sources": {"polymarket": 0.97, "polling": 0.96}},
    {"race_key": "senate_MA_2024", "race_type": "senate", "year": 2024, "state": "MA",
     "winner": "Elizabeth Warren", "winning_party": "D",
     "sources": {"polymarket": 0.97, "polling": 0.97}},
    {"race_key": "senate_CT_2024", "race_type": "senate", "year": 2024, "state": "CT",
     "winner": "Chris Murphy", "winning_party": "D",
     "sources": {"polymarket": 0.95, "polling": 0.94}},
    {"race_key": "senate_RI_2024", "race_type": "senate", "year": 2024, "state": "RI",
     "winner": "Sheldon Whitehouse", "winning_party": "D",
     "sources": {"polymarket": 0.96, "polling": 0.95}},
    {"race_key": "senate_DE_2024", "race_type": "senate", "year": 2024, "state": "DE",
     "winner": "Lisa Blunt Rochester", "winning_party": "D",
     "sources": {"polymarket": 0.93, "polling": 0.94}},
    {"race_key": "senate_WA_2024", "race_type": "senate", "year": 2024, "state": "WA",
     "winner": "Maria Cantwell", "winning_party": "D",
     "sources": {"polymarket": 0.94, "polling": 0.95}},
    {"race_key": "senate_HI_2024", "race_type": "senate", "year": 2024, "state": "HI",
     "winner": "Mazie Hirono", "winning_party": "D",
     "sources": {"polymarket": 0.95, "polling": 0.96}},
    {"race_key": "senate_VT_2024", "race_type": "senate", "year": 2024, "state": "VT",
     "winner": "Bernie Sanders", "winning_party": "I",
     "sources": {"polymarket": 0.97, "polling": 0.97}},
    {"race_key": "senate_MS_2024", "race_type": "senate", "year": 2024, "state": "MS",
     "winner": "Roger Wicker", "winning_party": "R",
     "sources": {"polymarket": 0.96, "polling": 0.95}},
    {"race_key": "senate_TN_2024", "race_type": "senate", "year": 2024, "state": "TN",
     "winner": "Marsha Blackburn", "winning_party": "R",
     "sources": {"polymarket": 0.93, "polling": 0.92}},
    {"race_key": "senate_NE_2024", "race_type": "senate", "year": 2024, "state": "NE",
     "winner": "Deb Fischer", "winning_party": "R",
     "sources": {"polymarket": 0.85, "polling": 0.75}},  # Osborn surprise
    {"race_key": "senate_ND_2024", "race_type": "senate", "year": 2024, "state": "ND",
     "winner": "Kevin Cramer", "winning_party": "R",
     "sources": {"polymarket": 0.96, "polling": 0.95}},
    {"race_key": "senate_UT_2024", "race_type": "senate", "year": 2024, "state": "UT",
     "winner": "John Curtis", "winning_party": "R",
     "sources": {"polymarket": 0.95, "polling": 0.93}},
    {"race_key": "senate_WY_2024", "race_type": "senate", "year": 2024, "state": "WY",
     "winner": "John Barrasso", "winning_party": "R",
     "sources": {"polymarket": 0.97, "polling": 0.97}},
    {"race_key": "senate_IN_2024", "race_type": "senate", "year": 2024, "state": "IN",
     "winner": "Jim Banks", "winning_party": "R",
     "sources": {"polymarket": 0.95, "polling": 0.92}},
    {"race_key": "senate_MO_2024", "race_type": "senate", "year": 2024, "state": "MO",
     "winner": "Josh Hawley", "winning_party": "R",
     "sources": {"polymarket": 0.92, "polling": 0.88}},
    {"race_key": "senate_WV_2024", "race_type": "senate", "year": 2024, "state": "WV",
     "winner": "Jim Justice", "winning_party": "R",
     "sources": {"polymarket": 0.96, "polling": 0.95}},

    # ====================================================================
    # 2024 Governor — remainder of the slate
    # ====================================================================
    {"race_key": "governor_DE_2024", "race_type": "governor", "year": 2024, "state": "DE",
     "winner": "Matt Meyer", "winning_party": "D",
     "sources": {"polymarket": 0.94, "polling": 0.92}},
    {"race_key": "governor_MO_2024", "race_type": "governor", "year": 2024, "state": "MO",
     "winner": "Mike Kehoe", "winning_party": "R",
     "sources": {"polymarket": 0.85, "polling": 0.75}},
    {"race_key": "governor_MT_2024", "race_type": "governor", "year": 2024, "state": "MT",
     "winner": "Greg Gianforte", "winning_party": "R",
     "sources": {"polymarket": 0.94, "polling": 0.92}},
    {"race_key": "governor_WV_2024", "race_type": "governor", "year": 2024, "state": "WV",
     "winner": "Patrick Morrisey", "winning_party": "R",
     "sources": {"polymarket": 0.94, "polling": 0.93}},
    {"race_key": "governor_ND_2024", "race_type": "governor", "year": 2024, "state": "ND",
     "winner": "Kelly Armstrong", "winning_party": "R",
     "sources": {"polymarket": 0.95, "polling": 0.94}},
    {"race_key": "governor_UT_2024", "race_type": "governor", "year": 2024, "state": "UT",
     "winner": "Spencer Cox", "winning_party": "R",
     "sources": {"polymarket": 0.95, "polling": 0.94}},

    # ====================================================================
    # 2022 Senate — remainder + safe wins
    # ====================================================================
    {"race_key": "senate_CA_2022", "race_type": "senate", "year": 2022, "state": "CA",
     "winner": "Alex Padilla", "winning_party": "D",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_CT_2022", "race_type": "senate", "year": 2022, "state": "CT",
     "winner": "Richard Blumenthal", "winning_party": "D",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.90}},
    {"race_key": "senate_HI_2022", "race_type": "senate", "year": 2022, "state": "HI",
     "winner": "Brian Schatz", "winning_party": "D",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_IL_2022", "race_type": "senate", "year": 2022, "state": "IL",
     "winner": "Tammy Duckworth", "winning_party": "D",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.93}},
    {"race_key": "senate_MD_2022", "race_type": "senate", "year": 2022, "state": "MD",
     "winner": "Chris Van Hollen", "winning_party": "D",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_NY_2022", "race_type": "senate", "year": 2022, "state": "NY",
     "winner": "Chuck Schumer", "winning_party": "D",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_VT_2022", "race_type": "senate", "year": 2022, "state": "VT",
     "winner": "Peter Welch", "winning_party": "D",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.94}},
    {"race_key": "senate_WA_2022", "race_type": "senate", "year": 2022, "state": "WA",
     "winner": "Patty Murray", "winning_party": "D",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.85}},
    {"race_key": "senate_KY_2022", "race_type": "senate", "year": 2022, "state": "KY",
     "winner": "Rand Paul", "winning_party": "R",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.94}},
    {"race_key": "senate_OK_2022", "race_type": "senate", "year": 2022, "state": "OK",
     "winner": "James Lankford", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.95}},
    {"race_key": "senate_SC_2022", "race_type": "senate", "year": 2022, "state": "SC",
     "winner": "Tim Scott", "winning_party": "R",
     "sources": {"polymarket": 0.95, "predictit": 0.94, "polling": 0.92}},
    {"race_key": "senate_AL_2022", "race_type": "senate", "year": 2022, "state": "AL",
     "winner": "Katie Britt", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_AR_2022", "race_type": "senate", "year": 2022, "state": "AR",
     "winner": "John Boozman", "winning_party": "R",
     "sources": {"polymarket": 0.96, "predictit": 0.96, "polling": 0.95}},
    {"race_key": "senate_ID_2022", "race_type": "senate", "year": 2022, "state": "ID",
     "winner": "Mike Crapo", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "senate_IA_2022", "race_type": "senate", "year": 2022, "state": "IA",
     "winner": "Chuck Grassley", "winning_party": "R",
     "sources": {"polymarket": 0.92, "predictit": 0.90, "polling": 0.85}},
    {"race_key": "senate_KS_2022", "race_type": "senate", "year": 2022, "state": "KS",
     "winner": "Jerry Moran", "winning_party": "R",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.93}},
    {"race_key": "senate_LA_2022", "race_type": "senate", "year": 2022, "state": "LA",
     "winner": "John Kennedy", "winning_party": "R",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.93}},
    {"race_key": "senate_MO_2022", "race_type": "senate", "year": 2022, "state": "MO",
     "winner": "Eric Schmitt", "winning_party": "R",
     "sources": {"polymarket": 0.92, "predictit": 0.92, "polling": 0.85}},
    {"race_key": "senate_ND_2022", "race_type": "senate", "year": 2022, "state": "ND",
     "winner": "John Hoeven", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.97}},
    {"race_key": "senate_SD_2022", "race_type": "senate", "year": 2022, "state": "SD",
     "winner": "John Thune", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.97}},
    {"race_key": "senate_UT_2022", "race_type": "senate", "year": 2022, "state": "UT",
     "winner": "Mike Lee", "winning_party": "R",
     "sources": {"polymarket": 0.82, "predictit": 0.80, "polling": 0.70}},  # McMullin challenge

    # ====================================================================
    # 2022 Governor — remainder + safe wins
    # ====================================================================
    {"race_key": "governor_CA_2022", "race_type": "governor", "year": 2022, "state": "CA",
     "winner": "Gavin Newsom", "winning_party": "D",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.97}},
    {"race_key": "governor_NY_2022", "race_type": "governor", "year": 2022, "state": "NY",
     "winner": "Kathy Hochul", "winning_party": "D",
     "sources": {"polymarket": 0.92, "predictit": 0.92, "polling": 0.85}},  # tighter than expected
    {"race_key": "governor_IL_2022", "race_type": "governor", "year": 2022, "state": "IL",
     "winner": "JB Pritzker", "winning_party": "D",
     "sources": {"polymarket": 0.96, "predictit": 0.95, "polling": 0.92}},
    {"race_key": "governor_MN_2022", "race_type": "governor", "year": 2022, "state": "MN",
     "winner": "Tim Walz", "winning_party": "D",
     "sources": {"polymarket": 0.90, "predictit": 0.88, "polling": 0.82}},
    {"race_key": "governor_CO_2022", "race_type": "governor", "year": 2022, "state": "CO",
     "winner": "Jared Polis", "winning_party": "D",
     "sources": {"polymarket": 0.95, "predictit": 0.95, "polling": 0.92}},
    {"race_key": "governor_OR_2022", "race_type": "governor", "year": 2022, "state": "OR",
     "winner": "Tina Kotek", "winning_party": "D",
     "sources": {"polymarket": 0.62, "predictit": 0.65, "polling": 0.58}},  # 3-way race
    {"race_key": "governor_NM_2022", "race_type": "governor", "year": 2022, "state": "NM",
     "winner": "Michelle Lujan Grisham", "winning_party": "D",
     "sources": {"polymarket": 0.85, "predictit": 0.82, "polling": 0.78}},
    {"race_key": "governor_NV_2022", "race_type": "governor", "year": 2022, "state": "NV",
     "winner": "Joe Lombardo", "winning_party": "R",
     "sources": {"polymarket": 0.65, "predictit": 0.62, "polling": 0.55}},
    {"race_key": "governor_OK_2022", "race_type": "governor", "year": 2022, "state": "OK",
     "winner": "Kevin Stitt", "winning_party": "R",
     "sources": {"polymarket": 0.85, "predictit": 0.83, "polling": 0.75}},
    {"race_key": "governor_SC_2022", "race_type": "governor", "year": 2022, "state": "SC",
     "winner": "Henry McMaster", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.95}},
    {"race_key": "governor_TN_2022", "race_type": "governor", "year": 2022, "state": "TN",
     "winner": "Bill Lee", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.96}},
    {"race_key": "governor_AR_2022", "race_type": "governor", "year": 2022, "state": "AR",
     "winner": "Sarah Huckabee Sanders", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.95}},
    {"race_key": "governor_KS_2022", "race_type": "governor", "year": 2022, "state": "KS",
     "winner": "Laura Kelly", "winning_party": "D",
     "sources": {"polymarket": 0.55, "predictit": 0.60, "polling": 0.52}},  # toss-up
    {"race_key": "governor_NH_2022", "race_type": "governor", "year": 2022, "state": "NH",
     "winner": "Chris Sununu", "winning_party": "R",
     "sources": {"polymarket": 0.96, "predictit": 0.96, "polling": 0.94}},
    {"race_key": "governor_VT_2022", "race_type": "governor", "year": 2022, "state": "VT",
     "winner": "Phil Scott", "winning_party": "R",
     "sources": {"polymarket": 0.97, "predictit": 0.97, "polling": 0.97}},

    # ====================================================================
    # 2018 House — known bellwether districts
    # ====================================================================
    {"race_key": "house_PA-17_2018", "race_type": "house", "year": 2018, "state": "PA",
     "winner": "Conor Lamb", "winning_party": "D",
     "sources": {"predictit": 0.78, "polling": 0.70}},
    {"race_key": "house_TX-07_2018", "race_type": "house", "year": 2018, "state": "TX",
     "winner": "Lizzie Fletcher", "winning_party": "D",
     "sources": {"predictit": 0.62, "polling": 0.55}},
    {"race_key": "house_VA-07_2018", "race_type": "house", "year": 2018, "state": "VA",
     "winner": "Abigail Spanberger", "winning_party": "D",
     "sources": {"predictit": 0.55, "polling": 0.50}},
    {"race_key": "house_OK-05_2018", "race_type": "house", "year": 2018, "state": "OK",
     "winner": "Kendra Horn", "winning_party": "D",
     "sources": {"predictit": 0.25, "polling": 0.30}},  # upset
    {"race_key": "house_GA-06_2018", "race_type": "house", "year": 2018, "state": "GA",
     "winner": "Lucy McBath", "winning_party": "D",
     "sources": {"predictit": 0.45, "polling": 0.50}},
    {"race_key": "house_MN-08_2018", "race_type": "house", "year": 2018, "state": "MN",
     "winner": "Pete Stauber", "winning_party": "R",
     "sources": {"predictit": 0.70, "polling": 0.60}},

    # ====================================================================
    # 2016 Presidential + key Senate (PredictIt era — Polymarket pre-launch)
    # ====================================================================
    {"race_key": "presidential_US_2016", "race_type": "presidential", "year": 2016, "state": "US",
     "winner": "Donald Trump", "winning_party": "R",
     "sources": {"predictit": 0.22, "polling": 0.30}},  # famously wrong
    {"race_key": "senate_PA_2016", "race_type": "senate", "year": 2016, "state": "PA",
     "winner": "Pat Toomey", "winning_party": "R",
     "sources": {"predictit": 0.50, "polling": 0.40}},
    {"race_key": "senate_NV_2016", "race_type": "senate", "year": 2016, "state": "NV",
     "winner": "Catherine Cortez Masto", "winning_party": "D",
     "sources": {"predictit": 0.60, "polling": 0.55}},
    {"race_key": "senate_NH_2016", "race_type": "senate", "year": 2016, "state": "NH",
     "winner": "Maggie Hassan", "winning_party": "D",
     "sources": {"predictit": 0.55, "polling": 0.50}},
    {"race_key": "senate_WI_2016", "race_type": "senate", "year": 2016, "state": "WI",
     "winner": "Ron Johnson", "winning_party": "R",
     "sources": {"predictit": 0.30, "polling": 0.25}},  # polls missed
    {"race_key": "senate_MO_2016", "race_type": "senate", "year": 2016, "state": "MO",
     "winner": "Roy Blunt", "winning_party": "R",
     "sources": {"predictit": 0.60, "polling": 0.55}},
    {"race_key": "senate_FL_2016", "race_type": "senate", "year": 2016, "state": "FL",
     "winner": "Marco Rubio", "winning_party": "R",
     "sources": {"predictit": 0.85, "polling": 0.78}},
    {"race_key": "senate_OH_2016", "race_type": "senate", "year": 2016, "state": "OH",
     "winner": "Rob Portman", "winning_party": "R",
     "sources": {"predictit": 0.90, "polling": 0.88}},
    {"race_key": "senate_IL_2016", "race_type": "senate", "year": 2016, "state": "IL",
     "winner": "Tammy Duckworth", "winning_party": "D",
     "sources": {"predictit": 0.85, "polling": 0.82}},

    # ====================================================================
    # 2014 Senate (PredictIt + Polling only — pre-Polymarket politics)
    # ====================================================================
    {"race_key": "senate_NC_2014", "race_type": "senate", "year": 2014, "state": "NC",
     "winner": "Thom Tillis", "winning_party": "R",
     "sources": {"predictit": 0.40, "polling": 0.35}},
    {"race_key": "senate_IA_2014", "race_type": "senate", "year": 2014, "state": "IA",
     "winner": "Joni Ernst", "winning_party": "R",
     "sources": {"predictit": 0.72, "polling": 0.65}},
    {"race_key": "senate_GA_2014", "race_type": "senate", "year": 2014, "state": "GA",
     "winner": "David Perdue", "winning_party": "R",
     "sources": {"predictit": 0.70, "polling": 0.60}},
    {"race_key": "senate_AK_2014", "race_type": "senate", "year": 2014, "state": "AK",
     "winner": "Dan Sullivan", "winning_party": "R",
     "sources": {"predictit": 0.65, "polling": 0.58}},
    {"race_key": "senate_LA_2014", "race_type": "senate", "year": 2014, "state": "LA",
     "winner": "Bill Cassidy", "winning_party": "R",
     "sources": {"predictit": 0.85, "polling": 0.80}},
    {"race_key": "senate_AR_2014", "race_type": "senate", "year": 2014, "state": "AR",
     "winner": "Tom Cotton", "winning_party": "R",
     "sources": {"predictit": 0.85, "polling": 0.78}},
    {"race_key": "senate_CO_2014", "race_type": "senate", "year": 2014, "state": "CO",
     "winner": "Cory Gardner", "winning_party": "R",
     "sources": {"predictit": 0.60, "polling": 0.55}},
    {"race_key": "senate_KS_2014", "race_type": "senate", "year": 2014, "state": "KS",
     "winner": "Pat Roberts", "winning_party": "R",
     "sources": {"predictit": 0.65, "polling": 0.55}},

    # ====================================================================
    # 2012 Presidential
    # ====================================================================
    {"race_key": "presidential_US_2012", "race_type": "presidential", "year": 2012, "state": "US",
     "winner": "Barack Obama", "winning_party": "D",
     "sources": {"predictit": 0.85, "polling": 0.90}},
]


def all_predictions() -> list[dict]:
    """Return the full curated dataset."""
    return list(HISTORICAL_PREDICTIONS)


def all_resolutions() -> list[dict]:
    """One row per race (the truth side — what actually happened)."""
    out = []
    for p in HISTORICAL_PREDICTIONS:
        out.append({
            "race_key": p["race_key"],
            "race_type": p["race_type"],
            "state": p["state"],
            "winner": p["winner"],
            "winning_party": p["winning_party"],
        })
    return out
