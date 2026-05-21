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
