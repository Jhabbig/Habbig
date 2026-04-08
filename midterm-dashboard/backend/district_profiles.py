from __future__ import annotations
"""Comprehensive state & district profiles for the Midterm Elections Dashboard.

Provides background context about each state/district: demographics, economy,
infrastructure, political history, geography, and key facts.  Profiles are
keyed by state abbreviation and auto-matched to races.

A background task in main.py periodically scans active races and ensures every
state with an active race has an up-to-date profile stored in the DB.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Comprehensive state profiles
# ---------------------------------------------------------------------------
# Each profile contains:
#   population, demographics, economy, infrastructure, political_history,
#   geography, education, key_facts, last_updated

STATE_PROFILES: dict[str, dict] = {
    "GA": {
        "name": "Georgia",
        "population": {"total": 10_912_876, "year": 2024, "rank": 8, "growth_rate": "1.0% annually"},
        "demographics": {
            "white": 51.2, "black": 33.0, "hispanic": 10.5, "asian": 4.6, "other": 0.7,
            "median_age": 37.1, "urban_pct": 76.4,
            "summary": "Georgia has one of the most diverse electorates in the South, with a large Black population concentrated in Metro Atlanta and the Black Belt counties. Rapid suburban growth, especially among college-educated and Asian-American voters, has reshaped the political landscape."
        },
        "economy": {
            "gdp_billions": 755, "median_household_income": 65_030, "unemployment_rate": 3.3,
            "top_industries": ["Film & entertainment", "Logistics & transportation", "Agriculture (poultry, pecans, peanuts)", "Technology (Atlanta tech corridor)", "Military & defense"],
            "major_employers": ["Delta Air Lines", "Home Depot", "UPS", "Coca-Cola", "Aflac"],
            "summary": "Atlanta is a major logistics hub (Hartsfield-Jackson is the world's busiest airport) and an emerging tech center. The film industry ('Hollywood of the South') contributes $4B+ annually. Agriculture remains vital in rural areas."
        },
        "infrastructure": {
            "major_airports": ["Hartsfield-Jackson Atlanta International (ATL)"],
            "interstate_highways": ["I-75", "I-85", "I-20", "I-16", "I-95"],
            "ports": ["Port of Savannah (4th largest US container port)"],
            "military_bases": ["Fort Eisenhower (formerly Fort Gordon)", "Robins AFB", "Fort Stewart", "Kings Bay Naval Submarine Base"],
            "summary": "Georgia is a logistics powerhouse. The Port of Savannah has seen explosive growth and is a top-5 US container port. Atlanta's airport handles 90M+ passengers/year. Major interstate intersection connects the Southeast."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+0.2%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+2.2%"},
            "governor_since": "Brian Kemp (R) since 2019, term-limited",
            "state_legislature": "Republican trifecta",
            "electoral_votes": 16,
            "trend": "Shifted from solid R to battleground. Biden flipped it in 2020 (first D since 1992), but Trump won it back in 2024. Suburban Atlanta is the swing zone.",
            "cook_pvi": "R+3",
            "summary": "Georgia is the most competitive Southern state. The Atlanta metro's rapid diversification and suburban shift have made it a genuine battleground, though rural consolidation for R keeps it slightly right-leaning overall."
        },
        "geography": {
            "area_sq_miles": 59_425, "region": "Southeast",
            "major_cities": ["Atlanta", "Augusta", "Columbus", "Savannah", "Macon"],
            "terrain": "Appalachian mountains in the north, Piedmont plateau in the center, coastal plain and barrier islands in the south",
            "climate": "Humid subtropical; mild winters, hot summers"
        },
        "education": {
            "major_universities": ["Georgia Tech", "University of Georgia", "Emory University", "Morehouse College", "Spelman College"],
            "bachelors_or_higher_pct": 33.7,
            "summary": "Strong university system anchored by Georgia Tech (engineering/CS) and UGA. Atlanta's HBCUs (Morehouse, Spelman, Clark Atlanta) are nationally significant."
        },
        "key_facts": [
            "World's busiest airport (Hartsfield-Jackson ATL)",
            "4th largest container port (Savannah)",
            "#1 state for film production outside California",
            "Home to 18 Fortune 500 companies",
            "Fastest-growing state east of the Mississippi in the 2020s"
        ]
    },

    "AZ": {
        "name": "Arizona",
        "population": {"total": 7_431_344, "year": 2024, "rank": 14, "growth_rate": "1.6% annually"},
        "demographics": {
            "white": 53.4, "black": 5.2, "hispanic": 31.7, "asian": 3.7, "other": 5.9,
            "median_age": 38.3, "urban_pct": 89.8,
            "summary": "Arizona's large and growing Hispanic population (nearly a third of residents) is a decisive electoral force. The state has attracted massive migration from California, shifting suburban Phoenix's politics."
        },
        "economy": {
            "gdp_billions": 445, "median_household_income": 65_913, "unemployment_rate": 3.6,
            "top_industries": ["Semiconductor manufacturing (TSMC, Intel)", "Aerospace & defense", "Healthcare", "Tourism", "Real estate & construction"],
            "major_employers": ["Banner Health", "Raytheon", "Intel", "Arizona State University", "Honeywell"],
            "summary": "Arizona is a booming tech and semiconductor hub. TSMC's $40B+ fab complex in Phoenix is the largest foreign investment in US history. The state's economy has diversified beyond tourism and real estate."
        },
        "infrastructure": {
            "major_airports": ["Phoenix Sky Harbor (PHX)", "Tucson International"],
            "interstate_highways": ["I-10", "I-17", "I-40", "I-19"],
            "ports": [],
            "military_bases": ["Luke AFB", "Davis-Monthan AFB", "Fort Huachuca", "Marine Corps Air Station Yuma"],
            "summary": "Arizona's infrastructure is strained by rapid growth. Water infrastructure (Central Arizona Project canal from the Colorado River) is the existential issue. Phoenix Sky Harbor is a major Southwest hub."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+0.3%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+2.1%"},
            "governor_since": "Katie Hobbs (D) since 2023",
            "state_legislature": "Republican majority in both chambers",
            "electoral_votes": 11,
            "trend": "Classic swing state. Was solid R until 2018 when Sinema won Senate. Biden flipped it in 2020. Trump won it back in 2024. Maricopa County (Phoenix) decides everything.",
            "cook_pvi": "R+2",
            "summary": "Arizona has the tightest margins of any battleground. Border politics pull rightward while suburban growth and Hispanic engagement pull leftward. Every statewide race is competitive."
        },
        "geography": {
            "area_sq_miles": 113_990, "region": "Southwest",
            "major_cities": ["Phoenix", "Tucson", "Mesa", "Chandler", "Scottsdale"],
            "terrain": "Sonoran Desert in the south, Colorado Plateau and Grand Canyon in the north, mountains in the center",
            "climate": "Arid desert in most areas; Phoenix averages 300+ sunny days/year"
        },
        "education": {
            "major_universities": ["Arizona State University (largest US university by enrollment)", "University of Arizona", "Northern Arizona University"],
            "bachelors_or_higher_pct": 31.5,
            "summary": "ASU is the nation's largest university (80k+ students) and a major research institution. U of A in Tucson is a top astronomy/optics research center."
        },
        "key_facts": [
            "TSMC's $40B+ semiconductor fab — largest foreign investment in US history",
            "Grand Canyon — one of the Seven Natural Wonders",
            "Water crisis: Colorado River allocation is the #1 policy issue",
            "Fastest-growing large metro in the US (Phoenix)",
            "5th largest state by area"
        ]
    },

    "PA": {
        "name": "Pennsylvania",
        "population": {"total": 12_972_008, "year": 2024, "rank": 5, "growth_rate": "0.1% annually"},
        "demographics": {
            "white": 73.5, "black": 12.0, "hispanic": 8.4, "asian": 3.8, "other": 2.3,
            "median_age": 40.9, "urban_pct": 78.7,
            "summary": "Pennsylvania is defined by its urban-rural divide: liberal Philadelphia and its suburbs vs. conservative rural 'T' in the center. The Pittsburgh metro has shifted from blue-collar D to increasingly purple."
        },
        "economy": {
            "gdp_billions": 923, "median_household_income": 67_587, "unemployment_rate": 3.4,
            "top_industries": ["Healthcare & pharmaceuticals", "Energy (natural gas fracking)", "Manufacturing", "Financial services", "Agriculture (mushrooms, dairy)"],
            "major_employers": ["Penn Medicine", "UPMC", "Comcast", "US Steel", "Hershey"],
            "summary": "Pennsylvania straddles old and new economy. The Marcellus Shale fracking boom has made it the #2 natural gas producer. Philadelphia's healthcare/pharma corridor and Pittsburgh's tech renaissance (CMU/robotics) drive growth."
        },
        "infrastructure": {
            "major_airports": ["Philadelphia International (PHL)", "Pittsburgh International (PIT)"],
            "interstate_highways": ["I-76 (PA Turnpike)", "I-80", "I-95", "I-81", "I-78"],
            "ports": ["Port of Philadelphia (PhilaPort)"],
            "military_bases": ["Carlisle Barracks (Army War College)", "Naval Support Activity Philadelphia", "Tobyhanna Army Depot"],
            "summary": "The PA Turnpike is one of the oldest and most-traveled toll roads in the US. Philadelphia's port handles bulk cargo. Pittsburgh's infrastructure has been rebuilt around the tech and healthcare economy."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+1.2%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+1.8%"},
            "governor_since": "Josh Shapiro (D) since 2023",
            "state_legislature": "Split (R House, D Senate)",
            "electoral_votes": 19,
            "trend": "The ultimate bellwether. Went for Trump in 2016 (first R since 1988), Biden in 2020, Trump again in 2024. Philadelphia suburbs have trended D while rural areas have shifted massively R.",
            "cook_pvi": "R+1",
            "summary": "No state better illustrates America's political realignment. The 'collar counties' around Philadelphia (Chester, Delaware, Montgomery, Bucks) have swung 15+ points toward D since 2012, while rural PA has moved 20+ points toward R."
        },
        "geography": {
            "area_sq_miles": 46_054, "region": "Mid-Atlantic / Northeast",
            "major_cities": ["Philadelphia", "Pittsburgh", "Allentown", "Erie", "Reading"],
            "terrain": "Appalachian Mountains through the center, Piedmont and coastal plain in the east, Great Lakes shore in the northwest",
            "climate": "Humid continental; cold winters with significant snowfall, warm summers"
        },
        "education": {
            "major_universities": ["University of Pennsylvania (Ivy League)", "Carnegie Mellon University", "Penn State", "Temple University", "Drexel University"],
            "bachelors_or_higher_pct": 34.3,
            "summary": "UPenn and CMU are world-class research institutions. Penn State is one of the largest university systems. CMU's computer science and robotics programs fuel Pittsburgh's tech renaissance."
        },
        "key_facts": [
            "2nd largest natural gas producing state (Marcellus Shale)",
            "Liberty Bell and Independence Hall — birthplace of American democracy",
            "Home to Hershey (chocolate capital) and the nation's mushroom capital",
            "19 electoral votes — largest true swing state",
            "Pittsburgh's transformation from steel to tech/healthcare is a national model"
        ]
    },

    "MI": {
        "name": "Michigan",
        "population": {"total": 10_037_261, "year": 2024, "rank": 10, "growth_rate": "0.2% annually"},
        "demographics": {
            "white": 72.4, "black": 14.1, "hispanic": 5.7, "asian": 3.4, "other": 4.4,
            "median_age": 39.8, "urban_pct": 74.6,
            "summary": "Michigan has significant Black communities in Detroit, Flint, and Saginaw, plus a notable Arab-American population in Dearborn (largest per capita outside the Middle East). Union households remain a key voting bloc."
        },
        "economy": {
            "gdp_billions": 620, "median_household_income": 63_202, "unemployment_rate": 3.8,
            "top_industries": ["Automotive manufacturing & EV transition", "Healthcare", "Agriculture (cherries, blueberries)", "Tourism (Great Lakes)", "Defense & aerospace"],
            "major_employers": ["General Motors", "Ford Motor Company", "Stellantis", "Dow Chemical", "Spectrum Health"],
            "summary": "Michigan is ground zero for the EV transition. The Big Three automakers are headquartered here, and the shift to electric vehicles is the defining economic and political issue. A $2.5B+ battery plant boom is underway."
        },
        "infrastructure": {
            "major_airports": ["Detroit Metropolitan (DTW)", "Gerald R. Ford International (GRR)"],
            "interstate_highways": ["I-94", "I-96", "I-75", "I-69"],
            "ports": ["Port of Detroit"],
            "military_bases": ["Selfridge Air National Guard Base", "Camp Grayling (largest National Guard training facility)"],
            "summary": "Michigan's infrastructure is built around the auto industry. The state has 11,000+ miles of state roads. The Mackinac Bridge connects the Upper and Lower Peninsulas. Great Lakes shipping is a major freight corridor."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+2.8%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+1.4%"},
            "governor_since": "Gretchen Whitmer (D) since 2019, term-limited in 2026",
            "state_legislature": "Democratic trifecta (first since 1984)",
            "electoral_votes": 15,
            "trend": "Michigan is the Midwest's premier swing state. Trump flipped it in 2016 by 10,700 votes, Biden won it back by 154k in 2020, then Trump retook it in 2024. Wayne County (Detroit) turnout is the key variable.",
            "cook_pvi": "R+1",
            "summary": "Michigan swings with working-class sentiment. Union households, auto workers, and the Arab-American community in Dearborn are decisive. The EV transition has split the traditional D labor coalition."
        },
        "geography": {
            "area_sq_miles": 96_714, "region": "Midwest / Great Lakes",
            "major_cities": ["Detroit", "Grand Rapids", "Warren", "Ann Arbor", "Lansing"],
            "terrain": "Two peninsulas surrounded by Great Lakes. Lower Peninsula is flat to rolling; Upper Peninsula is rugged and forested",
            "climate": "Humid continental; cold snowy winters, warm summers. Lake effect snow along the coasts."
        },
        "education": {
            "major_universities": ["University of Michigan (Ann Arbor)", "Michigan State University", "Wayne State University"],
            "bachelors_or_higher_pct": 30.0,
            "summary": "U of M is a top-5 public research university. Michigan State is a land-grant powerhouse. Ann Arbor is a major college town that anchors the Washtenaw County D stronghold."
        },
        "key_facts": [
            "Birthplace of the American auto industry (Henry Ford, Detroit)",
            "Ground zero for the EV transition — $2.5B+ in new battery plants",
            "More coastline than any state except Alaska (3,288 miles of Great Lakes shore)",
            "Dearborn has the largest Arab-American community per capita in the US",
            "Flint water crisis (2014-2019) remains a potent political issue"
        ]
    },

    "TX": {
        "name": "Texas",
        "population": {"total": 30_503_340, "year": 2024, "rank": 2, "growth_rate": "1.6% annually"},
        "demographics": {
            "white": 39.8, "black": 13.2, "hispanic": 40.2, "asian": 5.4, "other": 1.4,
            "median_age": 35.0, "urban_pct": 84.7,
            "summary": "Texas is a majority-minority state. The Hispanic population (40%+) is the largest demographic group and is concentrated along the border and in urban centers. Rapid growth from domestic migration (primarily from CA) is reshaping suburban politics."
        },
        "economy": {
            "gdp_billions": 2_356, "median_household_income": 67_321, "unemployment_rate": 4.0,
            "top_industries": ["Oil & gas (largest US producer)", "Technology (Austin, Dallas)", "Healthcare", "Agriculture & ranching", "Aerospace & defense"],
            "major_employers": ["ExxonMobil", "AT&T", "Dell Technologies", "Texas Instruments", "HEB Grocery"],
            "summary": "If Texas were a country, it would have the 8th largest economy in the world. The energy sector remains dominant but tech (Austin's 'Silicon Hills'), healthcare (Texas Medical Center in Houston), and finance (Dallas) have diversified the economy significantly."
        },
        "infrastructure": {
            "major_airports": ["DFW International (DFW)", "George Bush Intercontinental (IAH)", "Austin-Bergstrom (AUS)", "San Antonio International"],
            "interstate_highways": ["I-35", "I-10", "I-20", "I-45", "I-30"],
            "ports": ["Port of Houston (largest US port by tonnage)", "Port of Corpus Christi", "Port of Beaumont"],
            "military_bases": ["Fort Cavazos (formerly Fort Hood)", "Joint Base San Antonio", "Fort Bliss", "Naval Air Station Corpus Christi"],
            "summary": "Texas has the most extensive highway system in the US. The Port of Houston is the largest US port by foreign tonnage. The state's power grid (ERCOT) is independent from the national grid — a vulnerability exposed by the 2021 freeze."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+5.6%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+5.1%"},
            "governor_since": "Greg Abbott (R) since 2015",
            "state_legislature": "Republican supermajority",
            "electoral_votes": 40,
            "trend": "Texas has been slowly trending competitive but remains solidly R statewide. Cruz won by only 2.6% in 2018 (vs. Beto). The urban centers (Austin, Houston, Dallas, San Antonio) are deep blue, but rural and suburban R margins are massive.",
            "cook_pvi": "R+7",
            "summary": "The 'will Texas flip?' question has been perennial since 2018. Demographic change (Hispanic growth, CA migration) favors D long-term, but R have maintained margins through rural consolidation and Hispanic outreach in South Texas."
        },
        "geography": {
            "area_sq_miles": 268_596, "region": "South / Southwest",
            "major_cities": ["Houston", "San Antonio", "Dallas", "Austin", "Fort Worth", "El Paso"],
            "terrain": "Vast and varied: Gulf Coast plains, Hill Country, Chihuahuan Desert, Panhandle prairies, piney woods in the east",
            "climate": "Ranges from humid subtropical in the east to arid desert in the west. Extreme heat in summer."
        },
        "education": {
            "major_universities": ["University of Texas at Austin", "Texas A&M University", "Rice University", "SMU", "Baylor University"],
            "bachelors_or_higher_pct": 31.3,
            "summary": "UT Austin and Texas A&M are flagship public universities. Rice is a top private research university. The UT system's Permanent University Fund ($30B+) is the largest university endowment system in the US."
        },
        "key_facts": [
            "2nd largest state by population and area",
            "Largest US oil & gas producer — economy would rank 8th globally",
            "Independent power grid (ERCOT) — 2021 freeze killed 200+ people",
            "40 electoral votes — most of any reliably R state",
            "Texas Medical Center in Houston is the world's largest medical complex"
        ]
    },

    "NC": {
        "name": "North Carolina",
        "population": {"total": 10_835_491, "year": 2024, "rank": 9, "growth_rate": "1.1% annually"},
        "demographics": {
            "white": 61.4, "black": 22.2, "hispanic": 10.7, "asian": 3.3, "other": 2.4,
            "median_age": 39.1, "urban_pct": 66.1,
            "summary": "North Carolina's Research Triangle (Raleigh-Durham-Chapel Hill) is one of the fastest-growing metros in the US, attracting highly educated transplants. The state has a significant Black population, especially in the eastern counties."
        },
        "economy": {
            "gdp_billions": 680, "median_household_income": 61_972, "unemployment_rate": 3.5,
            "top_industries": ["Banking & finance (Charlotte)", "Technology (Research Triangle)", "Agriculture (tobacco, sweet potatoes, hogs)", "Military", "Biotechnology & pharmaceuticals"],
            "major_employers": ["Bank of America", "Wells Fargo", "Duke Energy", "Lowe's", "Honeywell"],
            "summary": "Charlotte is the 2nd largest US banking center after NYC. The Research Triangle Park is the nation's largest research park. North Carolina has successfully transitioned from tobacco/textiles to tech and finance."
        },
        "infrastructure": {
            "major_airports": ["Charlotte Douglas International (CLT)", "Raleigh-Durham International (RDU)"],
            "interstate_highways": ["I-40", "I-85", "I-77", "I-95", "I-26"],
            "ports": ["Port of Wilmington", "Port of Morehead City"],
            "military_bases": ["Fort Liberty (formerly Fort Bragg — largest Army base by population)", "Camp Lejeune", "Seymour Johnson AFB"],
            "summary": "Charlotte Douglas is a major American Airlines hub. Fort Liberty is the largest military installation by population in the US. I-40 and I-85 form the state's economic spine connecting the major metros."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+1.3%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+3.3%"},
            "governor_since": "Josh Stein (D) since 2025",
            "state_legislature": "Republican supermajority (veto-proof)",
            "electoral_votes": 16,
            "trend": "NC is perpetually close but has gone R in every presidential election since 2012 (Obama won it in 2008 by 0.3%). D can win governor/AG but struggle at the presidential level. Wake County (Raleigh) growth helps D.",
            "cook_pvi": "R+3",
            "summary": "North Carolina is the competitive Southern state that D almost-but-never-quite flip. The Research Triangle's growth helps D, but rural consolidation for R has kept pace."
        },
        "geography": {
            "area_sq_miles": 53_819, "region": "Southeast",
            "major_cities": ["Charlotte", "Raleigh", "Greensboro", "Durham", "Winston-Salem"],
            "terrain": "Appalachian Mountains in the west, Piedmont plateau in the center, coastal plain and Outer Banks in the east",
            "climate": "Humid subtropical; mild winters in the Piedmont, colder in the mountains, warm at the coast"
        },
        "education": {
            "major_universities": ["Duke University", "UNC Chapel Hill", "NC State", "Wake Forest University"],
            "bachelors_or_higher_pct": 33.4,
            "summary": "The Research Triangle (Duke, UNC, NC State) is one of America's premier academic clusters. This concentration of research talent drives the state's tech and biotech economy."
        },
        "key_facts": [
            "2nd largest US banking center (Charlotte)",
            "Research Triangle Park — largest research park in the US",
            "Fort Liberty — largest US Army base by population",
            "Outer Banks — iconic barrier island chain",
            "Obama's 2008 win (by 0.3%) was the only D presidential win since 1976"
        ]
    },

    "FL": {
        "name": "Florida",
        "population": {"total": 22_610_726, "year": 2024, "rank": 3, "growth_rate": "1.6% annually"},
        "demographics": {
            "white": 51.5, "black": 16.9, "hispanic": 26.8, "asian": 3.0, "other": 1.8,
            "median_age": 42.4, "urban_pct": 91.2,
            "summary": "Florida's Hispanic population is uniquely diverse — Cuban-Americans (heavily R) in Miami-Dade, Puerto Ricans (lean D) in Central Florida, and growing Venezuelan/Colombian communities. The state's median age (42.4) is the highest in the Sun Belt."
        },
        "economy": {
            "gdp_billions": 1_389, "median_household_income": 63_062, "unemployment_rate": 3.2,
            "top_industries": ["Tourism (Disney, beaches)", "Real estate & construction", "Agriculture (oranges, sugarcane)", "Aerospace (Space Coast)", "Healthcare & senior services"],
            "major_employers": ["Walt Disney World", "Publix", "NextEra Energy", "Baptist Health", "Lockheed Martin"],
            "summary": "Tourism is king — Florida attracts 140M+ visitors/year. The Space Coast (Cape Canaveral, SpaceX) is booming. No state income tax attracts both retirees and remote workers, fueling the real estate market."
        },
        "infrastructure": {
            "major_airports": ["Miami International (MIA)", "Orlando International (MCO)", "Fort Lauderdale (FLL)", "Tampa International (TPA)"],
            "interstate_highways": ["I-95", "I-75", "I-4", "I-10", "Florida Turnpike"],
            "ports": ["Port of Miami (cruise capital of the world)", "Port Everglades", "Port of Jacksonville", "Port of Tampa Bay"],
            "military_bases": ["MacDill AFB (CENTCOM & SOCOM HQ)", "NAS Jacksonville", "Eglin AFB", "Patrick Space Force Base"],
            "summary": "Florida's infrastructure serves 140M+ annual tourists. I-4 corridor (Tampa-Orlando-Daytona) is the state's political and economic spine. Property insurance is in crisis — Citizens Insurance (state insurer of last resort) has exploded in size."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+3.4%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+13.2%"},
            "governor_since": "Ron DeSantis (R) since 2019, term-limited in 2026",
            "state_legislature": "Republican supermajority",
            "electoral_votes": 30,
            "trend": "Florida has shifted dramatically from purple to red. Obama won it twice, but Trump carried it by 3.4% in 2020 and 13.2% in 2024. Miami-Dade flipped R for the first time since 2000. Property insurance crisis is the wild card for 2026.",
            "cook_pvi": "R+6",
            "summary": "Florida's days as a swing state appear over — for now. The R shift among Hispanic voters (especially Cuban and Venezuelan) and massive retiree in-migration have built a durable R advantage. The property insurance crisis is the one issue that could disrupt this."
        },
        "geography": {
            "area_sq_miles": 65_758, "region": "Southeast / Sun Belt",
            "major_cities": ["Jacksonville", "Miami", "Tampa", "Orlando", "St. Petersburg"],
            "terrain": "Low-lying peninsula, Everglades wetlands in the south, barrier islands and Keys",
            "climate": "Tropical in the south, subtropical in the north. Hurricane season (June-November) is a major risk."
        },
        "education": {
            "major_universities": ["University of Florida", "Florida State University", "University of Miami", "University of Central Florida (2nd largest US university)", "University of South Florida"],
            "bachelors_or_higher_pct": 32.4,
            "summary": "UCF is the 2nd largest university in the US by enrollment. UF in Gainesville is a top-5 public university. Florida's university system is one of the most affordable in the nation."
        },
        "key_facts": [
            "3rd most populous state — passed New York in 2014",
            "No state income tax — major driver of in-migration",
            "Property insurance crisis — premiums tripled in many areas since 2020",
            "Cape Canaveral / Kennedy Space Center — home of American spaceflight",
            "DeSantis is term-limited in 2026 — open governor's seat"
        ]
    },

    "OH": {
        "name": "Ohio",
        "population": {"total": 11_780_017, "year": 2024, "rank": 7, "growth_rate": "-0.1% annually"},
        "demographics": {
            "white": 76.7, "black": 13.3, "hispanic": 4.4, "asian": 2.5, "other": 3.1,
            "median_age": 39.5, "urban_pct": 77.9,
            "summary": "Ohio is older and whiter than the national average. Rust Belt cities (Cleveland, Youngstown, Toledo) have lost population while Columbus has boomed. The non-college white working class is the dominant voting bloc."
        },
        "economy": {
            "gdp_billions": 782, "median_household_income": 61_938, "unemployment_rate": 3.7,
            "top_industries": ["Manufacturing (auto parts, steel)", "Healthcare", "Agriculture (soybeans, corn)", "Technology (Columbus)", "Energy (natural gas, wind)"],
            "major_employers": ["Cleveland Clinic", "Ohio State University", "Kroger", "Nationwide Insurance", "Honda (Marysville plant)"],
            "summary": "Ohio is reinventing its Rust Belt economy. Intel's $20B+ semiconductor fab near Columbus is the state's biggest economic bet. Columbus is the fastest-growing city in the Midwest. Cleveland Clinic is a world-class healthcare institution."
        },
        "infrastructure": {
            "major_airports": ["Cleveland Hopkins (CLE)", "John Glenn Columbus (CMH)", "Cincinnati/Northern Kentucky (CVG)"],
            "interstate_highways": ["I-70", "I-71", "I-75", "I-77", "I-80/90 (Ohio Turnpike)"],
            "ports": ["Port of Cleveland", "Port of Toledo", "Port of Cincinnati"],
            "military_bases": ["Wright-Patterson AFB (Air Force research HQ)", "NASA Glenn Research Center"],
            "summary": "Ohio sits at the crossroads of major interstates connecting the East Coast to the Midwest. Wright-Patterson AFB is the center of US Air Force research. Great Lakes ports connect to global shipping via the St. Lawrence Seaway."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+8.0%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+11.2%"},
            "governor_since": "Mike DeWine (R) since 2019, term-limited in 2026",
            "state_legislature": "Republican supermajority",
            "electoral_votes": 17,
            "trend": "Ohio has shifted from the ultimate bellwether to lean-R. Obama won it twice, but Trump carried it by 8+ points in both 2020 and 2024. However, voters passed a strong abortion rights amendment in 2023 — showing issue-level D strength even as the state trends R at the candidate level.",
            "cook_pvi": "R+6",
            "summary": "Ohio's shift from swing to lean-R is one of the biggest realignments of the 2010s-2020s. The working-class white vote moved R while Columbus's growth hasn't been enough to compensate. But the 2023 abortion amendment (57% yes) shows voters still cross party lines on issues."
        },
        "geography": {
            "area_sq_miles": 44_826, "region": "Midwest / Great Lakes",
            "major_cities": ["Columbus", "Cleveland", "Cincinnati", "Toledo", "Akron"],
            "terrain": "Great Lakes shore in the north, Appalachian foothills in the southeast, flat till plains in the west",
            "climate": "Humid continental; cold winters with lake-effect snow near Lake Erie, warm humid summers"
        },
        "education": {
            "major_universities": ["Ohio State University (3rd largest US university)", "Case Western Reserve", "University of Cincinnati", "Miami University"],
            "bachelors_or_higher_pct": 29.6,
            "summary": "Ohio State in Columbus is a massive research university and economic engine. Case Western in Cleveland is a top medical/engineering school. The state's university system is large but the bachelor's attainment rate lags national averages."
        },
        "key_facts": [
            "Intel's $20B+ semiconductor fab — biggest investment in state history",
            "Abortion rights amendment passed 57-43% in 2023 despite R supermajority",
            "Wright-Patterson AFB — center of US Air Force research",
            "Columbus is the fastest-growing major city in the Midwest",
            "Ohio has voted for the presidential winner in every election from 1964-2016"
        ]
    },

    "WI": {
        "name": "Wisconsin",
        "population": {"total": 5_910_955, "year": 2024, "rank": 20, "growth_rate": "0.2% annually"},
        "demographics": {
            "white": 80.2, "black": 6.8, "hispanic": 7.6, "asian": 3.0, "other": 2.4,
            "median_age": 40.0, "urban_pct": 70.2,
            "summary": "Wisconsin is one of the whitest states in the Midwest. Milwaukee has a significant Black population, and the Hmong community is one of the largest in the US. The state's political divide is Milwaukee/Madison vs. everywhere else."
        },
        "economy": {
            "gdp_billions": 382, "median_household_income": 67_125, "unemployment_rate": 2.9,
            "top_industries": ["Manufacturing (machinery, paper)", "Agriculture (dairy — 'America's Dairyland')", "Healthcare", "Insurance & finance", "Tourism (lakes, Door County)"],
            "major_employers": ["Epic Systems", "SC Johnson", "Harley-Davidson", "Oshkosh Corp", "Kohl's"],
            "summary": "Wisconsin's economy blends manufacturing heritage with modern industries. Epic Systems in Verona (near Madison) is the nation's largest electronic health records company. Dairy farming remains culturally and economically central."
        },
        "infrastructure": {
            "major_airports": ["Milwaukee Mitchell International (MKE)", "Dane County Regional (MSN)"],
            "interstate_highways": ["I-94", "I-90", "I-43", "I-39"],
            "ports": ["Port of Milwaukee", "Port of Green Bay", "Port of Superior"],
            "military_bases": ["Fort McCoy"],
            "summary": "Wisconsin's Great Lakes ports provide shipping access. The I-94 corridor connects Milwaukee to Chicago (90 miles) and Minneapolis. Infrastructure investment has been a bipartisan priority."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+0.6%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+0.9%"},
            "governor_since": "Tony Evers (D) since 2019",
            "state_legislature": "New fair maps from 2024 court ruling — first competitive elections in 2026",
            "electoral_votes": 10,
            "trend": "Wisconsin is perpetually decided by 1-2 points. Trump won by 22k votes in 2016, Biden by 20k in 2020, Trump by ~30k in 2024. Dane County (Madison) turnout vs. WOW counties (Waukesha-Ozaukee-Washington) is the formula.",
            "cook_pvi": "Even",
            "summary": "Wisconsin may be the most evenly divided state in America. Fair redistricting maps (from the 2024 court ruling) will make the 2026 state legislature competitive for the first time in a decade."
        },
        "geography": {
            "area_sq_miles": 65_496, "region": "Midwest / Great Lakes",
            "major_cities": ["Milwaukee", "Madison", "Green Bay", "Kenosha", "Racine"],
            "terrain": "Rolling plains, northern forests, Great Lakes shoreline, driftless area in the southwest",
            "climate": "Humid continental; cold snowy winters (especially near Lake Michigan), warm summers"
        },
        "education": {
            "major_universities": ["University of Wisconsin-Madison (top-5 public research university)", "Marquette University", "UW-Milwaukee"],
            "bachelors_or_higher_pct": 31.3,
            "summary": "UW-Madison is a world-class research university that drives Dane County's booming economy. Madison's college-town energy makes it the most liberal city in the Midwest."
        },
        "key_facts": [
            "Most evenly divided swing state — last 3 elections decided by <1%",
            "America's Dairyland — largest US cheese producer",
            "2024 court ruling created first fair legislative maps in a decade",
            "Epic Systems in Verona — largest electronic health records company",
            "Milwaukee/Madison vs. WOW counties is the defining political divide"
        ]
    },

    "NV": {
        "name": "Nevada",
        "population": {"total": 3_194_176, "year": 2024, "rank": 32, "growth_rate": "1.2% annually"},
        "demographics": {
            "white": 45.9, "black": 10.8, "hispanic": 29.2, "asian": 8.7, "other": 5.4,
            "median_age": 38.7, "urban_pct": 94.2,
            "summary": "Nevada is a majority-minority state and the most urbanized in the nation — 94%+ live in metro areas (mostly Las Vegas). The Culinary Workers Union (hospitality workers, majority Hispanic and Black) is the most powerful political force in the state."
        },
        "economy": {
            "gdp_billions": 212, "median_household_income": 63_276, "unemployment_rate": 5.3,
            "top_industries": ["Tourism & hospitality (Las Vegas Strip)", "Mining (gold, lithium, copper)", "Logistics & warehousing", "Clean energy (solar, geothermal)", "Technology (data centers)"],
            "major_employers": ["MGM Resorts", "Caesars Entertainment", "Wynn Resorts", "Station Casinos", "Switch (data centers)"],
            "summary": "Las Vegas drives the economy — tourism generates $70B+ annually. But Nevada is diversifying: the Gigafactory (Tesla/Panasonic), Thacker Pass (largest US lithium mine), and massive data center construction are reducing dependence on hospitality."
        },
        "infrastructure": {
            "major_airports": ["Harry Reid International Las Vegas (LAS)", "Reno-Tahoe International"],
            "interstate_highways": ["I-15", "I-80", "I-515"],
            "ports": [],
            "military_bases": ["Nellis AFB", "Creech AFB (drone warfare HQ)", "Naval Air Station Fallon", "Nevada Test and Training Range"],
            "summary": "Las Vegas's airport is one of the busiest in the US. The state has a massive military testing footprint — the Nevada Test Site and Area 51 are here. Water infrastructure (Lake Mead/Colorado River) is the existential challenge."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+2.4%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+3.2%"},
            "governor_since": "Joe Lombardo (R) since 2023",
            "state_legislature": "D majority in both chambers",
            "electoral_votes": 6,
            "trend": "Nevada has been decided by <3 points in every election since 2016. D have won most recent statewide races but often by razor-thin margins. Clark County (Las Vegas) drives everything — 73% of the state's population.",
            "cook_pvi": "Even",
            "summary": "Nevada is the West's premier swing state. The Culinary Union's organizing power in Las Vegas has kept D competitive, but Republican inroads among Hispanic voters and non-union workers are closing the gap."
        },
        "geography": {
            "area_sq_miles": 110_572, "region": "West / Mountain",
            "major_cities": ["Las Vegas", "Henderson", "Reno", "North Las Vegas", "Sparks"],
            "terrain": "Basin and Range — rugged mountains alternating with flat desert basins. Driest state in the US.",
            "climate": "Arid desert; Las Vegas averages 4 inches of rain/year. Extreme heat in summer (115F+ common)."
        },
        "education": {
            "major_universities": ["University of Nevada Las Vegas (UNLV)", "University of Nevada Reno"],
            "bachelors_or_higher_pct": 25.8,
            "summary": "Nevada has one of the lowest bachelor's attainment rates in the US, reflecting the hospitality economy's non-degree workforce. UNLV has been growing rapidly."
        },
        "key_facts": [
            "Most urbanized state in the US — 94%+ live in metro areas",
            "Las Vegas Strip generates $70B+ annually in tourism",
            "Lake Mead/Colorado River water crisis — existential threat",
            "Tesla Gigafactory and Thacker Pass lithium mine — clean energy hub",
            "Culinary Workers Union — most powerful political machine in the state"
        ]
    },

    "CO": {
        "name": "Colorado",
        "population": {"total": 5_877_610, "year": 2024, "rank": 21, "growth_rate": "0.7% annually"},
        "demographics": {
            "white": 65.1, "black": 4.6, "hispanic": 22.3, "asian": 3.5, "other": 4.5,
            "median_age": 37.1, "urban_pct": 86.2,
            "summary": "Colorado has attracted massive migration of college-educated workers, especially to the Front Range (Denver-Boulder-Fort Collins). This in-migration has shifted the state from purple to solidly blue at the statewide level."
        },
        "economy": {
            "gdp_billions": 468, "median_household_income": 82_254, "unemployment_rate": 3.4,
            "top_industries": ["Technology (Denver/Boulder tech corridor)", "Aerospace & defense", "Tourism (skiing, outdoor recreation)", "Energy (oil & gas + renewables)", "Cannabis"],
            "major_employers": ["Lockheed Martin", "Ball Corporation", "DISH Network", "Arrow Electronics", "University of Colorado"],
            "summary": "Colorado has one of the strongest economies in the US, driven by tech (Denver is a top-10 tech hub), aerospace (Lockheed Martin, United Launch Alliance), and outdoor recreation tourism. The state leads in cannabis industry revenue."
        },
        "infrastructure": {
            "major_airports": ["Denver International (DEN — 5th busiest US airport)"],
            "interstate_highways": ["I-25", "I-70", "I-76"],
            "ports": [],
            "military_bases": ["US Air Force Academy (Colorado Springs)", "Peterson Space Force Base", "Schriever Space Force Base", "Fort Carson", "NORAD (Cheyenne Mountain)"],
            "summary": "Denver International is a major national hub. I-70 through the Rockies is a critical mountain corridor. Colorado Springs is the epicenter of US Space Force operations. NORAD is headquartered inside Cheyenne Mountain."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+13.5%"},
            "2024_presidential": {"winner": "Harris (D)", "margin": "+10.8%"},
            "governor_since": "Jared Polis (D) since 2019",
            "state_legislature": "Democratic trifecta",
            "electoral_votes": 10,
            "trend": "Colorado has completed its transition from swing state to reliably blue. Bush won it in 2004, but D have won every presidential race since 2008 with growing margins. The Front Range's educated, transplant-heavy population drives this.",
            "cook_pvi": "D+3",
            "summary": "Colorado is no longer competitive at the statewide level. The question in 2026 is margin, not outcome. Housing affordability along the Front Range is the dominant issue."
        },
        "geography": {
            "area_sq_miles": 104_094, "region": "Mountain West",
            "major_cities": ["Denver", "Colorado Springs", "Aurora", "Fort Collins", "Boulder"],
            "terrain": "Rocky Mountains bisect the state. Eastern plains, high peaks (54 fourteeners), western plateau",
            "climate": "Semi-arid; 300+ days of sunshine. Extreme variation by altitude — Denver is mild, mountains are alpine."
        },
        "education": {
            "major_universities": ["University of Colorado Boulder", "Colorado State University", "US Air Force Academy", "Colorado School of Mines"],
            "bachelors_or_higher_pct": 42.7,
            "summary": "Colorado has one of the highest bachelor's degree rates in the nation (42.7%), reflecting the tech/professional workforce. CU Boulder is a top research university. School of Mines is a premier engineering school."
        },
        "key_facts": [
            "Highest average elevation of any state (6,800 feet)",
            "NORAD HQ inside Cheyenne Mountain",
            "42.7% bachelor's degree rate — among the highest in the US",
            "Denver International is the 5th busiest US airport",
            "First state to legalize recreational cannabis (2012)"
        ]
    },

    "NH": {
        "name": "New Hampshire",
        "population": {"total": 1_402_054, "year": 2024, "rank": 41, "growth_rate": "0.5% annually"},
        "demographics": {
            "white": 89.0, "black": 1.8, "hispanic": 4.3, "asian": 3.0, "other": 1.9,
            "median_age": 43.0, "urban_pct": 60.3,
            "summary": "New Hampshire is one of the whitest and oldest states. Its 'Live Free or Die' libertarian streak makes it unique in New England. High cost of living and proximity to Boston shape its economy and demographics."
        },
        "economy": {
            "gdp_billions": 104, "median_household_income": 88_465, "unemployment_rate": 2.4,
            "top_industries": ["Technology & software", "Healthcare", "Tourism (skiing, leaf peeping)", "Manufacturing (precision instruments)", "Defense (Portsmouth Naval Shipyard)"],
            "major_employers": ["Dartmouth-Hitchcock Health", "BAE Systems", "Fidelity Investments", "Liberty Mutual"],
            "summary": "New Hampshire has no income tax and no sales tax, attracting businesses and residents from Massachusetts. It has one of the lowest unemployment rates and highest median incomes in the country."
        },
        "infrastructure": {
            "major_airports": ["Manchester-Boston Regional (MHT)"],
            "interstate_highways": ["I-93", "I-89", "I-95"],
            "ports": ["Port of Portsmouth"],
            "military_bases": ["Portsmouth Naval Shipyard (submarine maintenance)"],
            "summary": "New Hampshire benefits from proximity to Boston. I-93 connects the state to the Boston metro. The Portsmouth Naval Shipyard is a critical submarine maintenance facility."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+7.4%"},
            "2024_presidential": {"winner": "Harris (D)", "margin": "+4.2%"},
            "governor_since": "Kelly Ayotte (R) since 2025",
            "state_legislature": "Split (R House, D Senate after 2024)",
            "electoral_votes": 4,
            "trend": "New Hampshire is the most competitive state in New England. It swings more than its neighbors — Trump came within 0.4% in 2016. The 'First in the Nation' primary gives it outsized political influence.",
            "cook_pvi": "D+1",
            "summary": "New Hampshire is New England's only true swing state. Its libertarian streak and lack of income/sales tax distinguish it from deep-blue neighbors. Shaheen's potential retirement in 2026 could make the Senate race very competitive."
        },
        "geography": {
            "area_sq_miles": 9_349, "region": "New England",
            "major_cities": ["Manchester", "Nashua", "Concord", "Dover", "Rochester"],
            "terrain": "White Mountains in the north, lakes region in the center, seacoast in the southeast",
            "climate": "Humid continental; cold snowy winters, warm summers. Significant seasonal tourism."
        },
        "education": {
            "major_universities": ["Dartmouth College (Ivy League)", "University of New Hampshire", "Saint Anselm College"],
            "bachelors_or_higher_pct": 38.0,
            "summary": "Dartmouth in Hanover is an Ivy League institution. UNH is the state's public university. Saint Anselm hosts major presidential primary debates."
        },
        "key_facts": [
            "No income tax and no sales tax — unique in the US",
            "'Live Free or Die' — strongest libertarian culture in New England",
            "First-in-the-nation presidential primary",
            "Most competitive state in New England",
            "Shaheen may retire in 2026 — could create open Senate seat"
        ]
    },

    "IA": {
        "name": "Iowa",
        "population": {"total": 3_207_004, "year": 2024, "rank": 31, "growth_rate": "0.1% annually"},
        "demographics": {
            "white": 83.8, "black": 4.1, "hispanic": 7.0, "asian": 2.8, "other": 2.3,
            "median_age": 38.2, "urban_pct": 64.0,
            "summary": "Iowa is predominantly white and rural. The meatpacking industry has attracted growing Hispanic and immigrant communities to smaller cities. The state's aging farm population is a long-term demographic challenge."
        },
        "economy": {
            "gdp_billions": 222, "median_household_income": 65_573, "unemployment_rate": 2.8,
            "top_industries": ["Agriculture (corn, soybeans, pork — #1 US hog producer)", "Food processing", "Insurance & finance (Des Moines)", "Manufacturing", "Wind energy"],
            "major_employers": ["Principal Financial", "John Deere", "Hy-Vee", "Corteva Agriscience", "Wellmark Blue Cross"],
            "summary": "Iowa is the agricultural heartland. It's the #1 US producer of corn, hogs, and ethanol. Des Moines is a major insurance hub. Iowa has also become a wind energy leader — wind generates 60%+ of its electricity."
        },
        "infrastructure": {
            "major_airports": ["Des Moines International (DSM)", "Eastern Iowa (CID)"],
            "interstate_highways": ["I-80", "I-35", "I-29", "I-380"],
            "ports": [],
            "military_bases": ["Camp Dodge (Iowa National Guard)"],
            "summary": "Iowa's infrastructure is agriculture-focused: grain elevators, ethanol plants, and Mississippi River barge shipping. I-80 and I-35 are the major freight corridors."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+8.2%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+13.4%"},
            "governor_since": "Kim Reynolds (R) since 2017",
            "state_legislature": "Republican trifecta",
            "electoral_votes": 6,
            "trend": "Iowa has shifted from swing to solid R. Obama won it twice, but Trump carried it by 8+ and 13+ points. Rural consolidation for R has been massive. The 'First in the Nation' caucus was stripped in 2024 by the DNC.",
            "cook_pvi": "R+8",
            "summary": "Iowa's political transformation is dramatic — it went from Obama+6 in 2012 to Trump+13 in 2024. Rural depopulation and cultural realignment have made it uncompetitive for D at the statewide level."
        },
        "geography": {
            "area_sq_miles": 56_273, "region": "Midwest",
            "major_cities": ["Des Moines", "Cedar Rapids", "Davenport", "Sioux City", "Iowa City"],
            "terrain": "Rolling prairies, some of the most fertile farmland in the world. Mississippi River on the east, Missouri River on the west.",
            "climate": "Humid continental; harsh winters, hot humid summers. Severe weather (tornadoes, flooding) is common."
        },
        "education": {
            "major_universities": ["University of Iowa", "Iowa State University", "Drake University"],
            "bachelors_or_higher_pct": 29.9,
            "summary": "U of Iowa (Iowa City) is known for its writing program (Iowa Writers' Workshop). Iowa State is a land-grant university strong in agriculture and engineering."
        },
        "key_facts": [
            "#1 US producer of corn, hogs, and ethanol",
            "Wind energy generates 60%+ of Iowa's electricity",
            "Lost 'First in the Nation' caucus status with DNC in 2024",
            "Shifted from Obama+6 (2012) to Trump+13 (2024)",
            "Des Moines is a top-3 US insurance hub"
        ]
    },

    "ME": {
        "name": "Maine",
        "population": {"total": 1_395_722, "year": 2024, "rank": 42, "growth_rate": "0.3% annually"},
        "demographics": {
            "white": 92.0, "black": 1.8, "hispanic": 2.1, "asian": 1.3, "other": 2.8,
            "median_age": 45.1, "urban_pct": 38.7,
            "summary": "Maine is the whitest and oldest state in the US (median age 45.1). Its rural character and aging population create unique political dynamics. A growing Somali and immigrant community in Lewiston has become politically notable."
        },
        "economy": {
            "gdp_billions": 82, "median_household_income": 64_767, "unemployment_rate": 3.0,
            "top_industries": ["Fishing & lobster (iconic)", "Tourism (Acadia, coastal)", "Healthcare", "Forest products & paper", "Shipbuilding (Bath Iron Works)"],
            "major_employers": ["Maine Health", "Bath Iron Works (Navy destroyers)", "L.L. Bean", "Hannaford", "IDEXX Laboratories"],
            "summary": "Maine's economy blends traditional industries (fishing, forestry) with tourism and defense. The lobster industry ($700M+/year) is culturally and economically central. Bath Iron Works builds Navy destroyers."
        },
        "infrastructure": {
            "major_airports": ["Portland International Jetport (PWM)"],
            "interstate_highways": ["I-95 (Maine Turnpike)", "I-295"],
            "ports": ["Port of Portland", "Port of Eastport"],
            "military_bases": ["Portsmouth Naval Shipyard (shared with NH)", "Naval Computer and Telecommunications Station Cutler"],
            "summary": "Maine's infrastructure reflects its rural character. I-95 is the main artery. The state has invested in broadband expansion for rural areas. Ports serve the fishing industry and some cargo."
        },
        "political_history": {
            "2020_presidential": {"winner": "Biden (D)", "margin": "+9.1% (statewide)"},
            "2024_presidential": {"winner": "Harris (D)", "margin": "+6.8%"},
            "governor_since": "Janet Mills (D) since 2019",
            "state_legislature": "Democratic trifecta",
            "electoral_votes": 4,
            "trend": "Maine splits its electoral votes by congressional district. The 1st District (Portland/coast) is solidly D. The 2nd District (rural north) voted for Trump in both 2016 and 2020 but went D in 2024. Susan Collins remains popular despite the state's D lean.",
            "cook_pvi": "D+3",
            "summary": "Maine is a D-leaning state where Collins' bipartisan brand still wins. The CD-2 split makes it one of only two states (with Nebraska) that can split electoral votes. The lobster industry and fishing rights are uniquely important policy issues."
        },
        "geography": {
            "area_sq_miles": 35_380, "region": "New England",
            "major_cities": ["Portland", "Lewiston", "Bangor", "South Portland", "Auburn"],
            "terrain": "Rocky coastline, dense forests, mountains (Katahdin). 90% forested — most forested state in the US.",
            "climate": "Humid continental; cold snowy winters, mild summers. Maritime influence on the coast."
        },
        "education": {
            "major_universities": ["Bowdoin College", "Bates College", "Colby College", "University of Maine"],
            "bachelors_or_higher_pct": 33.3,
            "summary": "Maine has three prestigious liberal arts colleges (Bowdoin, Bates, Colby — the 'Colby-Bates-Bowdoin' triumvirate). University of Maine in Orono is the state's public research university."
        },
        "key_facts": [
            "Whitest and oldest state in the US",
            "Splits electoral votes by congressional district (only 2 states do this)",
            "Lobster industry worth $700M+/year — cultural identity",
            "90% forested — most forested state",
            "Collins is the only Republican senator in New England"
        ]
    },

    "SC": {
        "name": "South Carolina",
        "population": {"total": 5_373_555, "year": 2024, "rank": 23, "growth_rate": "1.3% annually"},
        "demographics": {
            "white": 62.1, "black": 26.8, "hispanic": 6.8, "asian": 1.8, "other": 2.5,
            "median_age": 39.8, "urban_pct": 66.3,
            "summary": "South Carolina has a large Black population (27%) concentrated in the Lowcountry and Midlands. The state has seen rapid growth from retiree in-migration and military families. Charleston has become a top destination city."
        },
        "economy": {
            "gdp_billions": 296, "median_household_income": 59_318, "unemployment_rate": 3.3,
            "top_industries": ["Military & defense", "Manufacturing (BMW, Boeing, Volvo)", "Tourism (Charleston, Myrtle Beach)", "Agriculture", "Automotive"],
            "major_employers": ["BMW (Spartanburg — largest BMW plant in the world)", "Boeing (Charleston)", "Prisma Health", "Michelin"],
            "summary": "South Carolina has attracted major foreign manufacturers — BMW, Volvo, and Boeing all have major facilities. The Port of Charleston is growing rapidly. Military spending is a massive economic driver."
        },
        "infrastructure": {
            "major_airports": ["Charleston International (CHS)", "Greenville-Spartanburg (GSP)", "Myrtle Beach (MYR)"],
            "interstate_highways": ["I-95", "I-26", "I-85", "I-77"],
            "ports": ["Port of Charleston (fast-growing East Coast container port)"],
            "military_bases": ["Joint Base Charleston", "Fort Jackson (Army basic training)", "Marine Corps Recruit Depot Parris Island", "Shaw AFB"],
            "summary": "South Carolina has a massive military footprint — Fort Jackson is the Army's largest basic training center, and Parris Island trains half of all Marines. The Port of Charleston is one of the fastest-growing on the East Coast."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+11.7%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+13.8%"},
            "governor_since": "Henry McMaster (R) since 2017",
            "state_legislature": "Republican supermajority",
            "electoral_votes": 9,
            "trend": "South Carolina is solidly R — no D has won statewide since 2006. The 'First in the South' presidential primary gives it outsized influence in R nominations.",
            "cook_pvi": "R+9",
            "summary": "South Carolina is safe R territory. Graham's 2020 Senate race attracted massive D fundraising but he still won by 10 points. The state's influence comes from its early presidential primary, not competitive general elections."
        },
        "geography": {
            "area_sq_miles": 32_020, "region": "Southeast",
            "major_cities": ["Charleston", "Columbia", "North Charleston", "Greenville", "Rock Hill"],
            "terrain": "Blue Ridge Mountains in the northwest, Piedmont in the center, coastal plain and barrier islands in the east",
            "climate": "Humid subtropical; mild winters, hot humid summers. Hurricane risk on the coast."
        },
        "education": {
            "major_universities": ["Clemson University", "University of South Carolina", "The Citadel", "College of Charleston"],
            "bachelors_or_higher_pct": 29.1,
            "summary": "Clemson and USC are the state's major public universities with an intense rivalry. The Citadel is a historic military college in Charleston."
        },
        "key_facts": [
            "Largest BMW plant in the world (Spartanburg)",
            "Fort Jackson — Army's largest basic training center",
            "Parris Island — trains half of all US Marines",
            "'First in the South' presidential primary",
            "Charleston ranked #1 US city by Travel + Leisure multiple years"
        ]
    },

    "MT": {
        "name": "Montana",
        "population": {"total": 1_132_812, "year": 2024, "rank": 44, "growth_rate": "1.1% annually"},
        "demographics": {
            "white": 84.6, "black": 0.6, "hispanic": 4.4, "asian": 0.9, "other": 9.5,
            "median_age": 39.8, "urban_pct": 55.9,
            "summary": "Montana has a significant Native American population (~7%) spread across seven reservations. The state has seen rapid in-migration, especially to Bozeman and Missoula, driving up housing costs and shifting local politics."
        },
        "economy": {
            "gdp_billions": 62, "median_household_income": 60_560, "unemployment_rate": 2.8,
            "top_industries": ["Agriculture (cattle, wheat)", "Mining (coal, copper, gold)", "Tourism (Glacier, Yellowstone)", "Timber & forestry", "Technology (Bozeman startup scene)"],
            "major_employers": ["Billings Clinic", "St. Vincent Healthcare", "Montana State University", "BNSF Railway"],
            "summary": "Montana's economy is resource-based but diversifying. Bozeman has become a tech hub ('Silicon Prairie'). Tourism around Glacier and Yellowstone national parks is a major revenue driver. Agriculture and mining remain foundational."
        },
        "infrastructure": {
            "major_airports": ["Billings Logan (BIL)", "Bozeman Yellowstone (BZN)", "Missoula Montana (MSO)"],
            "interstate_highways": ["I-90", "I-15", "I-94"],
            "ports": [],
            "military_bases": ["Malmstrom AFB (ICBM missile base)"],
            "summary": "Montana is vast and sparsely populated — infrastructure is stretched. Malmstrom AFB houses Minuteman III ICBMs. The state's highway system covers enormous distances between population centers."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+16.4%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+18.2%"},
            "governor_since": "Greg Gianforte (R) since 2021",
            "state_legislature": "Republican supermajority",
            "electoral_votes": 4,
            "trend": "Montana is solidly R at the federal level but has a tradition of electing D governors and senators (Tester served until 2024). Tester's loss in 2024 may mark the end of D competitiveness here.",
            "cook_pvi": "R+11",
            "summary": "Montana's libertarian-populist streak occasionally elects D (Tester, Bullock), but the state is trending more solidly R as national politics dominate. Tester's defeat in 2024 was a watershed moment."
        },
        "geography": {
            "area_sq_miles": 147_040, "region": "Mountain West",
            "major_cities": ["Billings", "Missoula", "Great Falls", "Bozeman", "Helena"],
            "terrain": "Rocky Mountains in the west, Great Plains in the east. 'Big Sky Country'",
            "climate": "Continental; harsh cold winters, warm short summers. Extreme variation by region."
        },
        "education": {
            "major_universities": ["University of Montana (Missoula)", "Montana State University (Bozeman)"],
            "bachelors_or_higher_pct": 33.1,
            "summary": "UM and MSU are the two main universities. Bozeman's university helps fuel the town's tech and outdoor recreation economy."
        },
        "key_facts": [
            "4th largest state by area, 44th by population",
            "Glacier and Yellowstone national parks",
            "Malmstrom AFB — houses US nuclear ICBMs",
            "Tester's 2024 loss may end D competitiveness here",
            "Bozeman is one of the fastest-growing small cities in the US"
        ]
    },

    "WY": {
        "name": "Wyoming",
        "population": {"total": 584_057, "year": 2024, "rank": 50, "growth_rate": "0.4% annually"},
        "demographics": {
            "white": 83.6, "black": 1.0, "hispanic": 10.5, "asian": 0.9, "other": 4.0,
            "median_age": 38.0, "urban_pct": 64.8,
            "summary": "Wyoming is the least populous US state. Its small, dispersed population is predominantly white and rural. The state's political culture is strongly libertarian and conservative."
        },
        "economy": {
            "gdp_billions": 47, "median_household_income": 65_003, "unemployment_rate": 3.3,
            "top_industries": ["Mining (coal, trona, oil & gas)", "Tourism (Yellowstone, Grand Teton)", "Agriculture (cattle, sheep)", "Wind energy"],
            "major_employers": ["State of Wyoming", "University of Wyoming", "Cloud Peak Energy", "Wyoming Medical Center"],
            "summary": "Wyoming's economy is heavily resource-dependent. It produces 40% of US coal and is a top oil/gas state. Tourism around Yellowstone and Grand Teton is a major employer. The state has no income tax (funded by mineral severance taxes)."
        },
        "infrastructure": {
            "major_airports": ["Jackson Hole Airport (JAC)", "Casper/Natrona County (CPR)"],
            "interstate_highways": ["I-80", "I-25", "I-90"],
            "ports": [],
            "military_bases": ["F.E. Warren AFB (ICBM base — oldest continuously active military installation in the US)"],
            "summary": "Wyoming's infrastructure serves a vast, sparsely populated state. I-80 across southern Wyoming is a major transcontinental freight corridor. F.E. Warren AFB is one of three US ICBM bases."
        },
        "political_history": {
            "2020_presidential": {"winner": "Trump (R)", "margin": "+43.4%"},
            "2024_presidential": {"winner": "Trump (R)", "margin": "+46.3%"},
            "governor_since": "Mark Gordon (R) since 2019",
            "state_legislature": "Republican supermajority (most R state legislature in the US)",
            "electoral_votes": 3,
            "trend": "Wyoming is the most Republican state in the US. It hasn't voted for a D president since 1964. Liz Cheney's 2022 primary loss illustrated how little room there is for intra-party dissent here.",
            "cook_pvi": "R+25",
            "summary": "Wyoming is the reddest state in America. Its politics are defined by energy policy (coal, oil, gas) and a fierce libertarian individualism. Cheney's primary loss was a national story but surprised no one locally."
        },
        "geography": {
            "area_sq_miles": 97_813, "region": "Mountain West",
            "major_cities": ["Cheyenne", "Casper", "Laramie", "Gillette", "Rock Springs"],
            "terrain": "High plains in the east, Rocky Mountains in the west. Yellowstone Plateau, Wind River Range, Bighorn Mountains.",
            "climate": "Semi-arid continental; cold harsh winters, short warm summers. Very windy."
        },
        "education": {
            "major_universities": ["University of Wyoming (only 4-year university in the state)"],
            "bachelors_or_higher_pct": 28.2,
            "summary": "University of Wyoming in Laramie is the state's sole four-year public university — unique among US states."
        },
        "key_facts": [
            "Least populous US state (584k people)",
            "Most Republican state — Trump+46% in 2024",
            "Produces 40% of US coal",
            "Yellowstone and Grand Teton national parks",
            "F.E. Warren AFB — oldest continuously active US military base",
            "Only state with a single four-year public university",
            "Has two congressional districts for House races"
        ]
    },
}


def get_profile(state: str) -> dict | None:
    """Get a full profile for a state abbreviation."""
    return STATE_PROFILES.get(state.upper())


def get_all_profiles() -> dict:
    """Return all state profiles."""
    return STATE_PROFILES


def get_available_states() -> list[str]:
    """Return list of states that have profiles."""
    return sorted(STATE_PROFILES.keys())


def generate_basic_profile(state: str, state_name: str = "") -> dict:
    """Generate a minimal profile template for a state without a full profile.

    This is used by the background task to create placeholder profiles for
    newly-discovered states.  The template can be enriched later.
    """
    return {
        "name": state_name or state,
        "population": {"total": 0, "year": 2024, "rank": 0, "growth_rate": "N/A"},
        "demographics": {
            "summary": f"Demographic data for {state_name or state} is being compiled."
        },
        "economy": {
            "summary": f"Economic profile for {state_name or state} is being compiled."
        },
        "infrastructure": {
            "summary": f"Infrastructure data for {state_name or state} is being compiled."
        },
        "political_history": {
            "summary": f"Political history for {state_name or state} is being compiled."
        },
        "geography": {
            "region": "United States",
            "major_cities": [],
        },
        "education": {
            "summary": f"Education data for {state_name or state} is being compiled."
        },
        "key_facts": [],
        "_auto_generated": True,
    }
