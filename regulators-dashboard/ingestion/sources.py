"""Master source registry for v2.0 — global regulator coverage.

Single declaration site for every RSS-shaped source. Add new ones here
and they automatically flow through the full v0.1 → v1.5 pipeline
(classifier, severity, topics, market match, heatmap, stance, diff,
RSS feed, email digest) — no other code changes needed.

==============================================================================
  URL VERIFICATION CAVEAT
  Every URL below is best-guess against common regulator RSS conventions.
  Many bodies re-organize their feed paths on annual website refreshes.
  If a source goes red in `/api/feed`, drop the live URL (lift from each
  body's homepage) into the matching entry — no other change required.
  The graceful-degradation lane in `_rss.fetch_source` means a wrong URL
  just shows the source as `unavailable` in the per-source status row.
==============================================================================

Jurisdiction codes (ISO 3166-1 alpha-2 mostly, plus EU/UK):

  US, UK, EU, DE, CH, FR, IT, SG, HK, AU, IN, JP, BR, CA

To extend with a new body:
  1. Append an `RssSource(...)` to `SOURCES` below.
  2. Make sure the jurisdiction code has a badge color in `index.html`
     (`.jx.XX { background: ...; color: ... }`) and a filter chip if you
     want UI filtering.
  3. If the body's name or notable people aren't in
     `analysis/market_match.ANCHOR_TOKENS`, add them so prediction-market
     matching can join.

To extend with a new SCRAPED (non-RSS) source: write a separate ingestion
module (mirror `ingestion/ofac_sdn.py` for the pattern) — don't shoehorn
HTML scraping into this list.
"""

from __future__ import annotations

from ._rss import RssSource


