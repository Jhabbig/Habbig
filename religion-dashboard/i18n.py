"""UI translation dictionary for the religion dashboard.

Translates panel titles, button labels, column headers, and footnote
strings. Data fields (cult summaries, leader bios, market questions)
are NOT translated — those would require human translators for ~190
entries each.

Languages: en (English, base), es (Spanish), it (Italian), fr (French),
pt (Portuguese). All five are major religious-affairs reporting
languages with significant Catholic + Pentecostal audiences.

Lookups fall back to English when a key is missing in the target
language.

USAGE
    from i18n import t, available_languages
    t("conclave.title", "es")  →  "Modelo del cónclave — Colegio de Cardenales"
    available_languages()      →  ["en", "es", "it", "fr", "pt"]
"""

from __future__ import annotations

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # Header
        "header.title":               "Religion & Cults Tracker",
        "header.loading":             "loading…",
        # Panels
        "panel.summary":              "Global snapshot",
        "panel.leaders":              "Religious leaders — actuarial",
        "panel.conclave":             "Conclave model — College of Cardinals + papabile",
        "panel.calendar":             "Upcoming religious calendar",
        "panel.religions":            "World religions — adherents (millions)",
        "panel.registry":             "Full registry — traditions, sects & movements",
        "panel.cults":                "New religious movements & cults — watchlist",
        "panel.countries":            "Country religion composition",
        "panel.violence":             "Religious-violence events — ACLED, last 30 days",
        "panel.sentinel":             "Cult / NRM emergence sentinel — Reddit",
        "panel.freedom":              "Religious-freedom designations (USCIRF 2024)",
        "panel.markets":              "Polymarket — religion-tagged markets",
        "panel.edge":                 "Edge — model vs market",
        "panel.news":                 "Religion news — live feed",
        # Common UI
        "ui.all":                     "All",
        "ui.search":                  "Search…",
        "ui.electors_only":           "Electors only",
        "ui.region":                  "Region",
        "ui.appointer":               "Appointer",
        "ui.source":                  "source",
        # Conclave fields
        "conclave.total":             "Total cardinals (college)",
        "conclave.electors":          "Total electors (<80)",
        "conclave.enumerated":        "Enumerated here",
        "conclave.francis_created":   "Created by Francis",
        "conclave.two_thirds":        "2/3 majority",
        "conclave.papabile_sum":      "Papabile priors sum",
        "conclave.field":             "Field residual",
        "conclave.papabile_heading":  "Papabile — Vaticanist consensus priors",
        "conclave.cardinals_heading": "Cardinal-electors",
        # Leaders
        "leaders.p_dies_1y":          "P(dies 1y)",
        "leaders.p_alive_5y":         "P(alive 5y)",
        "leaders.p_alive_10y":        "P(alive 10y)",
        "leaders.succession":         "Succession",
        "leaders.recent_news":        "Recent news",
        # Pope health
        "popehealth.label":           "Pope health signal",
        "popehealth.quiet":           "quiet",
        "popehealth.elevated":        "elevated",
        "popehealth.high":            "high",
        "popehealth.critical":        "critical",
    },
    "es": {
        "header.title":               "Rastreador de Religión y Sectas",
        "header.loading":             "cargando…",
        "panel.summary":              "Resumen global",
        "panel.leaders":              "Líderes religiosos — actuarial",
        "panel.conclave":             "Modelo del cónclave — Colegio de Cardenales + papables",
        "panel.calendar":             "Calendario religioso próximo",
        "panel.religions":            "Religiones del mundo — adeptos (millones)",
        "panel.registry":             "Registro completo — tradiciones, sectas y movimientos",
        "panel.cults":                "Nuevos movimientos religiosos y sectas — vigilancia",
        "panel.countries":            "Composición religiosa por país",
        "panel.violence":             "Eventos de violencia religiosa — ACLED, últimos 30 días",
        "panel.sentinel":             "Centinela de sectas emergentes — Reddit",
        "panel.freedom":              "Designaciones de libertad religiosa (USCIRF 2024)",
        "panel.markets":              "Polymarket — mercados de religión",
        "panel.edge":                 "Ventaja — modelo vs mercado",
        "panel.news":                 "Noticias de religión — en vivo",
        "ui.all":                     "Todos",
        "ui.search":                  "Buscar…",
        "ui.electors_only":           "Solo electores",
        "ui.region":                  "Región",
        "ui.appointer":               "Nombrador",
        "ui.source":                  "fuente",
        "conclave.total":             "Total de cardenales (colegio)",
        "conclave.electors":          "Total de electores (<80)",
        "conclave.enumerated":        "Enumerados aquí",
        "conclave.francis_created":   "Creados por Francisco",
        "conclave.two_thirds":        "Mayoría 2/3",
        "conclave.papabile_sum":      "Suma de probabilidades papables",
        "conclave.field":             "Residuo del campo",
        "conclave.papabile_heading":  "Papables — consenso vaticanista",
        "conclave.cardinals_heading": "Cardenales electores",
        "leaders.p_dies_1y":          "P(muere 1a)",
        "leaders.p_alive_5y":         "P(vivo 5a)",
        "leaders.p_alive_10y":        "P(vivo 10a)",
        "leaders.succession":         "Sucesión",
        "leaders.recent_news":        "Noticias recientes",
        "popehealth.label":           "Señal de salud del Papa",
        "popehealth.quiet":           "tranquila",
        "popehealth.elevated":        "elevada",
        "popehealth.high":            "alta",
        "popehealth.critical":        "crítica",
    },
    "it": {
        "header.title":               "Tracker di Religione e Sette",
        "header.loading":             "caricamento…",
        "panel.summary":              "Quadro globale",
        "panel.leaders":              "Leader religiosi — attuariale",
        "panel.conclave":             "Modello del conclave — Collegio Cardinalizio + papabili",
        "panel.calendar":             "Calendario religioso prossimo",
        "panel.religions":            "Religioni del mondo — fedeli (milioni)",
        "panel.registry":             "Registro completo — tradizioni, sette e movimenti",
        "panel.cults":                "Nuovi movimenti religiosi e sette — lista di sorveglianza",
        "panel.countries":            "Composizione religiosa per paese",
        "panel.violence":             "Eventi di violenza religiosa — ACLED, ultimi 30 giorni",
        "panel.sentinel":             "Sentinella sette emergenti — Reddit",
        "panel.freedom":              "Designazioni di libertà religiosa (USCIRF 2024)",
        "panel.markets":              "Polymarket — mercati su religione",
        "panel.edge":                 "Vantaggio — modello vs mercato",
        "panel.news":                 "Notizie di religione — live",
        "ui.all":                     "Tutti",
        "ui.search":                  "Cerca…",
        "ui.electors_only":           "Solo elettori",
        "ui.region":                  "Regione",
        "ui.appointer":               "Nominato da",
        "ui.source":                  "fonte",
        "conclave.total":             "Cardinali totali (collegio)",
        "conclave.electors":          "Elettori totali (<80)",
        "conclave.enumerated":        "Enumerati qui",
        "conclave.francis_created":   "Creati da Francesco",
        "conclave.two_thirds":        "Maggioranza 2/3",
        "conclave.papabile_sum":      "Somma probabilità papabili",
        "conclave.field":             "Residuo del campo",
        "conclave.papabile_heading":  "Papabili — consenso vaticanista",
        "conclave.cardinals_heading": "Cardinali elettori",
        "leaders.p_dies_1y":          "P(muore 1a)",
        "leaders.p_alive_5y":         "P(vivo 5a)",
        "leaders.p_alive_10y":        "P(vivo 10a)",
        "leaders.succession":         "Successione",
        "leaders.recent_news":        "Notizie recenti",
        "popehealth.label":           "Segnale di salute del Papa",
        "popehealth.quiet":           "tranquillo",
        "popehealth.elevated":        "elevato",
        "popehealth.high":            "alto",
        "popehealth.critical":        "critico",
    },
    "fr": {
        "header.title":               "Suivi des Religions et Sectes",
        "header.loading":             "chargement…",
        "panel.summary":              "Aperçu mondial",
        "panel.leaders":              "Dirigeants religieux — actuariat",
        "panel.conclave":             "Modèle du conclave — Collège des cardinaux + papables",
        "panel.calendar":             "Prochain calendrier religieux",
        "panel.religions":            "Religions du monde — fidèles (millions)",
        "panel.registry":             "Registre complet — traditions, sectes et mouvements",
        "panel.cults":                "Nouveaux mouvements religieux et sectes — liste de surveillance",
        "panel.countries":            "Composition religieuse par pays",
        "panel.violence":             "Événements de violence religieuse — ACLED, 30 derniers jours",
        "panel.sentinel":             "Sentinelle des sectes émergentes — Reddit",
        "panel.freedom":              "Désignations de liberté religieuse (USCIRF 2024)",
        "panel.markets":              "Polymarket — marchés religieux",
        "panel.edge":                 "Avantage — modèle vs marché",
        "panel.news":                 "Actualités religieuses — en direct",
        "ui.all":                     "Tous",
        "ui.search":                  "Rechercher…",
        "ui.electors_only":           "Électeurs seulement",
        "ui.region":                  "Région",
        "ui.appointer":               "Nommé par",
        "ui.source":                  "source",
        "conclave.total":             "Cardinaux totaux (collège)",
        "conclave.electors":          "Électeurs totaux (<80)",
        "conclave.enumerated":        "Énumérés ici",
        "conclave.francis_created":   "Créés par François",
        "conclave.two_thirds":        "Majorité 2/3",
        "conclave.papabile_sum":      "Somme des probabilités papables",
        "conclave.field":             "Reste du champ",
        "conclave.papabile_heading":  "Papables — consensus vaticaniste",
        "conclave.cardinals_heading": "Cardinaux électeurs",
        "leaders.p_dies_1y":          "P(décès 1a)",
        "leaders.p_alive_5y":         "P(vivant 5a)",
        "leaders.p_alive_10y":        "P(vivant 10a)",
        "leaders.succession":         "Succession",
        "leaders.recent_news":        "Actualités récentes",
        "popehealth.label":           "Signal de santé du Pape",
        "popehealth.quiet":           "calme",
        "popehealth.elevated":        "élevé",
        "popehealth.high":            "haut",
        "popehealth.critical":        "critique",
    },
    "pt": {
        "header.title":               "Rastreador de Religião e Seitas",
        "header.loading":             "carregando…",
        "panel.summary":              "Panorama global",
        "panel.leaders":              "Líderes religiosos — atuarial",
        "panel.conclave":             "Modelo do conclave — Colégio dos Cardeais + papáveis",
        "panel.calendar":             "Próximo calendário religioso",
        "panel.religions":            "Religiões do mundo — adeptos (milhões)",
        "panel.registry":             "Registro completo — tradições, seitas e movimentos",
        "panel.cults":                "Novos movimentos religiosos e seitas — vigilância",
        "panel.countries":            "Composição religiosa por país",
        "panel.violence":             "Eventos de violência religiosa — ACLED, últimos 30 dias",
        "panel.sentinel":             "Sentinela de seitas emergentes — Reddit",
        "panel.freedom":              "Designações de liberdade religiosa (USCIRF 2024)",
        "panel.markets":              "Polymarket — mercados religiosos",
        "panel.edge":                 "Vantagem — modelo vs mercado",
        "panel.news":                 "Notícias de religião — ao vivo",
        "ui.all":                     "Todos",
        "ui.search":                  "Buscar…",
        "ui.electors_only":           "Apenas eleitores",
        "ui.region":                  "Região",
        "ui.appointer":               "Nomeador",
        "ui.source":                  "fonte",
        "conclave.total":             "Total de cardeais (colégio)",
        "conclave.electors":          "Total de eleitores (<80)",
        "conclave.enumerated":        "Enumerados aqui",
        "conclave.francis_created":   "Criados por Francisco",
        "conclave.two_thirds":        "Maioria 2/3",
        "conclave.papabile_sum":      "Soma de probabilidades papáveis",
        "conclave.field":             "Residual do campo",
        "conclave.papabile_heading":  "Papáveis — consenso vaticanista",
        "conclave.cardinals_heading": "Cardeais eleitores",
        "leaders.p_dies_1y":          "P(morre 1a)",
        "leaders.p_alive_5y":         "P(vivo 5a)",
        "leaders.p_alive_10y":        "P(vivo 10a)",
        "leaders.succession":         "Sucessão",
        "leaders.recent_news":        "Notícias recentes",
        "popehealth.label":           "Sinal de saúde do Papa",
        "popehealth.quiet":           "tranquilo",
        "popehealth.elevated":        "elevado",
        "popehealth.high":            "alto",
        "popehealth.critical":        "crítico",
    },
}


def available_languages() -> list[str]:
    return list(TRANSLATIONS.keys())


def t(key: str, lang: str = "en") -> str:
    """Translate a key. Falls back to English, then to the key itself."""
    return (TRANSLATIONS.get(lang) or {}).get(key) \
        or TRANSLATIONS["en"].get(key) \
        or key


def all_strings(lang: str = "en") -> dict[str, str]:
    """Full translation dict for a language (with English fallback for missing keys)."""
    base = dict(TRANSLATIONS["en"])
    base.update(TRANSLATIONS.get(lang) or {})
    return base
