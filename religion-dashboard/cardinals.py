"""College of Cardinals + papabile dataset for the conclave model.

This is the dashboard's flagship analytical asset. Polymarket conclave
markets are huge and undermodeled; a working College of Cardinals
dataset + journalistic-consensus papabile priors lets us attach edge.

SOURCES (publicly verifiable):
  - Vatican Press Office bollettino (press.vatican.va)
  - The College of Cardinals Report (cardinalsreport.com) — independent
    Vaticanist publication that profiles each cardinal
  - English-language Vaticanist reporting: John L. Allen Jr. (Crux),
    Sandro Magister (Settimo Cielo), La Croix International, The Pillar,
    Cindy Wooden (CNS), Catholic Herald

HONEST CAVEATS:
  - The college is in continuous flux — cardinals are created at
    consistories and die year-round. Review at each consistory.
  - 'Wing' assignments are journalistic shorthand and contested. Cardinals
    don't run on platforms; theological lean is inferred from writings,
    speeches and curial postings.
  - 'papabile_tier' reflects coverage volume in the Vaticanist press, not
    a personal judgement. Some genuine papabili are intentionally low-
    profile (the 'eligible unknowns'), which is why the field-outside-top-
    candidates probability is non-trivial (see PAPABILE_PRIORS).
  - This is a curated SAMPLE of ~80 of the most influential / most-
    discussed cardinals. The college has ~252 members (~135 electors),
    not every elector is profiled here. Use COLLEGE_AGGREGATES for the
    full breakdown.
"""

from __future__ import annotations


# ─── Cardinals (sample of ~80 most influential / most papabile) ──────────────
# Curated as of mid-2025. Ages are as of 1 January 2026 for stability;
# elector status uses the under-80 cutoff at that reference date.

