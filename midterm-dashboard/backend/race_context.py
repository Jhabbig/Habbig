from __future__ import annotations
"""Race context data: candidate policies, state referendums, and key issues.

Provides background context for prediction markets so users can understand
WHY odds are moving and make pattern-based judgments.

2026 midterm cycle: Class II Senate seats (elected 2020) + governor races.
"""

# Each entry keyed by "{race_type}_{state}" matching the race_key format.
# Fields:
#   incumbents: list of current officeholder(s)
#   candidates: known/likely candidates with policy positions
#   referendums: state ballot measures expected or confirmed for 2026
#   key_issues: top issues driving the race
#   lean: Cook/Sabato rating (Safe R, Likely R, Lean R, Toss-up, Lean D, Likely D, Safe D)
#   context: brief narrative

RACE_CONTEXT = {
    # ===================================================================
    # 2026 SENATE — KEY RACES (Class II seats)
    # ===================================================================
    "senate_GA": {
        "incumbents": [{"name": "Jon Ossoff", "party": "D", "since": 2021}],
        "candidates": [
            {"name": "Jon Ossoff", "party": "D", "status": "incumbent",
             "policies": ["Bipartisan infrastructure support", "Voting rights expansion", "Medicare drug price negotiation", "Tech antitrust regulation", "Criminal justice reform"]},
        ],
        "referendums": [],
        "key_issues": ["Economy and inflation", "Voting rights", "Immigration", "Healthcare costs"],
        "lean": "Lean D",
        "context": "Ossoff won by 1.2% in the 2021 runoff. Georgia has trended purple — Biden won it in 2020 but Trump carried it in 2024. Suburban Atlanta turnout is the key variable.",
        "public_opinion": {"approval": "Mixed — Ossoff polls around 47% approve, 44% disapprove", "top_concern": "Economy/cost of living is #1 issue for GA voters"}
    },
    "senate_AZ": {
        "incumbents": [{"name": "Mark Kelly", "party": "D", "since": 2020}],
        "candidates": [
            {"name": "Mark Kelly", "party": "D", "status": "incumbent",
             "policies": ["Border security funding", "Bipartisan gun safety measures", "Veterans affairs", "Space/tech industry investment", "Water rights for Colorado River"]},
        ],
        "referendums": [
            {"title": "Proposition 314 follow-up", "topic": "Immigration enforcement", "description": "Potential ballot measure on state-level immigration enforcement powers"}
        ],
        "key_issues": ["Border security and immigration", "Water scarcity", "Housing affordability", "Cost of living"],
        "lean": "Toss-up",
        "context": "Kelly won by 5pts in 2022 but Arizona swung right in 2024. Border issues dominate. Kelly's bipartisan brand vs. nationalized immigration politics.",
        "public_opinion": {"approval": "Kelly approval ~49%. Border security is the #1 issue.", "top_concern": "Immigration/border is dominant, followed by housing costs"}
    },
    "senate_CO": {
        "incumbents": [{"name": "John Hickenlooper", "party": "D", "since": 2021}],
        "candidates": [
            {"name": "John Hickenlooper", "party": "D", "status": "incumbent",
             "policies": ["Clean energy transition", "Public lands conservation", "Bipartisan tech regulation", "Affordable housing", "Gun violence prevention"]},
        ],
        "referendums": [],
        "key_issues": ["Housing affordability", "Wildfire preparedness", "Water rights", "Cost of living in Denver metro"],
        "lean": "Likely D",
        "context": "Colorado has trended solidly blue. Hickenlooper won by 9pts in 2020. Main question is margin, not outcome.",
        "public_opinion": {"approval": "Moderate approval. Housing costs are the top local issue.", "top_concern": "Housing affordability, especially along the Front Range"}
    },
    "senate_NH": {
        "incumbents": [{"name": "Jeanne Shaheen", "party": "D", "since": 2009}],
        "candidates": [
            {"name": "Jeanne Shaheen", "party": "D", "status": "incumbent (may retire)",
             "policies": ["Healthcare access", "Opioid crisis funding", "Small business support", "Climate resilience", "Veterans care"]},
        ],
        "referendums": [],
        "key_issues": ["Healthcare and opioid crisis", "Housing shortage", "Property taxes", "Education funding"],
        "lean": "Lean D",
        "context": "Shaheen may retire — she'd be 79. Open seat would make this very competitive. NH is a true swing state at the federal level.",
        "public_opinion": {"approval": "Shaheen is well-liked (~52% approve). Housing is the top issue.", "top_concern": "Housing costs and property taxes"}
    },
    "senate_NC": {
        "incumbents": [{"name": "Thom Tillis", "party": "R", "since": 2015}],
        "candidates": [
            {"name": "Thom Tillis", "party": "R", "status": "incumbent",
             "policies": ["Immigration enforcement", "Business tax cuts", "Military/defense spending", "Bipartisan immigration deals (attempted)", "Banking deregulation"]},
        ],
        "referendums": [],
        "key_issues": ["Economy and jobs", "Immigration", "Education (school choice vs public schools)", "Hurricane recovery"],
        "lean": "Lean R",
        "context": "Tillis won by 1.7pts in 2020. NC is very competitive but has trended slightly R. Research Triangle suburban vote is key.",
        "public_opinion": {"approval": "Tillis approval ~44%. Economy is the top issue.", "top_concern": "Economy, followed by education and hurricane recovery funding"}
    },
    "senate_IA": {
        "incumbents": [{"name": "Joni Ernst", "party": "R", "since": 2015}],
        "candidates": [
            {"name": "Joni Ernst", "party": "R", "status": "incumbent",
             "policies": ["Agriculture and ethanol support", "Military veterans", "Government spending cuts", "Anti-regulation", "Second Amendment"]},
        ],
        "referendums": [],
        "key_issues": ["Agriculture and trade policy", "Rural healthcare", "Immigration (meatpacking workforce)", "Property taxes"],
        "lean": "Likely R",
        "context": "Iowa has shifted solidly R at federal level. Ernst won by 7pts in 2020. Would need a major wave for D to compete.",
        "public_opinion": {"approval": "Ernst approval ~46%. Agricultural trade policy matters most here.", "top_concern": "Farm economy and trade, then healthcare access in rural areas"}
    },
    "senate_TX": {
        "incumbents": [{"name": "John Cornyn", "party": "R", "since": 2002}],
        "candidates": [
            {"name": "John Cornyn", "party": "R", "status": "incumbent",
             "policies": ["Border security hardliner", "Energy independence (oil & gas)", "Second Amendment", "Judicial confirmations", "Anti-regulation"]},
        ],
        "referendums": [
            {"title": "Property Tax Reform", "topic": "Taxes", "description": "Continued property tax relief measures following 2023 reforms"}
        ],
        "key_issues": ["Border security", "Energy policy", "Property taxes", "Power grid reliability", "Urban growth"],
        "lean": "Likely R",
        "context": "Cornyn won by 9.6pts in 2020. Texas is slowly trending competitive but not yet. Grid reliability after 2021 freeze remains a vulnerability for R.",
        "public_opinion": {"approval": "Cornyn approval ~43%. Border and energy dominate.", "top_concern": "Border security and immigration, followed by energy/grid reliability"}
    },
    "senate_ME": {
        "incumbents": [{"name": "Susan Collins", "party": "R", "since": 1997}],
        "candidates": [
            {"name": "Susan Collins", "party": "R", "status": "incumbent",
             "policies": ["Bipartisan dealmaking", "Healthcare (ACA protection vote)", "Moderate on social issues", "Defense spending", "Lobster industry protection"]},
        ],
        "referendums": [],
        "key_issues": ["Healthcare", "Fishing/lobster industry", "Opioid crisis", "Affordable housing", "Climate (coastal flooding)"],
        "lean": "Lean R",
        "context": "Collins won by 9pts in 2020 despite Biden carrying ME. Her bipartisan brand is strong locally but weakened nationally. Age (73) could be a factor.",
        "public_opinion": {"approval": "Collins has ~50% approval in-state, higher than national R average.", "top_concern": "Healthcare costs and the fishing economy"}
    },
    "senate_MI": {
        "incumbents": [{"name": "Gary Peters", "party": "D", "since": 2015}],
        "candidates": [
            {"name": "Gary Peters", "party": "D", "status": "incumbent",
             "policies": ["Auto industry support (EV transition)", "Great Lakes protection", "Cybersecurity", "Veterans affairs", "Manufacturing jobs"]},
        ],
        "referendums": [],
        "key_issues": ["Auto industry and EV transition", "Manufacturing jobs", "Great Lakes water quality", "Cost of living"],
        "lean": "Toss-up",
        "context": "Peters won by only 1.7pts in 2020. Michigan swung back to Trump in 2024. Auto/EV policy is the defining issue — workers split on the transition.",
        "public_opinion": {"approval": "Peters ~45% approve. Auto industry/jobs is the top issue.", "top_concern": "Economy and auto industry jobs, especially EV transition impact"}
    },
    "senate_SC": {
        "incumbents": [{"name": "Lindsey Graham", "party": "R", "since": 2003}],
        "candidates": [
            {"name": "Lindsey Graham", "party": "R", "status": "incumbent",
             "policies": ["Defense hawk", "Immigration enforcement", "Judicial confirmations", "Anti-abortion (15-week federal ban)", "Military aid to allies"]},
        ],
        "referendums": [],
        "key_issues": ["Military/defense (large military presence)", "Immigration", "Abortion access", "Hurricane preparedness"],
        "lean": "Safe R",
        "context": "Graham won by 10pts in 2020 despite massive D fundraising. SC remains solid R territory.",
        "public_opinion": {"approval": "Graham ~47% approve. Military/defense is top priority.", "top_concern": "National security and cost of living"}
    },

    # ===================================================================
    # 2026 GOVERNOR — KEY RACES
    # ===================================================================
    "governor_FL": {
        "incumbents": [{"name": "Ron DeSantis", "party": "R", "since": 2019, "note": "term-limited"}],
        "candidates": [],
        "referendums": [
            {"title": "Homestead Exemption Expansion", "topic": "Property Tax", "description": "Expanding homestead exemption to reduce property taxes"},
        ],
        "key_issues": ["Insurance costs (property)", "Immigration enforcement", "Education (parents' rights vs. curriculum)", "Housing affordability", "Climate/hurricane resilience"],
        "lean": "Lean R",
        "context": "DeSantis is term-limited. Open seat in a state that shifted heavily R in 2022/2024. Property insurance crisis is the sleeper issue that could open a lane for D.",
        "public_opinion": {"approval": "DeSantis approval ~50% in-state. Insurance costs are the #1 kitchen-table issue.", "top_concern": "Property insurance and housing costs"}
    },
    "governor_GA": {
        "incumbents": [{"name": "Brian Kemp", "party": "R", "since": 2019, "note": "term-limited"}],
        "candidates": [],
        "referendums": [],
        "key_issues": ["Economy and growth", "Voting laws", "Education", "Healthcare expansion (Medicaid)"],
        "lean": "Toss-up",
        "context": "Kemp is term-limited. Georgia governor races have been razor-thin. Whether D can recruit a strong candidate will determine competitiveness.",
        "public_opinion": {"approval": "Kemp is popular (~55% approve) but can't run again.", "top_concern": "Economy, followed by healthcare access"}
    },
    "governor_PA": {
        "incumbents": [{"name": "Josh Shapiro", "party": "D", "since": 2023}],
        "candidates": [
            {"name": "Josh Shapiro", "party": "D", "status": "incumbent (if running)",
             "policies": ["Education funding increase", "Infrastructure investment", "Energy (all-of-the-above)", "Public safety", "Economic development"]},
        ],
        "referendums": [],
        "key_issues": ["Economy and manufacturing", "Energy policy (fracking + clean energy)", "Education funding", "Public safety in cities"],
        "lean": "Lean D",
        "context": "Shapiro won by 15pts in 2022 and is very popular. Question is whether he runs for reelection or aims higher (president 2028).",
        "public_opinion": {"approval": "Shapiro has ~58% approval — one of most popular governors.", "top_concern": "Economy and energy jobs"}
    },
    "governor_TX": {
        "incumbents": [{"name": "Greg Abbott", "party": "R", "since": 2015}],
        "candidates": [
            {"name": "Greg Abbott", "party": "R", "status": "incumbent (if running for 4th term)",
             "policies": ["Border enforcement (Operation Lone Star)", "Business-friendly tax policy", "School choice/vouchers", "Anti-ESG", "Grid reliability investments"]},
        ],
        "referendums": [
            {"title": "School Voucher Measure", "topic": "Education", "description": "Public funding for private school tuition — highly contested in rural areas"},
        ],
        "key_issues": ["Border security", "Power grid", "School choice vs public schools", "Property taxes", "Urban vs rural divide"],
        "lean": "Likely R",
        "context": "Abbott won by 11pts in 2022. Grid reliability and school vouchers are rare wedge issues that split the R coalition (rural R voters oppose vouchers).",
        "public_opinion": {"approval": "Abbott ~49% approve. Grid and border are top issues.", "top_concern": "Border security, then grid reliability and property taxes"}
    },
    "governor_OH": {
        "incumbents": [{"name": "Mike DeWine", "party": "R", "since": 2019, "note": "term-limited"}],
        "candidates": [],
        "referendums": [],
        "key_issues": ["Economy and manufacturing", "Opioid crisis", "Abortion rights (passed 2023 amendment)", "Education"],
        "lean": "Lean R",
        "context": "DeWine term-limited. Ohio has shifted solidly R federally but voters passed a strong abortion rights amendment in 2023, showing issue-level D strength.",
        "public_opinion": {"approval": "DeWine popular at ~54%. Economy is top issue.", "top_concern": "Economy and jobs, especially in industrial areas. Abortion amendment showed voters split tickets."}
    },
    "governor_MI": {
        "incumbents": [{"name": "Gretchen Whitmer", "party": "D", "since": 2019, "note": "term-limited"}],
        "candidates": [],
        "referendums": [],
        "key_issues": ["Auto industry and EV transition", "Education", "Infrastructure (roads)", "Abortion rights (codified 2022)"],
        "lean": "Toss-up",
        "context": "Whitmer term-limited. Michigan is the ultimate swing state. Open seat will attract massive national money. Auto/EV policy is the defining issue.",
        "public_opinion": {"approval": "Whitmer very popular (~56%). Auto jobs and economy are top issues.", "top_concern": "Economy and auto industry, then education"}
    },
    "governor_WI": {
        "incumbents": [{"name": "Tony Evers", "party": "D", "since": 2019}],
        "candidates": [
            {"name": "Tony Evers", "party": "D", "status": "incumbent (if running for 3rd term)",
             "policies": ["Education funding (former state superintendent)", "Medicaid expansion", "Veto of R gerrymandering/tax cuts", "Infrastructure", "Marijuana legalization support"]},
        ],
        "referendums": [],
        "key_issues": ["Education funding", "Gerrymandering (new maps from court ruling)", "Healthcare", "Manufacturing jobs"],
        "lean": "Toss-up",
        "context": "Evers won by 3.4pts in 2022. Wisconsin is perennially 1-2pt races. New fair maps from 2024 court ruling change the legislative dynamics.",
        "public_opinion": {"approval": "Evers ~50% approve. Education is his signature issue.", "top_concern": "Education and economy"}
    },
    "governor_NV": {
        "incumbents": [{"name": "Joe Lombardo", "party": "R", "since": 2023}],
        "candidates": [
            {"name": "Joe Lombardo", "party": "R", "status": "incumbent",
             "policies": ["Public safety (former sheriff)", "Education reform (school choice)", "Business-friendly regulation", "Tourism economy support", "Water conservation"]},
        ],
        "referendums": [
            {"title": "Ranked Choice Voting Confirmation", "topic": "Elections", "description": "Second vote needed to confirm 2024's ranked-choice voting ballot measure"},
        ],
        "key_issues": ["Water scarcity (Lake Mead)", "Tourism/hospitality economy", "Housing costs", "Education quality", "Public safety"],
        "lean": "Toss-up",
        "context": "Lombardo won by just 1.3pts in 2022. Nevada is always close. The Culinary Union (hospitality workers) is the key D organizing force.",
        "public_opinion": {"approval": "Lombardo ~47% approve. Housing and water are top issues.", "top_concern": "Cost of living and water/drought"}
    },

    # ===================================================================
    # CONTROL / NATIONAL
    # ===================================================================
    "control_US": {
        "incumbents": [],
        "candidates": [],
        "referendums": [],
        "key_issues": ["Senate control (50-50 dynamic)", "House majority (slim margins)", "Presidential agenda items", "Judicial confirmations"],
        "lean": "Toss-up",
        "context": "2026 midterms historically favor the opposition party. The president's party has lost House seats in every midterm since 2002 (except 2002). Senate map favors D in 2026 (more R seats up).",
        "public_opinion": {"approval": "Congressional approval typically 20-30%. Voters prefer divided government.", "top_concern": "Economy, immigration, and healthcare are the top national issues."}
    },
}


def get_context(race_type: str = None, state: str = None):
    """Get context for a specific race."""
    key = f"{race_type}_{state}" if race_type and state else None
    if key and key in RACE_CONTEXT:
        return RACE_CONTEXT[key]
    return None


def get_all_contexts() -> dict:
    """Return all race contexts."""
    return RACE_CONTEXT


def search_contexts(query: str):
    """Search contexts by keyword."""
    query = query.lower()
    results = []
    for key, ctx in RACE_CONTEXT.items():
        searchable = f"{key} {ctx.get('context', '')} {' '.join(ctx.get('key_issues', []))}".lower()
        if query in searchable:
            results.append({"race_key": key, **ctx})
    return results
