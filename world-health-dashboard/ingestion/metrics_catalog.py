"""Metrics catalog — every indicator the dashboard exposes.

Each metric has a stable internal `id` independent of the upstream source code,
so the upstream can be swapped (e.g. a WHO indicator retired, replace with a
World Bank or OWID equivalent) without changing the public API.

Field semantics:
  id          : canonical metric id used in our URLs and the frontend.
  name        : display name.
  category    : top-level grouping in the metric selector.
  unit        : human-readable unit string ("years", "%", "per 1,000 live births").
  source      : "who_gho" | "world_bank"
  source_code : indicator code in that source.
  direction   : "higher_is_better" | "lower_is_better" | "neutral"
                — used by the frontend to pick a color ramp direction.
  decimals    : preferred display precision.
  description : one-sentence what-it-is.

Categories follow WHO's standard groupings (mortality, NCDs, MNCH, immunization,
workforce, finance, risk factors, demographics) — keeps users oriented.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    category: str
    unit: str
    source: str
    source_code: str
    direction: str
    decimals: int
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Catalog ──────────────────────────────────────────────────────────────────
# Note: WHO GHO codes verified against https://ghoapi.azureedge.net/api/Indicator
# World Bank codes verified against https://api.worldbank.org/v2/indicator
# When in doubt about a code, prefer the World Bank source (cleaner, easier to
# query in bulk via /country/all/indicator/<CODE>).

CATALOG: list[Metric] = [
    # ── Life & death ─────────────────────────────────────────────────────────
    Metric("life_expectancy", "Life expectancy at birth", "Life & death",
           "years", "world_bank", "SP.DYN.LE00.IN", "higher_is_better", 1,
           "Years a newborn would live under current age-specific mortality."),
    Metric("life_expectancy_male", "Life expectancy (male)", "Life & death",
           "years", "world_bank", "SP.DYN.LE00.MA.IN", "higher_is_better", 1,
           "Male life expectancy at birth."),
    Metric("life_expectancy_female", "Life expectancy (female)", "Life & death",
           "years", "world_bank", "SP.DYN.LE00.FE.IN", "higher_is_better", 1,
           "Female life expectancy at birth."),
    Metric("hale", "Healthy life expectancy (HALE)", "Life & death",
           "years", "who_gho", "WHOSIS_000002", "higher_is_better", 1,
           "Years lived in full health, accounting for time in poor health."),
    Metric("infant_mortality", "Infant mortality", "Life & death",
           "per 1,000 live births", "world_bank", "SP.DYN.IMRT.IN", "lower_is_better", 1,
           "Deaths under age 1 per 1,000 live births."),
    Metric("under5_mortality", "Under-5 mortality", "Life & death",
           "per 1,000 live births", "world_bank", "SH.DYN.MORT", "lower_is_better", 1,
           "Probability of dying before age 5 per 1,000 live births."),
    Metric("neonatal_mortality", "Neonatal mortality", "Life & death",
           "per 1,000 live births", "world_bank", "SH.DYN.NMRT", "lower_is_better", 1,
           "Deaths in first 28 days per 1,000 live births."),
    Metric("maternal_mortality", "Maternal mortality ratio", "Life & death",
           "per 100,000 live births", "world_bank", "SH.STA.MMRT", "lower_is_better", 0,
           "Maternal deaths per 100,000 live births."),
    Metric("adult_mortality_male", "Adult mortality (male, 15–60)", "Life & death",
           "per 1,000", "world_bank", "SP.DYN.AMRT.MA", "lower_is_better", 0,
           "Probability a 15-year-old male dies before age 60."),
    Metric("adult_mortality_female", "Adult mortality (female, 15–60)", "Life & death",
           "per 1,000", "world_bank", "SP.DYN.AMRT.FE", "lower_is_better", 0,
           "Probability a 15-year-old female dies before age 60."),
    Metric("crude_death_rate", "Crude death rate", "Life & death",
           "per 1,000", "world_bank", "SP.DYN.CDRT.IN", "neutral", 1,
           "Deaths per 1,000 population per year."),
    Metric("suicide_rate", "Suicide mortality rate", "Life & death",
           "per 100,000", "world_bank", "SH.STA.SUIC.P5", "lower_is_better", 1,
           "Age-standardized suicide deaths per 100,000."),

    # ── Disease burden ───────────────────────────────────────────────────────
    Metric("hiv_prevalence", "HIV prevalence (15–49)", "Disease burden",
           "% of population", "world_bank", "SH.DYN.AIDS.ZS", "lower_is_better", 2,
           "Share of adults aged 15–49 living with HIV."),
    Metric("hiv_incidence", "HIV new infections", "Disease burden",
           "per 1,000 uninfected", "world_bank", "SH.HIV.INCD.ZS", "lower_is_better", 2,
           "New HIV infections per 1,000 uninfected population."),
    Metric("tb_incidence", "Tuberculosis incidence", "Disease burden",
           "per 100,000", "world_bank", "SH.TBS.INCD", "lower_is_better", 0,
           "Estimated new TB cases per 100,000 population."),
    Metric("tb_mortality", "Tuberculosis mortality (HIV-neg)", "Disease burden",
           "per 100,000", "who_gho", "MDG_0000000023", "lower_is_better", 1,
           "TB deaths excluding HIV-positive cases."),
    Metric("malaria_incidence", "Malaria incidence", "Disease burden",
           "per 1,000 at risk", "world_bank", "SH.MLR.INCD.P3", "lower_is_better", 0,
           "New malaria cases per 1,000 population at risk."),
    Metric("ncd_premature_mortality", "Premature NCD mortality (30–70)", "Disease burden",
           "%", "world_bank", "SH.DYN.NCOM.ZS", "lower_is_better", 1,
           "Probability of dying from cardiovascular, cancer, diabetes or chronic respiratory between 30–70."),
    Metric("cardiovascular_mortality", "Cardiovascular & related mortality", "Disease burden",
           "per 100,000", "who_gho", "NCDMORT3070", "lower_is_better", 1,
           "Probability of premature death from CVD, cancer, diabetes or CRD."),
    Metric("road_traffic_deaths", "Road traffic mortality", "Disease burden",
           "per 100,000", "world_bank", "SH.STA.TRAF.P5", "lower_is_better", 1,
           "Estimated road-traffic deaths per 100,000."),
    Metric("homicide_rate", "Intentional homicide rate", "Disease burden",
           "per 100,000", "world_bank", "VC.IHR.PSRC.P5", "lower_is_better", 1,
           "Intentional homicide victims per 100,000."),

    # ── Maternal & child health ──────────────────────────────────────────────
    Metric("birth_skilled_attendant", "Births attended by skilled staff", "Maternal & child",
           "%", "world_bank", "SH.STA.BRTC.ZS", "higher_is_better", 1,
           "Share of births attended by skilled health personnel."),
    Metric("antenatal_care", "Antenatal care (4+ visits)", "Maternal & child",
           "%", "world_bank", "SH.STA.ANVC.ZS", "higher_is_better", 1,
           "Share of pregnant women receiving 4+ antenatal visits."),
    Metric("contraceptive_prev", "Contraceptive prevalence (any method)", "Maternal & child",
           "%", "world_bank", "SP.DYN.CONU.ZS", "higher_is_better", 1,
           "Share of women aged 15–49 (in union) using any contraception."),
    Metric("adolescent_fertility", "Adolescent fertility rate", "Maternal & child",
           "births per 1,000 women 15–19", "world_bank", "SP.ADO.TFRT", "lower_is_better", 1,
           "Annual births per 1,000 women aged 15–19."),
    Metric("stunting", "Stunting prevalence (under-5)", "Maternal & child",
           "%", "world_bank", "SH.STA.STNT.ME.ZS", "lower_is_better", 1,
           "Share of children under 5 with low height-for-age."),
    Metric("wasting", "Wasting prevalence (under-5)", "Maternal & child",
           "%", "world_bank", "SH.STA.WAST.ME.ZS", "lower_is_better", 1,
           "Share of children under 5 with low weight-for-height."),
    Metric("breastfeeding_excl", "Exclusive breastfeeding (<6 mo)", "Maternal & child",
           "%", "world_bank", "SH.STA.BFED.ZS", "higher_is_better", 1,
           "Share of infants under 6 months exclusively breastfed."),

    # ── Immunization ─────────────────────────────────────────────────────────
    Metric("imm_dtp3", "DTP3 immunization", "Immunization",
           "% of 1-yr-olds", "world_bank", "SH.IMM.IDPT", "higher_is_better", 1,
           "Children 1 yr old immunized against diphtheria, pertussis, tetanus."),
    Metric("imm_measles", "Measles immunization (MCV1)", "Immunization",
           "% of 1-yr-olds", "world_bank", "SH.IMM.MEAS", "higher_is_better", 1,
           "Children 1 yr old immunized against measles."),
    Metric("imm_polio", "Polio immunization (Pol3)", "Immunization",
           "% of 1-yr-olds", "who_gho", "WHS4_544", "higher_is_better", 1,
           "Children 1 yr old immunized against poliomyelitis (3rd dose)."),
    Metric("imm_bcg", "BCG immunization", "Immunization",
           "% of 1-yr-olds", "who_gho", "WHS4_543", "higher_is_better", 1,
           "Children 1 yr old immunized against tuberculosis (BCG)."),
    Metric("imm_hepb3", "Hepatitis B (HepB3)", "Immunization",
           "% of 1-yr-olds", "who_gho", "WHS4_117", "higher_is_better", 1,
           "Children 1 yr old immunized with 3 doses of HepB."),
    Metric("imm_hpv", "HPV immunization (girls, last dose)", "Immunization",
           "%", "who_gho", "SDGHPVRECEIVED", "higher_is_better", 1,
           "Adolescent girls receiving final HPV dose by age 15."),

    # ── Health systems ───────────────────────────────────────────────────────
    Metric("health_spend_gdp", "Health spending (% GDP)", "Health systems",
           "% of GDP", "world_bank", "SH.XPD.CHEX.GD.ZS", "higher_is_better", 2,
           "Current health expenditure as share of GDP."),
    Metric("health_spend_pc_usd", "Health spending per capita (USD)", "Health systems",
           "USD", "world_bank", "SH.XPD.CHEX.PC.CD", "higher_is_better", 0,
           "Current health expenditure per capita in current USD."),
    Metric("health_spend_pc_ppp", "Health spending per capita (PPP)", "Health systems",
           "intl. $", "world_bank", "SH.XPD.CHEX.PP.CD", "higher_is_better", 0,
           "Per-capita health spend at PPP-adjusted intl. dollars."),
    Metric("oop_share", "Out-of-pocket share", "Health systems",
           "% of CHE", "world_bank", "SH.XPD.OOPC.CH.ZS", "lower_is_better", 1,
           "Out-of-pocket as share of current health expenditure."),
    Metric("physicians", "Physicians per 1,000", "Health systems",
           "per 1,000", "world_bank", "SH.MED.PHYS.ZS", "higher_is_better", 2,
           "Practicing physicians per 1,000 population."),
    Metric("nurses", "Nurses & midwives per 1,000", "Health systems",
           "per 1,000", "world_bank", "SH.MED.NUMW.P3", "higher_is_better", 2,
           "Nurses and midwives per 1,000 population."),
    Metric("hospital_beds", "Hospital beds per 1,000", "Health systems",
           "per 1,000", "world_bank", "SH.MED.BEDS.ZS", "higher_is_better", 2,
           "Hospital beds per 1,000 population."),
    Metric("uhc_index", "UHC service coverage index", "Health systems",
           "index 0–100", "who_gho", "UHC_INDEX_REPORTED", "higher_is_better", 0,
           "WHO Universal Health Coverage service coverage index (SDG 3.8.1, 0–100)."),

    # ── Risk factors ─────────────────────────────────────────────────────────
    Metric("obesity_adult", "Obesity prevalence (adult)", "Risk factors",
           "%", "who_gho", "NCD_BMI_30A", "lower_is_better", 1,
           "Age-standardized adult obesity prevalence (BMI ≥ 30)."),
    Metric("overweight_adult", "Overweight prevalence (adult)", "Risk factors",
           "%", "who_gho", "NCD_BMI_25A", "lower_is_better", 1,
           "Age-standardized adult overweight prevalence (BMI ≥ 25)."),
    Metric("smoking", "Smoking prevalence (15+)", "Risk factors",
           "%", "world_bank", "SH.PRV.SMOK", "lower_is_better", 1,
           "Share of adults aged 15+ who smoke any tobacco."),
    Metric("alcohol_per_capita", "Alcohol consumption per capita", "Risk factors",
           "litres pure alcohol / 15+", "who_gho", "SA_0000001688", "lower_is_better", 2,
           "Total recorded alcohol per capita (15+) in pure-alcohol litres."),
    Metric("hypertension", "Raised blood pressure prevalence", "Risk factors",
           "%", "who_gho", "BP_04", "lower_is_better", 1,
           "Adult prevalence of raised blood pressure (≥140/90)."),
    Metric("diabetes_glucose", "Raised fasting glucose prevalence", "Risk factors",
           "%", "who_gho", "NCD_GLUC_04", "lower_is_better", 1,
           "Adult prevalence of raised fasting plasma glucose."),
    Metric("pm25_exposure", "PM2.5 air pollution exposure", "Risk factors",
           "µg/m³", "world_bank", "EN.ATM.PM25.MC.M3", "lower_is_better", 1,
           "Population-weighted mean PM2.5 exposure."),
    Metric("air_pollution_deaths", "Air-pollution attributable deaths", "Risk factors",
           "per 100,000", "world_bank", "SH.STA.AIRP.P5", "lower_is_better", 1,
           "Mortality attributable to ambient and household air pollution."),
    Metric("water_sanit_deaths", "Unsafe water/sanitation deaths", "Risk factors",
           "per 100,000", "world_bank", "SH.STA.WASH.P5", "lower_is_better", 1,
           "Mortality attributable to unsafe WASH services."),
    Metric("basic_water", "Basic drinking water access", "Risk factors",
           "%", "world_bank", "SH.H2O.BASW.ZS", "higher_is_better", 1,
           "Share of population using at least basic drinking water services."),
    Metric("basic_sanitation", "Basic sanitation access", "Risk factors",
           "%", "world_bank", "SH.STA.BASS.ZS", "higher_is_better", 1,
           "Share of population using at least basic sanitation services."),

    # ── Demographics ─────────────────────────────────────────────────────────
    Metric("population", "Population", "Demographics",
           "people", "world_bank", "SP.POP.TOTL", "neutral", 0,
           "Total population, midyear estimate."),
    Metric("fertility_rate", "Total fertility rate", "Demographics",
           "births per woman", "world_bank", "SP.DYN.TFRT.IN", "neutral", 2,
           "Average births per woman over her lifetime."),
    Metric("crude_birth_rate", "Crude birth rate", "Demographics",
           "per 1,000", "world_bank", "SP.DYN.CBRT.IN", "neutral", 1,
           "Live births per 1,000 population per year."),
    Metric("urban_pop_share", "Urban population share", "Demographics",
           "%", "world_bank", "SP.URB.TOTL.IN.ZS", "neutral", 1,
           "Share of population living in urban areas."),
    Metric("pop_65_plus", "Population aged 65+", "Demographics",
           "% of population", "world_bank", "SP.POP.65UP.TO.ZS", "neutral", 1,
           "Share of population aged 65 and older."),
    Metric("pop_growth", "Population growth", "Demographics",
           "% / year", "world_bank", "SP.POP.GROW", "neutral", 2,
           "Annual population growth rate."),
]


# Indexes for fast lookup
BY_ID: dict[str, Metric] = {m.id: m for m in CATALOG}


def all_metrics() -> list[dict]:
    return [m.to_dict() for m in CATALOG]


def get(metric_id: str) -> Metric | None:
    return BY_ID.get(metric_id)


def by_category() -> dict[str, list[Metric]]:
    out: dict[str, list[Metric]] = {}
    for m in CATALOG:
        out.setdefault(m.category, []).append(m)
    return out
