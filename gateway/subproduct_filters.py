"""Translate a subproduct slug into a SQL filter fragment.

Every subproduct narrows the market/prediction stream to its own
content. The filter lives here rather than being inlined into every
feed handler so:

  * adding a new subproduct = one entry in this table, not a dozen
    ``if slug == 'sports'`` branches across routes;
  * tests can ingest a canonical list of predictions and assert that
    the filter partitions them correctly per slug;
  * the sub-brand landing "floating numbers" and the real feed use
    the same filter, so stats don't drift from what users see.

The filters use conservative keyword sets plus category names that
our classifier already emits. Where a single category can't cleanly
represent the subproduct (crypto prices can show up under 'finance'),
we also match a regex against the question text.
"""

from __future__ import annotations

import re
from typing import Iterable


# Category whitelists from the existing market classifier. Staying in
# sync with backend/markets/unified_markets.py category list.
#
# ``midterm`` category is left empty: the category "politics" is too
# broad (UK / international elections would match), so the regex alone
# gates inclusion. That way a politics row mentioning "Senate" matches
# midterm, but a UK general-election row with no US keyword does not.
_CATEGORIES_BY_SUBPRODUCT: dict[str, set[str]] = {
    "sports":  {"sports", "nfl", "nba", "soccer", "mma", "tennis", "mlb", "nhl"},
    "weather": {"weather"},
    "world":   {"geopolitics", "conflicts", "international", "world"},
    "crypto":  {"crypto"},
    "midterm": set(),
    "traders": set(),  # traders: all Polymarket markets; rank by top-trader activity
}


# Keyword regexes — case-insensitive — matched against the market
# question when the category alone isn't specific enough.
_KEYWORD_REGEX: dict[str, re.Pattern] = {
    "weather": re.compile(
        r"\b(temperature|rain|rainfall|snow|hurricane|typhoon|tornado|"
        r"flood|heatwave|cold snap|climate|precipitation|weather)\b",
        re.IGNORECASE,
    ),
    "crypto": re.compile(
        r"\b(bitcoin|btc|ethereum|eth|solana|sol|ada|xrp|doge|"
        r"crypto|blockchain|altcoin|defi)\b",
        re.IGNORECASE,
    ),
    # US election markets — differentiates midterm from general
    # "politics" markets (e.g. UK elections). Matches Senate/House
    # races, state-level gubernatorial, and the next midterm year.
    "midterm": re.compile(
        r"\b("
        r"senate|house|governor|gubernatorial|"
        r"midterm|midterms|"
        r"republican|democrat|gop|dnc|"
        r"congressional|congress|"
        r"election\s+\d{4}"
        r")\b",
        re.IGNORECASE,
    ),
}


def categories_for(slug: str) -> set[str]:
    """Return the category whitelist for ``slug``.

    Public so the stats page can answer "how many active markets in
    sports right now?" by passing the category set into the existing
    market counter rather than re-deriving it here.
    """
    return _CATEGORIES_BY_SUBPRODUCT.get(slug, set())


def keyword_regex_for(slug: str) -> re.Pattern | None:
    """Return the optional keyword regex for ``slug`` (None if category
    alone is enough)."""
    return _KEYWORD_REGEX.get(slug)


def _match_category(row: object, allowed: Iterable[str]) -> bool:
    allowed_lower = {c.lower() for c in allowed}
    cat = _attr(row, "category", "") or _attr(row, "market_category", "")
    return str(cat).lower() in allowed_lower


def _match_keyword(row: object, pattern: re.Pattern) -> bool:
    # Join every field that might carry market text — different
    # callers feed rows with different column names (predictions.content,
    # markets.question, environmental_impacts.market_question, ...).
    fields = ("question", "title", "market_question", "content")
    text = " ".join(str(_attr(row, f, "") or "") for f in fields)
    return bool(pattern.search(text))


def _attr(row: object, name: str, default=None):
    """Uniform access for dict / sqlite3.Row / dataclass / object."""
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]  # sqlite3.Row
    except (KeyError, IndexError, TypeError):
        pass
    return getattr(row, name, default)


def matches_subproduct(row: object, slug: str) -> bool:
    """Does ``row`` belong to the ``slug`` subproduct?

    Returns True for the ``traders`` slug on every Polymarket market —
    the brand's value-add is the leaderboard overlay, not a content
    filter, so every active Polymarket market is in-scope. For every
    other slug the union of category and keyword match is used.
    """
    if slug == "traders":
        # Platform check for safety. Some rows may not have it
        # populated; treat missing as "probably polymarket" since
        # that's the dominant source today.
        platform = (_attr(row, "platform", "") or "").lower()
        return platform in ("", "polymarket")

    cats = categories_for(slug)
    if cats and _match_category(row, cats):
        return True
    pat = _KEYWORD_REGEX.get(slug)
    if pat and _match_keyword(row, pat):
        return True
    return False


def filter_by_subproduct(rows: Iterable[object], slug: str) -> list:
    """Non-SQL filter for already-fetched rows.

    The production feeds push the filter into SQL (see
    ``sql_where_for``) for performance; this in-memory version is the
    canonical definition that SQL must match. Tests assert the two
    agree on a seeded dataset.
    """
    return [r for r in rows if matches_subproduct(r, slug)]


def sql_where_for(slug: str, alias: str = "p") -> tuple[str, list]:
    """Return a ``(where_fragment, params)`` pair for SQL composition.

    ``alias`` is the table alias used in the outer query. Fragment
    starts with 'AND' so it can be appended to an existing WHERE:

        sql = "SELECT * FROM predictions p WHERE 1=1 "
        frag, params = sql_where_for("sports")
        sql += frag

    For subproducts with no filter (``traders``) the fragment is
    ``" AND 1=1 "`` — callers can still concatenate safely.
    """
    cats = categories_for(slug)
    params: list = []
    clauses: list[str] = []

    if cats:
        placeholders = ",".join("?" * len(cats))
        clauses.append(f"{alias}.category IN ({placeholders})")
        params.extend(sorted(cats))

    pat = _KEYWORD_REGEX.get(slug)
    if pat:
        # SQLite has no built-in regex; match a lightweight list of
        # literal keywords from the same family. Each clause stays
        # parameterised so the caller's DB adapter can continue to use
        # the statement cache.
        literal_alts = _regex_literals(slug)
        if literal_alts:
            lit_clauses = []
            for lit in literal_alts:
                lit_clauses.append(f"LOWER({alias}.content) LIKE ?")
                params.append(f"%{lit.lower()}%")
            clauses.append("(" + " OR ".join(lit_clauses) + ")")

    if not clauses:
        # traders: no filter. Return a harmless true clause so the
        # caller's WHERE composition still works.
        return " AND 1=1 ", []

    return " AND (" + " OR ".join(clauses) + ") ", params


# Conservative literal keyword lists derived from the regex patterns
# above. These are safer to push into SQL LIKE than the regex.
_LITERALS: dict[str, list[str]] = {
    "weather": [
        "weather", "temperature", "rainfall", "snow", "hurricane",
        "typhoon", "tornado", "flood", "heatwave", "climate",
    ],
    "crypto": [
        "bitcoin", " btc ", "ethereum", " eth ", "solana", " sol ",
        "crypto", "blockchain", "altcoin", "defi",
    ],
    "midterm": [
        "senate", "house", "governor", "gubernatorial", "midterm",
        "congressional", "congress",
    ],
}


def _regex_literals(slug: str) -> list[str]:
    return _LITERALS.get(slug, [])