SOURCES: list[RssSource] = [
    # ── United States ──────────────────────────────────────────────────────
    RssSource(
        code="SEC",
        name="U.S. Securities and Exchange Commission",
        jurisdiction="US",
        rss_url="https://www.sec.gov/news/pressreleases.rss",
    ),
    RssSource(
        code="SEC-LIT",
        name="SEC Litigation Releases",
        jurisdiction="US",
        rss_url="https://www.sec.gov/rss/litigation/litreleases.xml",
    ),
    RssSource(
        code="CFTC",
        name="Commodity Futures Trading Commission",
        jurisdiction="US",
        rss_url="https://www.cftc.gov/PressRoom/PressReleases/cftc_press_releases.xml",
    ),
    RssSource(
        code="FinCEN",
        name="Financial Crimes Enforcement Network",
        jurisdiction="US",
        rss_url="https://www.fincen.gov/news-room/rss.xml",
    ),
    RssSource(
        code="OCC",
        name="Office of the Comptroller of the Currency",
        jurisdiction="US",
        rss_url="https://www.occ.gov/rss/occ_nr.xml",
    ),
    RssSource(
        code="FDIC",
        name="Federal Deposit Insurance Corporation",
        jurisdiction="US",
        rss_url="https://www.fdic.gov/news/press-releases/rss.xml",
    ),
    RssSource(
        code="CFPB",
        name="Consumer Financial Protection Bureau",
        jurisdiction="US",
        rss_url="https://www.consumerfinance.gov/about-us/newsroom/feed/",
    ),
    RssSource(
        code="OFAC",
        name="Office of Foreign Assets Control (recent actions)",
        jurisdiction="US",
        rss_url="https://ofac.treasury.gov/recent-actions/rss.xml",
    ),

    # ── United Kingdom ─────────────────────────────────────────────────────
    RssSource(
        code="FCA",
        name="Financial Conduct Authority",
        jurisdiction="UK",
        rss_url="https://www.fca.org.uk/news/rss.xml",
    ),
    RssSource(
        code="PRA",
        name="Prudential Regulation Authority (via BoE)",
        jurisdiction="UK",
        rss_url="https://www.bankofengland.co.uk/rss/news?taxonomies=4a55be37-ed53-4ad6-baee-43ecf6b86df0",
    ),
    RssSource(
        code="BoE",
        name="Bank of England (news)",
        jurisdiction="UK",
        rss_url="https://www.bankofengland.co.uk/rss/news",
    ),

    # ── European Union ─────────────────────────────────────────────────────
    RssSource(
        code="ESMA",
        name="European Securities and Markets Authority",
        jurisdiction="EU",
        rss_url="https://www.esma.europa.eu/press-news/esma-news/rss.xml",
    ),
    RssSource(
        code="EBA",
        name="European Banking Authority",
        jurisdiction="EU",
        rss_url="https://www.eba.europa.eu/rss.xml",
    ),
    RssSource(
        code="EIOPA",
        name="European Insurance and Occupational Pensions Authority",
        jurisdiction="EU",
        rss_url="https://www.eiopa.europa.eu/rss.xml",
    ),
    RssSource(
        code="ECB",
        name="European Central Bank (press)",
        jurisdiction="EU",
        rss_url="https://www.ecb.europa.eu/rss/press.xml",
    ),

    # ── Continental Europe (national) ──────────────────────────────────────
    RssSource(
        code="BaFin",
        name="Bundesanstalt für Finanzdienstleistungsaufsicht (DE)",
        jurisdiction="DE",
        rss_url="https://www.bafin.de/SharedDocs/RSS/EN/Newsfeed_Pressemitteilungen_en.xml",
    ),
    RssSource(
        code="FINMA",
        name="Swiss Financial Market Supervisory Authority",
        jurisdiction="CH",
        rss_url="https://www.finma.ch/en/news/news-rss/",
    ),
    RssSource(
        code="AMF",
        name="Autorité des marchés financiers (FR)",
        jurisdiction="FR",
        rss_url="https://www.amf-france.org/en/rss/news",
    ),
    RssSource(
        code="CONSOB",
        name="Commissione Nazionale per le Società e la Borsa (IT)",
        jurisdiction="IT",
        rss_url="https://www.consob.it/web/consob-and-its-activities/news/rss",
    ),

    # ── Asia-Pacific ───────────────────────────────────────────────────────
    RssSource(
        code="MAS",
        name="Monetary Authority of Singapore",
        jurisdiction="SG",
        rss_url="https://www.mas.gov.sg/api/rss/news",
    ),
    RssSource(
        code="HKMA",
        name="Hong Kong Monetary Authority",
        jurisdiction="HK",
        rss_url="https://www.hkma.gov.hk/eng/rss/press-releases.xml",
    ),
    RssSource(
        code="SFC-HK",
        name="Securities and Futures Commission (HK)",
        jurisdiction="HK",
        rss_url="https://apps.sfc.hk/edistributionWeb/rss/EN",
    ),
    RssSource(
        code="ASIC",
        name="Australian Securities and Investments Commission",
        jurisdiction="AU",
        rss_url="https://asic.gov.au/about-asic/news-centre/find-a-media-release/rss/",
    ),
    RssSource(
        code="RBA",
        name="Reserve Bank of Australia (media releases)",
        jurisdiction="AU",
        rss_url="https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
    ),
    RssSource(
        code="SEBI",
        name="Securities and Exchange Board of India",
        jurisdiction="IN",
        rss_url="https://www.sebi.gov.in/sebirss.xml",
    ),
    RssSource(
        code="RBI",
        name="Reserve Bank of India (press releases)",
        jurisdiction="IN",
        rss_url="https://www.rbi.org.in/pressreleases_rss.xml",
    ),

    # ── Americas (outside US) ──────────────────────────────────────────────
    RssSource(
        code="OSC",
        name="Ontario Securities Commission (CA)",
        jurisdiction="CA",
        rss_url="https://www.osc.ca/en/news-events/rss/news",
    ),
    RssSource(
        code="CVM",
        name="Comissão de Valores Mobiliários (BR)",
        jurisdiction="BR",
        rss_url="https://www.gov.br/cvm/pt-br/rss/noticias.xml",
    ),
    RssSource(
        code="CNBV",
        name="Comisión Nacional Bancaria y de Valores (MX)",
        jurisdiction="MX",
        rss_url="https://www.gob.mx/cnbv/rss",
    ),
    RssSource(
        code="CMF-CL",
        name="Comisión para el Mercado Financiero (CL)",
        jurisdiction="CL",
        rss_url="https://www.cmfchile.cl/rss/feed-novedades.xml",
    ),
    RssSource(
        code="SFC-CO",
        name="Superintendencia Financiera de Colombia",
        jurisdiction="CO",
        rss_url="https://www.superfinanciera.gov.co/jsp/loader.jsf?lServicio=Publicaciones&lFuncion=loadContenidoRSS",
    ),

    # ── More Europe (national) ─────────────────────────────────────────────
    RssSource(
        code="CNMV",
        name="Comisión Nacional del Mercado de Valores (ES)",
        jurisdiction="ES",
        rss_url="https://www.cnmv.es/portal/rss/UltimasNoticiasEn.aspx",
    ),
    RssSource(
        code="AFM",
        name="Autoriteit Financiële Markten (NL)",
        jurisdiction="NL",
        rss_url="https://www.afm.nl/en/over-afm/nieuws/rss",
    ),
    RssSource(
        code="DNB",
        name="De Nederlandsche Bank (NL)",
        jurisdiction="NL",
        rss_url="https://www.dnb.nl/en/general-news/news/rss/",
    ),
    RssSource(
        code="FI-SE",
        name="Finansinspektionen (SE)",
        jurisdiction="SE",
        rss_url="https://www.fi.se/en/rss/all-news/",
    ),
    RssSource(
        code="FTNO",
        name="Finanstilsynet (NO)",
        jurisdiction="NO",
        rss_url="https://www.finanstilsynet.no/en/rss/news/",
    ),
    RssSource(
        code="KNF",
        name="Komisja Nadzoru Finansowego (PL)",
        jurisdiction="PL",
        rss_url="https://www.knf.gov.pl/feed/Komunikaty.xml",
    ),
    RssSource(
        code="CSSF",
        name="Commission de Surveillance du Secteur Financier (LU)",
        jurisdiction="LU",
        rss_url="https://www.cssf.lu/en/news/feed/",
    ),
    RssSource(
        code="FSMA-BE",
        name="Financial Services and Markets Authority (BE)",
        jurisdiction="BE",
        rss_url="https://www.fsma.be/en/rss.xml",
    ),
    RssSource(
        code="OENB",
        name="Oesterreichische Nationalbank (AT)",
        jurisdiction="AT",
        rss_url="https://www.oenb.at/en/rss/news.xml",
    ),

    # ── More APAC ──────────────────────────────────────────────────────────
    RssSource(
        code="FSC-KR",
        name="Financial Services Commission (KR)",
        jurisdiction="KR",
        rss_url="https://www.fsc.go.kr/eng/rss/pr010101.xml",
    ),
    RssSource(
        code="FSC-TW",
        name="Financial Supervisory Commission (TW)",
        jurisdiction="TW",
        rss_url="https://www.fsc.gov.tw/en/rss.xml",
    ),
    RssSource(
        code="SEC-TH",
        name="Securities and Exchange Commission (TH)",
        jurisdiction="TH",
        rss_url="https://www.sec.or.th/EN/Pages/News/RSS.aspx",
    ),
    RssSource(
        code="BNM",
        name="Bank Negara Malaysia",
        jurisdiction="MY",
        rss_url="https://www.bnm.gov.my/web/guest/rss/-/asset_publisher/En4izWLQwR1H/rss",
    ),
    # JFSA (Japan) is non-RSS — see `ingestion/jfsa_scraper.py`. It's not
    # in this list; `unified_feed` iterates the scraped sources separately.

    # ── Middle East & Africa ───────────────────────────────────────────────
    RssSource(
        code="CMA-SA",
        name="Capital Market Authority (SA)",
        jurisdiction="SA",
        rss_url="https://cma.org.sa/en/Market/News/Pages/Rss.aspx",
    ),
    RssSource(
        code="SCA-AE",
        name="Securities and Commodities Authority (AE)",
        jurisdiction="AE",
        rss_url="https://www.sca.gov.ae/en/media-center/news/rss.aspx",
    ),
    RssSource(
        code="DFSA",
        name="Dubai Financial Services Authority",
        jurisdiction="AE",
        rss_url="https://www.dfsa.ae/news/rss",
    ),
    RssSource(
        code="ISA-IL",
        name="Israel Securities Authority",
        jurisdiction="IL",
        rss_url="https://www.isa.gov.il/sites/ISAEng/_layouts/15/Lists/Announcements/rss.aspx",
    ),
    RssSource(
        code="FSCA",
        name="Financial Sector Conduct Authority (ZA)",
        jurisdiction="ZA",
        rss_url="https://www.fsca.co.za/News%20Documents/Forms/AllItems.atom",
    ),

    # ── International / supranational ──────────────────────────────────────
    RssSource(
        code="FATF",
        name="Financial Action Task Force",
        jurisdiction="INTL",
        rss_url="https://www.fatf-gafi.org/en/publications/_jcr_content.feed",
    ),
    RssSource(
        code="BIS",
        name="Bank for International Settlements",
        jurisdiction="INTL",
        rss_url="https://www.bis.org/list/press_releases/from_01012020/index.rss",
    ),
    RssSource(
        code="IOSCO",
        name="International Organization of Securities Commissions",
        jurisdiction="INTL",
        rss_url="https://www.iosco.org/news/feed/",
    ),
    RssSource(
        code="FSB",
        name="Financial Stability Board",
        jurisdiction="INTL",
        rss_url="https://www.fsb.org/feed/",
    ),
]


# Build a code → RssSource index for the backwards-compat shim modules
# (`ingestion/sec_rss.py` etc) and any caller that wants a specific source.
BY_CODE: dict[str, RssSource] = {s.code: s for s in SOURCES}


def get(code: str) -> RssSource:
    """Lookup by source code. Raises KeyError if not registered."""
    return BY_CODE[code]


def jurisdictions() -> list[str]:
    """Distinct jurisdictions, in first-appearance order, for UI filter chips."""
    seen: dict[str, None] = {}
    for s in SOURCES:
        seen.setdefault(s.jurisdiction, None)
    return list(seen)
