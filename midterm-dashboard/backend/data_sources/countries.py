"""ISO-2 → ISO-3 → name mapping for the 14 countries currently in our markets,
plus a generous set of others that may appear in future market scrapes.

`state` field in `midterm_markets` overlaps with US state codes (AR, CA, IL,
DE, CO all collide). Disambiguate by checking `race_type == 'world'`.
"""

# ISO-2 → demonym/adjective for filtering Wikipedia election articles
# (e.g. "2022 Hungarian parliamentary election" — we want "Hungarian" not "Hungary").
COUNTRY_ADJECTIVES: dict[str, str] = {
    "AR": "Argentine", "AU": "Australian", "AT": "Austrian", "BE": "Belgian",
    "BR": "Brazilian", "CA": "Canadian", "CH": "Swiss", "CL": "Chilean",
    "CN": "Chinese", "CO": "Colombian", "CZ": "Czech", "DE": "German",
    "DK": "Danish", "EG": "Egyptian", "ES": "Spanish", "FI": "Finnish",
    "FR": "French", "GB": "British", "UK": "British", "GR": "Greek",
    "HU": "Hungarian", "ID": "Indonesian", "IE": "Irish", "IL": "Israeli",
    "IN": "Indian", "IT": "Italian", "JP": "Japanese", "KR": "South Korean",
    "MX": "Mexican", "NG": "Nigerian", "NL": "Dutch", "NO": "Norwegian",
    "NZ": "New Zealand", "PH": "Philippine", "PK": "Pakistani", "PL": "Polish",
    "PT": "Portuguese", "RO": "Romanian", "RU": "Russian", "SE": "Swedish",
    "SG": "Singaporean", "TH": "Thai", "TR": "Turkish", "UA": "Ukrainian",
    "VE": "Venezuelan", "VN": "Vietnamese", "ZA": "South African",
}

# ISO-2 → (ISO-3, full name) for countries we may see in world-elections markets.
# Sourced from ISO 3166-1.  Add more entries as new markets appear.
COUNTRIES: dict[str, tuple[str, str]] = {
    "AR": ("ARG", "Argentina"),
    "AU": ("AUS", "Australia"),
    "AT": ("AUT", "Austria"),
    "BE": ("BEL", "Belgium"),
    "BR": ("BRA", "Brazil"),
    "CA": ("CAN", "Canada"),
    "CH": ("CHE", "Switzerland"),
    "CL": ("CHL", "Chile"),
    "CN": ("CHN", "China"),
    "CO": ("COL", "Colombia"),
    "CZ": ("CZE", "Czech Republic"),
    "DE": ("DEU", "Germany"),
    "DK": ("DNK", "Denmark"),
    "EG": ("EGY", "Egypt"),
    "ES": ("ESP", "Spain"),
    "FI": ("FIN", "Finland"),
    "FR": ("FRA", "France"),
    "GB": ("GBR", "United Kingdom"),
    "UK": ("GBR", "United Kingdom"),
    "GR": ("GRC", "Greece"),
    "HU": ("HUN", "Hungary"),
    "ID": ("IDN", "Indonesia"),
    "IE": ("IRL", "Ireland"),
    "IL": ("ISR", "Israel"),
    "IN": ("IND", "India"),
    "IT": ("ITA", "Italy"),
    "JP": ("JPN", "Japan"),
    "KR": ("KOR", "South Korea"),
    "MX": ("MEX", "Mexico"),
    "NG": ("NGA", "Nigeria"),
    "NL": ("NLD", "Netherlands"),
    "NO": ("NOR", "Norway"),
    "NZ": ("NZL", "New Zealand"),
    "PH": ("PHL", "Philippines"),
    "PK": ("PAK", "Pakistan"),
    "PL": ("POL", "Poland"),
    "PT": ("PRT", "Portugal"),
    "RO": ("ROU", "Romania"),
    "RU": ("RUS", "Russia"),
    "SE": ("SWE", "Sweden"),
    "SG": ("SGP", "Singapore"),
    "TH": ("THA", "Thailand"),
    "TR": ("TUR", "Turkey"),
    "UA": ("UKR", "Ukraine"),
    "VE": ("VEN", "Venezuela"),
    "VN": ("VNM", "Vietnam"),
    "ZA": ("ZAF", "South Africa"),
}


def country_iso3(code: str) -> str | None:
    """ISO-2 → ISO-3 (or None if unknown)."""
    entry = COUNTRIES.get(code.upper())
    return entry[0] if entry else None


def country_name(code: str) -> str | None:
    """ISO-2 → full name (or None if unknown)."""
    entry = COUNTRIES.get(code.upper())
    return entry[1] if entry else None


def country_adjective(code: str) -> str | None:
    """ISO-2 → demonym (e.g. 'Hungarian'). Returns None if unknown."""
    return COUNTRY_ADJECTIVES.get(code.upper())
