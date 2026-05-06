"""Form 4 XML parser.

Form 4 is the Statement of Changes of Beneficial Ownership filed by
corporate insiders. The structured XML lives in the filing as a file
ending in `.xml` (typically `wf-form4_*.xml` or `primary_doc.xml`). It
contains a `<reportingOwner>` block plus `<nonDerivativeTable>` and
`<derivativeTable>` of transactions.

Transaction codes we care about most:
    P  - open-market or private purchase (the strong "insider buying" signal)
    S  - open-market or private sale
    A  - grant / award (compensation, not a directional signal)
    D  - sale to issuer (often disposition, not directional)
    M  - exercise / conversion of derivative
    F  - tax withholding
We tag rows with `is_buy=1` only when txn_code == 'P' AND shares > 0.
"""

from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

log = logging.getLogger("form4")


def _text(el, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    if found is None:
        return default
    return (found.text or "").strip()


def _value(el, path: str) -> str:
    """Form 4 wraps many fields in <fieldName><value>...</value></fieldName>."""
    return _text(el, f"{path}/value")


def _float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_form4(xml: str, accession: str, filed_at: str, filing_url: str) -> list[dict]:
    """Return a list of insider_txn rows from a Form 4 XML body."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("form4 parse error for %s: %s", accession, e)
        return []

    issuer = root.find("issuer")
    issuer_cik = _value(issuer, "issuerCik") or _text(issuer, "issuerCik")
    issuer_name = _value(issuer, "issuerName") or _text(issuer, "issuerName")
    issuer_ticker = (
        _value(issuer, "issuerTradingSymbol") or _text(issuer, "issuerTradingSymbol") or ""
    ).upper().strip()

    # Multiple reporters can co-file a single Form 4. Take the first
    # for naming, but keep CIK list for the summary string.
    reporters = root.findall("reportingOwner")
    reporter_name = ""
    reporter_cik = ""
    relations: list[str] = []
    if reporters:
        first = reporters[0]
        rid = first.find("reportingOwnerId")
        reporter_cik = _value(rid, "rptOwnerCik") or _text(rid, "rptOwnerCik")
        reporter_name = _value(rid, "rptOwnerName") or _text(rid, "rptOwnerName")
        rel = first.find("reportingOwnerRelationship")
        if rel is not None:
            for tag, label in (
                ("isDirector", "Director"),
                ("isOfficer", "Officer"),
                ("isTenPercentOwner", "10% Owner"),
                ("isOther", "Other"),
            ):
                v = (rel.findtext(tag) or "").strip()
                if v in ("1", "true", "True"):
                    relations.append(label)
            officer_title = (rel.findtext("officerTitle") or "").strip()
            if officer_title:
                relations.append(officer_title)
    relation_str = ", ".join(relations) or ""

    rows: list[dict] = []
    line_no = 0

    # Non-derivative (common stock) transactions
    nd = root.find("nonDerivativeTable")
    if nd is not None:
        for txn in nd.findall("nonDerivativeTransaction"):
            line_no += 1
            row = _row_from_txn(
                txn,
                accession=accession,
                line_no=line_no,
                filed_at=filed_at,
                reporter_cik=reporter_cik,
                reporter_name=reporter_name,
                reporter_relation=relation_str,
                issuer_cik=issuer_cik,
                issuer_ticker=issuer_ticker,
                issuer_name=issuer_name,
                filing_url=filing_url,
                derivative=False,
            )
            if row:
                rows.append(row)

    # Derivative transactions (options, warrants, RSUs)
    der = root.find("derivativeTable")
    if der is not None:
        for txn in der.findall("derivativeTransaction"):
            line_no += 1
            row = _row_from_txn(
                txn,
                accession=accession,
                line_no=line_no,
                filed_at=filed_at,
                reporter_cik=reporter_cik,
                reporter_name=reporter_name,
                reporter_relation=relation_str,
                issuer_cik=issuer_cik,
                issuer_ticker=issuer_ticker,
                issuer_name=issuer_name,
                filing_url=filing_url,
                derivative=True,
            )
            if row:
                rows.append(row)

    return rows


def _row_from_txn(
    txn,
    *,
    accession: str,
    line_no: int,
    filed_at: str,
    reporter_cik: str,
    reporter_name: str,
    reporter_relation: str,
    issuer_cik: str,
    issuer_ticker: str,
    issuer_name: str,
    filing_url: str,
    derivative: bool,
) -> dict | None:
    txn_date = _value(txn, "transactionDate")
    coded = txn.find("transactionCoding")
    txn_code = _value(coded, "transactionCode") if coded is not None else ""
    amounts = txn.find("transactionAmounts")
    shares = _float(_value(amounts, "transactionShares")) if amounts is not None else None
    price = _float(_value(amounts, "transactionPricePerShare")) if amounts is not None else None
    acquired_disposed = _value(amounts, "transactionAcquiredDisposedCode") if amounts is not None else ""

    # P with A flag and price > 0 == open-market purchase. Some filings
    # show A (acquired) for grants — we only count P as a real buy.
    is_buy = 1 if (txn_code == "P" and (shares or 0) > 0 and not derivative) else 0
    value_usd = (shares * price) if (shares is not None and price is not None) else None

    if not txn_code and shares is None:
        return None

    return {
        "accession":         accession,
        "line_no":           line_no,
        "filed_at":          filed_at,
        "reporter_cik":      reporter_cik,
        "reporter_name":     reporter_name,
        "reporter_relation": _short_relation(reporter_relation),
        "issuer_cik":        issuer_cik,
        "issuer_ticker":     issuer_ticker or None,
        "issuer_name":       issuer_name,
        "txn_date":          txn_date,
        "txn_code":          txn_code,
        "shares":            shares,
        "price":             price,
        "value_usd":         value_usd,
        "is_buy":            is_buy,
        "filing_url":        filing_url,
    }


def _short_relation(s: str) -> str:
    if not s:
        return ""
    # Trim absurdly long titles to keep the UI tidy.
    return re.sub(r"\s+", " ", s)[:120]