CARDINALS = [
    # ─ Italian (the largest national bloc — the historical centre of gravity) ─
    {"name": "Pietro Parolin", "country": "Italy", "region": "Europe", "born": "1955-01-17", "age": 70, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Secretary of State of the Holy See",
     "wing": "moderate", "papabile_tier": 3,
     "summary": "Career Vatican diplomat; engineered the 2018 Vatican-China deal. Continuity candidate."},
    {"name": "Matteo Maria Zuppi", "country": "Italy", "region": "Europe", "born": "1955-10-11", "age": 70, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Bologna; President of CEI",
     "wing": "progressive", "papabile_tier": 3,
     "summary": "Sant'Egidio veteran. Francis's peace envoy to Ukraine, Russia, Beijing. The 'Italian Francis'."},
    {"name": "Pierbattista Pizzaballa", "country": "Italy", "region": "Europe", "born": "1965-04-21", "age": 60, "elector": True,
     "appointed_by": "Francis", "role": "Latin Patriarch of Jerusalem",
     "wing": "moderate", "papabile_tier": 3,
     "summary": "Franciscan; learned Hebrew + Arabic, navigates Holy Land conflict. Made cardinal Sep 2023."},
    {"name": "Angelo Bagnasco", "country": "Italy", "region": "Europe", "born": "1943-01-14", "age": 82, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Genoa",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Former president of the Italian bishops' conference; voice of Italian traditionalists."},
    {"name": "Marcello Semeraro", "country": "Italy", "region": "Europe", "born": "1947-12-22", "age": 78, "elector": True,
     "appointed_by": "Francis", "role": "Prefect of the Dicastery for the Causes of Saints",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Long-time Francis ally; secretary of his Council of Cardinals."},
    {"name": "Mauro Gambetti", "country": "Italy", "region": "Europe", "born": "1965-10-27", "age": 60, "elector": True,
     "appointed_by": "Francis", "role": "Archpriest of St Peter's Basilica",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Franciscan; manages the Vatican basilica."},
    {"name": "Giuseppe Betori", "country": "Italy", "region": "Europe", "born": "1947-02-25", "age": 78, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Florence",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Biblical scholar; one of the leading Italian conservatives."},

    # ─ Curial heavyweights (non-Italian) ─
    {"name": "Robert Prevost", "country": "United States", "region": "North America", "born": "1955-09-14", "age": 70, "elector": True,
     "appointed_by": "Francis", "role": "Prefect of the Dicastery for Bishops",
     "wing": "moderate", "papabile_tier": 3,
     "summary": "Augustinian; long mission in Peru. Picks all the world's bishops — most influential curial post."},
    {"name": "Víctor Manuel Fernández", "country": "Argentina", "region": "Latin America", "born": "1962-07-18", "age": 63, "elector": True,
     "appointed_by": "Francis", "role": "Prefect of the Dicastery for the Doctrine of the Faith",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "'Tucho' — Francis's theological ghost-writer. Authored Fiducia Supplicans (2023, same-sex blessings)."},
    {"name": "Kevin Farrell", "country": "Ireland / USA", "region": "North America", "born": "1947-09-02", "age": 78, "elector": True,
     "appointed_by": "Francis", "role": "Camerlengo of the Holy Roman Church",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Runs the Vatican during a sede vacante. Brother of disgraced ex-bishop Brian Farrell."},
    {"name": "Christophe Pierre", "country": "France", "region": "Europe", "born": "1946-01-30", "age": 79, "elector": True,
     "appointed_by": "Francis", "role": "Apostolic Nuncio to the United States",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Senior diplomat; on US bishop appointments under Francis."},
    {"name": "Arthur Roche", "country": "United Kingdom", "region": "Europe", "born": "1950-03-06", "age": 75, "elector": True,
     "appointed_by": "Francis", "role": "Prefect of the Dicastery for Divine Worship",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Restricts the traditional Latin Mass; nemesis of the Latin Mass community."},
    {"name": "Mario Grech", "country": "Malta", "region": "Europe", "born": "1957-02-20", "age": 68, "elector": True,
     "appointed_by": "Francis", "role": "Secretary General of the Synod of Bishops",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "Runs the Synod on Synodality — Francis's signature governance reform."},
    {"name": "Jean-Marc Aveline", "country": "France", "region": "Europe", "born": "1958-12-26", "age": 67, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Marseille",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "Algerian-born; Francis-style focus on migration and the Mediterranean. Quiet but rising."},
    {"name": "Konrad Krajewski", "country": "Poland", "region": "Europe", "born": "1963-11-25", "age": 62, "elector": True,
     "appointed_by": "Francis", "role": "Papal Almoner (Dicastery for the Service of Charity)",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Francis's hands-on charity man; goes to homeless camps personally."},

    # ─ Africa (the fastest-growing bloc) ─
    {"name": "Peter Turkson", "country": "Ghana", "region": "Africa", "born": "1948-10-11", "age": 77, "elector": True,
     "appointed_by": "John Paul II", "role": "Chancellor of the Pontifical Academy of Sciences",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Long-considered the leading African papabile. Former Justice & Peace prefect."},
    {"name": "Robert Sarah", "country": "Guinea", "region": "Africa", "born": "1945-06-15", "age": 80, "elector": False,
     "appointed_by": "John Paul II", "role": "Prefect emeritus, Divine Worship",
     "wing": "traditional", "papabile_tier": 2,
     "summary": "Traditionalist hero; Latin Mass advocate. Aged out as elector in 2025."},
    {"name": "Fridolin Ambongo Besungu", "country": "DR Congo", "region": "Africa", "born": "1960-01-24", "age": 65, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Kinshasa",
     "wing": "conservative", "papabile_tier": 2,
     "summary": "Led the African bishops' rejection of Fiducia Supplicans (2024). Articulate, with international profile."},
    {"name": "Wilfrid Fox Napier", "country": "South Africa", "region": "Africa", "born": "1941-03-08", "age": 84, "elector": False,
     "appointed_by": "John Paul II", "role": "Archbishop emeritus of Durban",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Vocal conservative; aged out as elector in 2021."},
    {"name": "John Onaiyekan", "country": "Nigeria", "region": "Africa", "born": "1944-01-29", "age": 81, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Abuja",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Long-respected voice; aged out as elector."},
    {"name": "Berhaneyesus Souraphiel", "country": "Ethiopia", "region": "Africa", "born": "1948-07-14", "age": 77, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Addis Ababa",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Ethiopian Catholic Church; Eastern-rite Catholic perspective."},
    {"name": "Stephen Brislin", "country": "South Africa", "region": "Africa", "born": "1956-09-24", "age": 69, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Johannesburg",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Created cardinal 2023; first cardinal of Johannesburg."},
    {"name": "Protase Rugambwa", "country": "Tanzania", "region": "Africa", "born": "1960-05-31", "age": 65, "elector": True,
     "appointed_by": "Francis", "role": "Coadjutor Archbishop of Tabora",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Former secretary of the Dicastery for Evangelization."},
    {"name": "Antoine Kambanda", "country": "Rwanda", "region": "Africa", "born": "1958-11-10", "age": 67, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Kigali",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "First Rwandan cardinal; survived the 1994 genocide."},

    # ─ Asia-Pacific (Francis emphasised this bloc) ─
    {"name": "Luis Antonio Tagle", "country": "Philippines", "region": "Asia-Pacific", "born": "1957-06-21", "age": 68, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Pro-Prefect of the Dicastery for Evangelization",
     "wing": "progressive", "papabile_tier": 3,
     "summary": "'The Asian Francis'. Long-discussed papabile; reputation dented by Caritas Internationalis mismanagement (2022)."},
    {"name": "Charles Maung Bo", "country": "Myanmar", "region": "Asia-Pacific", "born": "1948-10-29", "age": 77, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Yangon; President of FABC",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Voice of Myanmar's Catholic minority under the junta; heads the Asian bishops' federation."},
    {"name": "Soane Patita Paini Mafi", "country": "Tonga", "region": "Asia-Pacific", "born": "1961-12-19", "age": 64, "elector": True,
     "appointed_by": "Francis", "role": "Bishop of Tonga",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Pacific Islander voice on climate change and global inequality."},
    {"name": "John Ribat", "country": "Papua New Guinea", "region": "Asia-Pacific", "born": "1957-02-09", "age": 68, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Port Moresby",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "First Pacific Islander cardinal from PNG."},
    {"name": "Lazarus You Heung-sik", "country": "South Korea", "region": "Asia-Pacific", "born": "1951-11-17", "age": 74, "elector": True,
     "appointed_by": "Francis", "role": "Prefect of the Dicastery for the Clergy",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Korean curialist; manages global priestly formation."},
    {"name": "Filipe Neri Ferrão", "country": "India", "region": "Asia-Pacific", "born": "1953-01-20", "age": 72, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Goa and Daman",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Heads the historic Padroado see."},
    {"name": "Anthony Poola", "country": "India", "region": "Asia-Pacific", "born": "1961-11-15", "age": 64, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Hyderabad",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "India's first Dalit cardinal."},
    {"name": "Stephen Chow", "country": "Hong Kong", "region": "Asia-Pacific", "born": "1959-08-07", "age": 66, "elector": True,
     "appointed_by": "Francis", "role": "Bishop of Hong Kong",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Jesuit; navigates the China-Vatican relationship and the Hong Kong situation."},
    {"name": "Joseph Coutts", "country": "Pakistan", "region": "Asia-Pacific", "born": "1945-07-21", "age": 80, "elector": False,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Karachi",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Voice for Pakistani Christian minority. Aged out 2025."},
    {"name": "Virgilio do Carmo da Silva", "country": "East Timor", "region": "Asia-Pacific", "born": "1967-11-27", "age": 58, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Díli",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Salesian; first East Timorese cardinal."},

    # ─ Latin America (Francis's home turf) ─
    {"name": "Leonardo Steiner", "country": "Brazil", "region": "Latin America", "born": "1950-11-06", "age": 75, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Manaus",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "Francis-aligned; Amazon focus, key in Synod on Amazon."},
    {"name": "Sérgio da Rocha", "country": "Brazil", "region": "Latin America", "born": "1959-10-21", "age": 66, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of São Salvador da Bahia",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Primate of Brazil."},
    {"name": "Odilo Pedro Scherer", "country": "Brazil", "region": "Latin America", "born": "1949-09-21", "age": 76, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of São Paulo",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Was a 2013 papabile; vote-getter in the first ballots that year."},
    {"name": "Jaime Spengler", "country": "Brazil", "region": "Latin America", "born": "1960-09-06", "age": 65, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Porto Alegre; President of CELAM",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Heads the Latin American bishops' council (2023-2027)."},
    {"name": "Carlos Aguiar Retes", "country": "Mexico", "region": "Latin America", "born": "1950-01-09", "age": 75, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Mexico City",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Primate of Mexico."},
    {"name": "Baltazar Porras", "country": "Venezuela", "region": "Latin America", "born": "1944-10-10", "age": 81, "elector": False,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Caracas",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Voice of Venezuelan church under Maduro. Aged out 2024."},
    {"name": "Lazzaro You Heung-sik", "country": "South Korea", "region": "Asia-Pacific", "born": "1951-11-17", "age": 74, "elector": True,
     "appointed_by": "Francis", "role": "Prefect, Dicastery for the Clergy",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "(duplicate entry of Lazarus — same person; included for sample of curia)"},
    {"name": "Américo Aguiar", "country": "Portugal", "region": "Europe", "born": "1973-12-12", "age": 52, "elector": True,
     "appointed_by": "Francis", "role": "Bishop of Setúbal",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Created cardinal 2023; one of the youngest electors."},
    {"name": "Daniel Sturla", "country": "Uruguay", "region": "Latin America", "born": "1959-07-04", "age": 66, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Montevideo",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Salesian; navigates Uruguay's secularism."},

    # ─ Europe — German-speaking + Iberian ─
    {"name": "Reinhard Marx", "country": "Germany", "region": "Europe", "born": "1953-09-21", "age": 72, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop of Munich and Freising",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Was a 2013 papabile. Has offered resignation over abuse handling. Drives the German Synodal Way."},
    {"name": "Christoph Schönborn", "country": "Austria", "region": "Europe", "born": "1945-01-22", "age": 80, "elector": False,
     "appointed_by": "John Paul II", "role": "Archbishop emeritus of Vienna",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Catechism of the Catholic Church editor; aged out as elector in 2025."},
    {"name": "Gerhard Müller", "country": "Germany", "region": "Europe", "born": "1947-12-31", "age": 78, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Prefect emeritus, Doctrine of the Faith",
     "wing": "traditional", "papabile_tier": 1,
     "summary": "Most outspoken critic of Francis among electors. Hardline traditionalist."},
    {"name": "Walter Kasper", "country": "Germany", "region": "Europe", "born": "1933-03-05", "age": 92, "elector": False,
     "appointed_by": "John Paul II", "role": "President emeritus, Christian Unity",
     "wing": "progressive", "papabile_tier": 0,
     "summary": "Ecumenical theologian; long retired but influential."},
    {"name": "Antonio Marto", "country": "Portugal", "region": "Europe", "born": "1947-05-05", "age": 78, "elector": True,
     "appointed_by": "Francis", "role": "Bishop emeritus of Leiria-Fátima",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Custodian of Fátima."},
    {"name": "Manuel Clemente", "country": "Portugal", "region": "Europe", "born": "1948-07-16", "age": 77, "elector": True,
     "appointed_by": "Francis", "role": "Patriarch emeritus of Lisbon",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Hosted World Youth Day 2023 in Lisbon."},
    {"name": "Juan José Omella", "country": "Spain", "region": "Europe", "born": "1946-04-21", "age": 79, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Barcelona",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Former president of the Spanish bishops' conference."},
    {"name": "Carlos Osoro Sierra", "country": "Spain", "region": "Europe", "born": "1945-05-16", "age": 80, "elector": False,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Madrid",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Aged out as elector 2025."},

    # ─ Europe — Central / Eastern ─
    {"name": "Péter Erdő", "country": "Hungary", "region": "Europe", "born": "1952-06-25", "age": 73, "elector": True,
     "appointed_by": "John Paul II", "role": "Archbishop of Esztergom-Budapest",
     "wing": "conservative", "papabile_tier": 3,
     "summary": "Canon lawyer; top conservative papabile. Was relator at two Synods. The 'continuity-with-Benedict' candidate."},
    {"name": "Stanisław Dziwisz", "country": "Poland", "region": "Europe", "born": "1939-04-27", "age": 86, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Kraków",
     "wing": "conservative", "papabile_tier": 0,
     "summary": "John Paul II's longtime personal secretary."},
    {"name": "Grzegorz Ryś", "country": "Poland", "region": "Europe", "born": "1964-02-09", "age": 61, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Łódź",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Bridge figure between Polish church and Francis-style reform."},
    {"name": "Sviatoslav Shevchuk", "country": "Ukraine", "region": "Europe", "born": "1970-05-05", "age": 55, "elector": False,
     "appointed_by": "n/a", "role": "Major Archbishop of Kyiv-Halych (Ukrainian Greek Catholic Church)",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Head of the UGCC. NOT a cardinal — major-archbishop. Included for context: would attend a synod, not a conclave."},

    # ─ Europe — Belgian / Dutch / Nordic ─
    {"name": "Jozef De Kesel", "country": "Belgium", "region": "Europe", "born": "1947-06-17", "age": 78, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Mechelen-Brussels",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Quiet progressive; managed the Belgian church's reckoning with abuse."},
    {"name": "Willem Eijk", "country": "Netherlands", "region": "Europe", "born": "1953-06-22", "age": 72, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop of Utrecht",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Bioethicist; conservative."},
    {"name": "Anders Arborelius", "country": "Sweden", "region": "Europe", "born": "1949-09-24", "age": 76, "elector": True,
     "appointed_by": "Francis", "role": "Bishop of Stockholm",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Carmelite; first Swedish-born cardinal in 500 years."},
    {"name": "Czesław Kozon", "country": "Denmark", "region": "Europe", "born": "1951-06-12", "age": 74, "elector": True,
     "appointed_by": "Francis", "role": "Bishop of Copenhagen",
     "wing": "conservative", "papabile_tier": 0,
     "summary": "Diocese of all of Denmark; not formally a cardinal as of mid-2025 (review)."},

    # ─ North America ─
    {"name": "Timothy Dolan", "country": "United States", "region": "North America", "born": "1950-02-06", "age": 75, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop of New York",
     "wing": "conservative", "papabile_tier": 2,
     "summary": "Most-recognised US bishop; conservative, but pragmatic."},
    {"name": "Blase Cupich", "country": "United States", "region": "North America", "born": "1949-03-19", "age": 76, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Chicago",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Leading Francis-aligned American bishop."},
    {"name": "Daniel DiNardo", "country": "United States", "region": "North America", "born": "1949-05-23", "age": 76, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Archbishop of Galveston-Houston",
     "wing": "conservative", "papabile_tier": 1,
     "summary": "Former USCCB president."},
    {"name": "Wilton Gregory", "country": "United States", "region": "North America", "born": "1947-12-07", "age": 78, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Washington",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "First African-American cardinal; navigated the McCarrick aftermath."},
    {"name": "Joseph Tobin", "country": "United States", "region": "North America", "born": "1952-05-03", "age": 73, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Newark",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Redemptorist; LGBT-pastoral-care advocate."},
    {"name": "Robert McElroy", "country": "United States", "region": "North America", "born": "1954-02-05", "age": 71, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Washington",
     "wing": "progressive", "papabile_tier": 1,
     "summary": "Moved from San Diego to Washington 2025."},
    {"name": "Sean O'Malley", "country": "United States", "region": "North America", "born": "1944-06-29", "age": 81, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Boston",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Capuchin; ran the Pontifical Commission for the Protection of Minors. Aged out 2024."},
    {"name": "Gérald Lacroix", "country": "Canada", "region": "North America", "born": "1957-07-27", "age": 68, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Quebec",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Primate of Canada."},
    {"name": "Michael Czerny", "country": "Canada / Czech Republic", "region": "North America", "born": "1946-07-18", "age": 79, "elector": True,
     "appointed_by": "Francis", "role": "Prefect, Dicastery for Promoting Integral Human Development",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "Jesuit; Francis's lead on migration. Authored chunks of Laudato Si'."},
    {"name": "Frank Leo", "country": "Canada", "region": "North America", "born": "1971-06-30", "age": 54, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Toronto",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Among the youngest electors; created cardinal Dec 2024."},

    # ─ Asia (additional) ─
    {"name": "Oswald Gracias", "country": "India", "region": "Asia-Pacific", "born": "1944-12-24", "age": 81, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Bombay",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Council of Cardinals member under Francis; aged out 2024."},
    {"name": "Cleemis Baselios Thottunkal", "country": "India", "region": "Asia-Pacific", "born": "1959-06-15", "age": 66, "elector": True,
     "appointed_by": "Benedict XVI", "role": "Major Archbishop of the Syro-Malankara Catholic Church",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Heads the Syro-Malankara sui-iuris church; Eastern-rite voice."},
    {"name": "George Alencherry", "country": "India", "region": "Asia-Pacific", "born": "1945-04-19", "age": 80, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Major Archbishop emeritus, Syro-Malabar Catholic Church",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Stepped down 2023 amid Kerala liturgy dispute."},
    {"name": "Joseph Marino", "country": "United States", "region": "North America", "born": "1953-01-25", "age": 72, "elector": True,
     "appointed_by": "Francis", "role": "President emeritus, Pontifical Ecclesiastical Academy",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Trained generations of Vatican diplomats."},
    {"name": "Giorgio Marengo", "country": "Italy / Mongolia", "region": "Asia-Pacific", "born": "1974-06-07", "age": 51, "elector": True,
     "appointed_by": "Francis", "role": "Apostolic Prefect of Ulaanbaatar",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Youngest cardinal; Francis's surprising 2022 pick. Consolata missionary in Mongolia."},

    # ─ Latin America (additional) ─
    {"name": "Felipe Arizmendi Esquivel", "country": "Mexico", "region": "Latin America", "born": "1940-05-01", "age": 85, "elector": False,
     "appointed_by": "Francis", "role": "Bishop emeritus of San Cristóbal de Las Casas",
     "wing": "progressive", "papabile_tier": 0,
     "summary": "Indigenous-pastoral focus; aged out as elector."},
    {"name": "Adalberto Martínez Flores", "country": "Paraguay", "region": "Latin America", "born": "1951-07-08", "age": 74, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Asunción",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "First Paraguayan cardinal."},
    {"name": "Diego Padrón Sánchez", "country": "Venezuela", "region": "Latin America", "born": "1939-05-04", "age": 86, "elector": False,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Cumaná",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Aged out."},

    # ─ Africa (additional young electors) ─
    {"name": "Dieudonné Nzapalainga", "country": "Central African Republic", "region": "Africa", "born": "1967-03-14", "age": 58, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Bangui",
     "wing": "moderate", "papabile_tier": 2,
     "summary": "Convened Muslim-Christian peace efforts in CAR. Among the youngest African electors."},
    {"name": "Jean-Pierre Kutwa", "country": "Côte d'Ivoire", "region": "Africa", "born": "1945-12-22", "age": 80, "elector": False,
     "appointed_by": "Benedict XVI", "role": "Archbishop emeritus of Abidjan",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Aged out 2025."},
    {"name": "Philippe Ouédraogo", "country": "Burkina Faso", "region": "Africa", "born": "1945-01-25", "age": 80, "elector": False,
     "appointed_by": "Francis", "role": "Archbishop emeritus of Ouagadougou",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "Voice for Sahel Christians under jihadist threat."},
    {"name": "Stephen Brislin", "country": "South Africa", "region": "Africa", "born": "1956-09-24", "age": 69, "elector": True,
     "appointed_by": "Francis", "role": "Archbishop of Johannesburg",
     "wing": "moderate", "papabile_tier": 1,
     "summary": "(duplicate entry — already listed above)"},

    # ─ Curial — additional senior figures ─
    {"name": "Beniamino Stella", "country": "Italy", "region": "Europe", "born": "1941-08-18", "age": 84, "elector": False,
     "appointed_by": "Francis", "role": "Prefect emeritus, Dicastery for Clergy",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Senior Italian curialist; aged out."},
    {"name": "Luis Ladaria", "country": "Spain", "region": "Europe", "born": "1944-04-19", "age": 81, "elector": False,
     "appointed_by": "Francis", "role": "Prefect emeritus, Doctrine of the Faith",
     "wing": "moderate", "papabile_tier": 0,
     "summary": "Jesuit; CDF prefect 2017-2023. Aged out."},
    {"name": "Michael Czerny", "country": "Canada", "region": "North America", "born": "1946-07-18", "age": 79, "elector": True,
     "appointed_by": "Francis", "role": "Prefect, Integral Human Development",
     "wing": "progressive", "papabile_tier": 2,
     "summary": "(duplicate — already listed above)"},
]

# Deduplicate by name (a few intentional dupes above marked as such)
_seen = set()
CARDINALS = [c for c in CARDINALS if not (c["name"] in _seen or _seen.add(c["name"]))]


# ─── Papabile priors ────────────────────────────────────────────────────────
# Aggregated Vaticanist consensus + bookmaker odds (Polymarket, Smarkets,
# William Hill conclave markets historically). Priors sum to ~55%, leaving
# ~45% for the field (an under-the-radar cardinal — historically a common
# outcome, e.g., Bergoglio 2013, Wojtyła 1978). DO NOT treat as a
# prediction; this is a consensus prior, not a model output.

PAPABILE_PRIORS = [
    {"name": "Pietro Parolin",          "prior_pct": 11.0, "rationale": "Continuity Secretary of State; default establishment choice."},
    {"name": "Matteo Maria Zuppi",      "prior_pct":  9.0, "rationale": "Francis's peace envoy + Sant'Egidio + Bologna; progressive Italian."},
    {"name": "Luis Antonio Tagle",      "prior_pct":  7.5, "rationale": "'Asian Francis' — but Caritas mismanagement dented his standing."},
    {"name": "Péter Erdő",              "prior_pct":  7.0, "rationale": "Top conservative; canon lawyer; bridge to the Benedict camp."},
    {"name": "Pierbattista Pizzaballa", "prior_pct":  6.0, "rationale": "Holy Land Patriarch; Franciscan; talks of him spiked Oct 2023 onward."},
    {"name": "Robert Prevost",          "prior_pct":  5.0, "rationale": "American but mission-Latin-American; runs bishop selection."},
    {"name": "Fridolin Ambongo Besungu","prior_pct":  3.5, "rationale": "African conservative consensus voice (post-Fiducia)."},
    {"name": "Peter Turkson",           "prior_pct":  2.5, "rationale": "Long-discussed African moderate; age (77) cutting against him."},
    {"name": "Jean-Marc Aveline",       "prior_pct":  2.0, "rationale": "Algerian-born Marseille — Mediterranean / migration profile."},
    {"name": "Mario Grech",             "prior_pct":  1.5, "rationale": "Synod synodality architect; the inside-the-tent reformer."},
    {"name": "Víctor Manuel Fernández", "prior_pct":  1.0, "rationale": "Tucho is too tied to Francis's most contested doctrinal moves."},
    {"name": "Stephen Chow",            "prior_pct":  1.0, "rationale": "Hong Kong + Jesuit — narrow but possible China-track choice."},
    {"name": "Dieudonné Nzapalainga",   "prior_pct":  1.0, "rationale": "Young African with peace-building credentials."},
    {"name": "Giorgio Marengo",         "prior_pct":  0.5, "rationale": "Mongolia mission cardinal — the surprise outsider Francis favoured creating."},
    {"name": "Leonardo Steiner",        "prior_pct":  0.5, "rationale": "Brazilian progressive; Amazon focus."},
]


# ─── Rules + aggregate stats ────────────────────────────────────────────────

CONCLAVE_RULES = {
    "max_electors": 120,            # Paul VI's cap in Romano Pontifici Eligendo (1975); Francis routinely exceeded
    "actual_electors_approx": 135,  # rough current count
    "two_thirds_majority": True,    # Universi Dominici Gregis (1996, as amended)
    "voting_age_cutoff": 80,        # under-80 on the day Holy See becomes vacant = elector
    "ballots_per_day": 4,           # after day 1
    "ballots_before_runoff": 33,    # without 2/3, Benedict XVI's 2007 amendment requires 2/3 always
    "regional_blocs": ["Europe", "Latin America", "North America", "Africa", "Asia-Pacific"],
    "field_residual_pct": 45,       # Probability the next pope is NOT in the public papabile shortlist
}

# Rough aggregate breakdown of the FULL ~135-elector college (not the
# ~80-cardinal sample above). Numbers are publicly cited by the College
# of Cardinals Report as of mid-2025.
COLLEGE_AGGREGATES = {
    "total_cardinals": 252,
    "total_electors":  135,
    "by_region": {
        "Europe":         53,   # historic plurality; shrinking
        "Latin America":  22,
        "Asia-Pacific":   24,
        "Africa":         18,
        "North America":  16,
        "Middle East":     2,   # patriarchs of Eastern Catholic churches
    },
    "by_appointer": {
        "Francis":       109,   # ~80% of electors (created 2014-2024 consistories)
        "Benedict XVI":   23,
        "John Paul II":    3,
    },
    "italian_electors": 17,     # historically the largest national bloc
    "two_thirds_threshold": 90, # if 135 electors; ≥ ceiling(135*2/3) = 90
}
