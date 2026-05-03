"""Country code index — ISO 3166-1 alpha-3 codes with display names.

WHO GHO and World Bank both use ISO3 as their primary country key, so this is
our canonical join column. The list below covers all 193 UN member states plus
a handful of widely-reported non-members (Taiwan, Palestine, Vatican, Kosovo).

We also keep a small alias table for codes that disagree across sources — most
notably, WHO uses 'GBR' for the UK while a few legacy datasets emit 'UK'.
"""

from __future__ import annotations

# (iso3, common name, region) — region is informational, useful for grouping.
COUNTRIES: list[tuple[str, str, str]] = [
    # Africa
    ("DZA", "Algeria", "Africa"),
    ("AGO", "Angola", "Africa"),
    ("BEN", "Benin", "Africa"),
    ("BWA", "Botswana", "Africa"),
    ("BFA", "Burkina Faso", "Africa"),
    ("BDI", "Burundi", "Africa"),
    ("CPV", "Cabo Verde", "Africa"),
    ("CMR", "Cameroon", "Africa"),
    ("CAF", "Central African Republic", "Africa"),
    ("TCD", "Chad", "Africa"),
    ("COM", "Comoros", "Africa"),
    ("COG", "Congo (Brazzaville)", "Africa"),
    ("COD", "Congo (Kinshasa)", "Africa"),
    ("CIV", "Côte d'Ivoire", "Africa"),
    ("DJI", "Djibouti", "Africa"),
    ("EGY", "Egypt", "Africa"),
    ("GNQ", "Equatorial Guinea", "Africa"),
    ("ERI", "Eritrea", "Africa"),
    ("SWZ", "Eswatini", "Africa"),
    ("ETH", "Ethiopia", "Africa"),
    ("GAB", "Gabon", "Africa"),
    ("GMB", "Gambia", "Africa"),
    ("GHA", "Ghana", "Africa"),
    ("GIN", "Guinea", "Africa"),
    ("GNB", "Guinea-Bissau", "Africa"),
    ("KEN", "Kenya", "Africa"),
    ("LSO", "Lesotho", "Africa"),
    ("LBR", "Liberia", "Africa"),
    ("LBY", "Libya", "Africa"),
    ("MDG", "Madagascar", "Africa"),
    ("MWI", "Malawi", "Africa"),
    ("MLI", "Mali", "Africa"),
    ("MRT", "Mauritania", "Africa"),
    ("MUS", "Mauritius", "Africa"),
    ("MAR", "Morocco", "Africa"),
    ("MOZ", "Mozambique", "Africa"),
    ("NAM", "Namibia", "Africa"),
    ("NER", "Niger", "Africa"),
    ("NGA", "Nigeria", "Africa"),
    ("RWA", "Rwanda", "Africa"),
    ("STP", "São Tomé and Príncipe", "Africa"),
    ("SEN", "Senegal", "Africa"),
    ("SYC", "Seychelles", "Africa"),
    ("SLE", "Sierra Leone", "Africa"),
    ("SOM", "Somalia", "Africa"),
    ("ZAF", "South Africa", "Africa"),
    ("SSD", "South Sudan", "Africa"),
    ("SDN", "Sudan", "Africa"),
    ("TZA", "Tanzania", "Africa"),
    ("TGO", "Togo", "Africa"),
    ("TUN", "Tunisia", "Africa"),
    ("UGA", "Uganda", "Africa"),
    ("ZMB", "Zambia", "Africa"),
    ("ZWE", "Zimbabwe", "Africa"),

    # Americas
    ("ATG", "Antigua and Barbuda", "Americas"),
    ("ARG", "Argentina", "Americas"),
    ("BHS", "Bahamas", "Americas"),
    ("BRB", "Barbados", "Americas"),
    ("BLZ", "Belize", "Americas"),
    ("BOL", "Bolivia", "Americas"),
    ("BRA", "Brazil", "Americas"),
    ("CAN", "Canada", "Americas"),
    ("CHL", "Chile", "Americas"),
    ("COL", "Colombia", "Americas"),
    ("CRI", "Costa Rica", "Americas"),
    ("CUB", "Cuba", "Americas"),
    ("DMA", "Dominica", "Americas"),
    ("DOM", "Dominican Republic", "Americas"),
    ("ECU", "Ecuador", "Americas"),
    ("SLV", "El Salvador", "Americas"),
    ("GRD", "Grenada", "Americas"),
    ("GTM", "Guatemala", "Americas"),
    ("GUY", "Guyana", "Americas"),
    ("HTI", "Haiti", "Americas"),
    ("HND", "Honduras", "Americas"),
    ("JAM", "Jamaica", "Americas"),
    ("MEX", "Mexico", "Americas"),
    ("NIC", "Nicaragua", "Americas"),
    ("PAN", "Panama", "Americas"),
    ("PRY", "Paraguay", "Americas"),
    ("PER", "Peru", "Americas"),
    ("KNA", "Saint Kitts and Nevis", "Americas"),
    ("LCA", "Saint Lucia", "Americas"),
    ("VCT", "Saint Vincent and the Grenadines", "Americas"),
    ("SUR", "Suriname", "Americas"),
    ("TTO", "Trinidad and Tobago", "Americas"),
    ("USA", "United States", "Americas"),
    ("URY", "Uruguay", "Americas"),
    ("VEN", "Venezuela", "Americas"),

    # Asia
    ("AFG", "Afghanistan", "Asia"),
    ("ARM", "Armenia", "Asia"),
    ("AZE", "Azerbaijan", "Asia"),
    ("BHR", "Bahrain", "Asia"),
    ("BGD", "Bangladesh", "Asia"),
    ("BTN", "Bhutan", "Asia"),
    ("BRN", "Brunei", "Asia"),
    ("KHM", "Cambodia", "Asia"),
    ("CHN", "China", "Asia"),
    ("CYP", "Cyprus", "Asia"),
    ("PRK", "North Korea", "Asia"),
    ("GEO", "Georgia", "Asia"),
    ("IND", "India", "Asia"),
    ("IDN", "Indonesia", "Asia"),
    ("IRN", "Iran", "Asia"),
    ("IRQ", "Iraq", "Asia"),
    ("ISR", "Israel", "Asia"),
    ("JPN", "Japan", "Asia"),
    ("JOR", "Jordan", "Asia"),
    ("KAZ", "Kazakhstan", "Asia"),
    ("KWT", "Kuwait", "Asia"),
    ("KGZ", "Kyrgyzstan", "Asia"),
    ("LAO", "Laos", "Asia"),
    ("LBN", "Lebanon", "Asia"),
    ("MYS", "Malaysia", "Asia"),
    ("MDV", "Maldives", "Asia"),
    ("MNG", "Mongolia", "Asia"),
    ("MMR", "Myanmar", "Asia"),
    ("NPL", "Nepal", "Asia"),
    ("OMN", "Oman", "Asia"),
    ("PAK", "Pakistan", "Asia"),
    ("PSE", "Palestine", "Asia"),
    ("PHL", "Philippines", "Asia"),
    ("QAT", "Qatar", "Asia"),
    ("SAU", "Saudi Arabia", "Asia"),
    ("SGP", "Singapore", "Asia"),
    ("KOR", "South Korea", "Asia"),
    ("LKA", "Sri Lanka", "Asia"),
    ("SYR", "Syria", "Asia"),
    ("TWN", "Taiwan", "Asia"),
    ("TJK", "Tajikistan", "Asia"),
    ("THA", "Thailand", "Asia"),
    ("TLS", "Timor-Leste", "Asia"),
    ("TUR", "Türkiye", "Asia"),
    ("TKM", "Turkmenistan", "Asia"),
    ("ARE", "United Arab Emirates", "Asia"),
    ("UZB", "Uzbekistan", "Asia"),
    ("VNM", "Vietnam", "Asia"),
    ("YEM", "Yemen", "Asia"),

    # Europe
    ("ALB", "Albania", "Europe"),
    ("AND", "Andorra", "Europe"),
    ("AUT", "Austria", "Europe"),
    ("BLR", "Belarus", "Europe"),
    ("BEL", "Belgium", "Europe"),
    ("BIH", "Bosnia and Herzegovina", "Europe"),
    ("BGR", "Bulgaria", "Europe"),
    ("HRV", "Croatia", "Europe"),
    ("CZE", "Czechia", "Europe"),
    ("DNK", "Denmark", "Europe"),
    ("EST", "Estonia", "Europe"),
    ("FIN", "Finland", "Europe"),
    ("FRA", "France", "Europe"),
    ("DEU", "Germany", "Europe"),
    ("GRC", "Greece", "Europe"),
    ("HUN", "Hungary", "Europe"),
    ("ISL", "Iceland", "Europe"),
    ("IRL", "Ireland", "Europe"),
    ("ITA", "Italy", "Europe"),
    ("XKX", "Kosovo", "Europe"),
    ("LVA", "Latvia", "Europe"),
    ("LIE", "Liechtenstein", "Europe"),
    ("LTU", "Lithuania", "Europe"),
    ("LUX", "Luxembourg", "Europe"),
    ("MLT", "Malta", "Europe"),
    ("MDA", "Moldova", "Europe"),
    ("MCO", "Monaco", "Europe"),
    ("MNE", "Montenegro", "Europe"),
    ("NLD", "Netherlands", "Europe"),
    ("MKD", "North Macedonia", "Europe"),
    ("NOR", "Norway", "Europe"),
    ("POL", "Poland", "Europe"),
    ("PRT", "Portugal", "Europe"),
    ("ROU", "Romania", "Europe"),
    ("RUS", "Russia", "Europe"),
    ("SMR", "San Marino", "Europe"),
    ("SRB", "Serbia", "Europe"),
    ("SVK", "Slovakia", "Europe"),
    ("SVN", "Slovenia", "Europe"),
    ("ESP", "Spain", "Europe"),
    ("SWE", "Sweden", "Europe"),
    ("CHE", "Switzerland", "Europe"),
    ("UKR", "Ukraine", "Europe"),
    ("GBR", "United Kingdom", "Europe"),
    ("VAT", "Vatican City", "Europe"),

    # Oceania
    ("AUS", "Australia", "Oceania"),
    ("FJI", "Fiji", "Oceania"),
    ("KIR", "Kiribati", "Oceania"),
    ("MHL", "Marshall Islands", "Oceania"),
    ("FSM", "Micronesia", "Oceania"),
    ("NRU", "Nauru", "Oceania"),
    ("NZL", "New Zealand", "Oceania"),
    ("PLW", "Palau", "Oceania"),
    ("PNG", "Papua New Guinea", "Oceania"),
    ("WSM", "Samoa", "Oceania"),
    ("SLB", "Solomon Islands", "Oceania"),
    ("TON", "Tonga", "Oceania"),
    ("TUV", "Tuvalu", "Oceania"),
    ("VUT", "Vanuatu", "Oceania"),
]

# Map ISO3 → (name, region)
INDEX: dict[str, tuple[str, str]] = {iso: (name, region) for iso, name, region in COUNTRIES}

# Aliases — if a source emits an alternate code, map to canonical ISO3.
# Most sources are clean; this is for the rare disagreement.
ALIASES: dict[str, str] = {
    "UK": "GBR",
    "EL": "GRC",
}


def normalize(code: str) -> str | None:
    """Return canonical ISO3 for a code, or None if unrecognized."""
    if not code:
        return None
    code = code.strip().upper()
    if code in INDEX:
        return code
    if code in ALIASES:
        return ALIASES[code]
    return None


def name_of(iso3: str) -> str | None:
    rec = INDEX.get(iso3.upper())
    return rec[0] if rec else None


def region_of(iso3: str) -> str | None:
    rec = INDEX.get(iso3.upper())
    return rec[1] if rec else None


def all_countries() -> list[dict]:
    return [{"iso3": iso, "name": name, "region": region} for iso, name, region in COUNTRIES]
