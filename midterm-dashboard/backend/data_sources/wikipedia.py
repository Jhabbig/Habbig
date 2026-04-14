"""Wikipedia REST API + MediaWiki action API.

Universal source for:
- Past election summaries (e.g. "2022 Hungarian parliamentary election")
- Candidate / leader bios with photos
- Country / state political background

Free, no key, just rate-limited (200 req/sec is generous).

Docs:
- REST: https://en.wikipedia.org/api/rest_v1/
- Action: https://en.wikipedia.org/w/api.php
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

REST_BASE = "https://en.wikipedia.org/api/rest_v1"
ACTION_BASE = "https://en.wikipedia.org/w/api.php"

USER_AGENT = "MidtermEdge/1.0 (https://midtermedge.example; ops@midtermedge.example)"

# A few US states share their name with another well-known place, so Wikipedia
# disambiguates the article and category titles. Maps USPS code → qualified name
# used in Wikipedia titles + categories.
WIKI_STATE_QUALIFIER: dict[str, str] = {
    "GA": "Georgia (U.S. state)",
    "WA": "Washington (state)",
    "NY": "New York (state)",
}


def _wiki_state_name(state_postal: str, plain_name: str) -> str:
    """Return the Wikipedia-qualified state name (handles GA/WA/NY ambiguity)."""
    return WIKI_STATE_QUALIFIER.get(state_postal.upper(), plain_name)


def _ordinal(n: int) -> str:
    """1 → '1st', 2 → '2nd', 11 → '11th', etc."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def _get_summary(session: aiohttp.ClientSession, title: str) -> Optional[dict]:
    """Fetch the page summary for a title (REST endpoint)."""
    url = f"{REST_BASE}/page/summary/{urllib.parse.quote(title)}"
    headers = {"User-Agent": USER_AGENT, "accept": "application/json"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Wikipedia summary %s failed: %s", title, e)
        return None


async def _search(session: aiohttp.ClientSession, query: str, limit: int = 5) -> list[str]:
    """Search Wikipedia titles, returns list of matching titles."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.get(ACTION_BASE, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Wikipedia search '%s' failed: %s", query, e)
        return []
    return [r["title"] for r in data.get("query", {}).get("search", [])]


async def _category_members(
    session: aiohttp.ClientSession, category: str, limit: int = 500
) -> list[str]:
    """Enumerate page titles in a Wikipedia category.

    `category` should be just the name (no "Category:" prefix). Returns
    titles only — caller is responsible for filtering / sorting.
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmlimit": str(limit),
        "cmtype": "page",
        "format": "json",
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        async with session.get(ACTION_BASE, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Wikipedia category '%s' failed: %s", category, e)
        return []
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


async def fetch_country_political_summary(
    session: aiohttp.ClientSession, country_name: str
) -> Optional[dict]:
    """Get a country's political history summary from Wikipedia.

    Tries 'Politics of {country}' first, falls back to country page.
    """
    titles = [f"Politics of {country_name}", f"{country_name}"]
    for title in titles:
        data = await _get_summary(session, title)
        if data and data.get("extract"):
            return {
                "title": data.get("title"),
                "extract": data.get("extract"),
                "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
                "thumbnail": data.get("thumbnail", {}).get("source"),
            }
    return None


async def fetch_recent_elections(
    session: aiohttp.ClientSession,
    country_name: str,
    max_results: int = 5,
    adjective: Optional[str] = None,
) -> list[dict]:
    """Search for recent elections in a country and fetch summaries for each.

    Returns a list of {year, title, extract, url} sorted newest-first.
    Pass `adjective` (e.g. "French", "Hungarian") to restrict results to articles
    whose title actually contains the country's demonym — Wikipedia search alone
    is too fuzzy and will return globally-ranked elections that mention the
    country in passing.
    """
    queries = [
        f"{country_name} general election",
        f"{country_name} parliamentary election",
        f"{country_name} presidential election",
    ]
    if adjective:
        queries = [
            f"{adjective} general election",
            f"{adjective} parliamentary election",
            f"{adjective} presidential election",
            f"{adjective} federal election",
        ]
    seen_titles = set()
    elections = []
    # Run all queries to ensure we don't miss presidential elections behind parliamentary ones
    for q in queries:
        titles = await _search(session, q, limit=15)
        for t in titles:
            if t in seen_titles:
                continue
            seen_titles.add(t)
            # Filter to election pages with a year (e.g. "2022 Hungarian parliamentary election")
            year_match = re.match(r"^(\d{4})\s.*election", t)
            if not year_match:
                continue
            # Skip subpages like "results by constituency", "candidates", "polling"
            t_lower = t.lower()
            if any(skip in t_lower for skip in ("results by", "by constituency", "polling for", "candidates in", "endorsements")):
                continue
            # Restrict to titles that actually reference the country
            if adjective and adjective.lower() not in t.lower() and country_name.lower() not in t.lower():
                continue
            year = int(year_match.group(1))
            elections.append({"year": year, "title": t})

    elections.sort(key=lambda e: -e["year"])
    elections = elections[:max_results]

    # Fetch a summary for each
    enriched = []
    for el in elections:
        summary = await _get_summary(session, el["title"])
        if summary:
            enriched.append({
                "year": el["year"],
                "title": summary.get("title"),
                "extract": summary.get("extract"),
                "url": summary.get("content_urls", {}).get("desktop", {}).get("page"),
                "thumbnail": summary.get("thumbnail", {}).get("source"),
            })
    return enriched


def _is_disambiguation(data: dict) -> bool:
    """Detect Wikipedia disambiguation/redirect pages."""
    if not data:
        return True
    desc = (data.get("description") or "").lower()
    extract = (data.get("extract") or "").lower()
    if "topics referred to by the same term" in extract:
        return True
    if data.get("type") == "disambiguation":
        return True
    if desc == "topics referred to by the same term":
        return True
    return False


_POLITICIAN_KEYWORDS = (
    "politician", "senator", "representative", "governor", "congress",
    "congressman", "congresswoman", "commissioner", "attorney general",
    "lieutenant governor", "secretary of state", "state senate",
    "state house", "mayor", "councilman", "councilwoman",
)


def _format_bio(data: dict) -> dict:
    return {
        "name": data.get("title"),
        "description": data.get("description"),
        "extract": data.get("extract"),
        "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
        "thumbnail": data.get("thumbnail", {}).get("source"),
    }


def _bio_text(data: dict) -> str:
    return (
        (data.get("extract") or "")
        + " "
        + (data.get("description") or "")
    ).lower()


def _looks_like_politician(data: dict) -> bool:
    return any(kw in _bio_text(data) for kw in _POLITICIAN_KEYWORDS)


def _score_bio(data: dict, state_name: Optional[str], context: Optional[str]) -> int:
    """Score a candidate bio for relevance. Higher = better."""
    if not data or _is_disambiguation(data) or not data.get("extract"):
        return -1
    text = _bio_text(data)
    score = 0
    if _looks_like_politician(data):
        score += 5
    if state_name and state_name.lower() in text:
        # Strong signal — bio actually mentions the state we care about
        score += 10
    if context:
        for word in context.lower().split():
            if len(word) > 3 and word in text:
                score += 1
    # Bonus for current/active office holders (e.g. "is an American politician")
    if " is an american politician" in text or " is a republican" in text or " is a democrat" in text:
        score += 2
    # Penalty for clearly wrong matches
    if "ancient" in text or "(died" in text or "general (" in text.split(" politician")[0]:
        score -= 3
    return score


def _name_matches(searched_name: str, found_title: str) -> bool:
    """Return True if the Wikipedia title looks like it's about the searched person.

    Requires every alphabetic token in the searched name (length > 1) to appear
    somewhere in the article title — case-insensitive. Allows parenthetical
    qualifiers like 'Mike Collins (politician)' and middle initials.
    """
    if not searched_name or not found_title:
        return False
    title_lower = found_title.lower()
    # Drop parentheticals from title for cleaner token matching
    title_clean = re.sub(r"\([^)]*\)", " ", title_lower)
    title_tokens = set(re.findall(r"[a-z']+", title_clean))
    name_tokens = [t for t in re.findall(r"[a-z']+", searched_name.lower()) if len(t) > 1]
    if not name_tokens:
        return False
    # Every token in the searched name must appear in the title
    return all(t in title_tokens for t in name_tokens)


async def fetch_person_bio(
    session: aiohttp.ClientSession,
    name: str,
    context: Optional[str] = None,
    state_name: Optional[str] = None,
) -> Optional[dict]:
    """Fetch a Wikipedia bio for a politician/candidate.

    Disambiguation strategy:
    1. Direct lookup. If it lands on a real bio AND it mentions the state
       (when given), accept it.
    2. Otherwise enumerate search candidates with state-aware queries,
       score them, and return the best match. Any candidate whose title
       doesn't actually match the searched name is dropped — this prevents
       returning a different politician when the real person has no article.

    `state_name` is the full state name (e.g. "Georgia") used as a strong
    relevance signal. `context` is a free-form hint string ("Georgia senate").
    """
    direct = await _get_summary(session, name)
    if (
        direct
        and not _is_disambiguation(direct)
        and direct.get("extract")
        and _name_matches(name, direct.get("title") or "")
    ):
        # Accept the direct hit only if we have no state to validate against,
        # or the bio actually mentions that state.
        if not state_name or state_name.lower() in _bio_text(direct):
            if _looks_like_politician(direct) or not state_name:
                return _format_bio(direct)

    # Build search queries — state-aware first, then generic fallbacks
    queries: list[str] = []
    if state_name:
        queries.extend([
            f"{name} {state_name} politician",
            f"{name} politician {state_name}",
            f"{name} {state_name}",
        ])
    if context:
        queries.append(f"{name} politician {context}")
        queries.append(f"{name} {context}")
    queries.append(f"{name} politician")

    best: Optional[dict] = None
    best_score = -1
    seen_titles: set[str] = set()
    for query in queries:
        titles = await _search(session, query, limit=8)
        for title in titles:
            if title in seen_titles:
                continue
            seen_titles.add(title)
            # Hard requirement: title must actually be about the searched person.
            # This stops the scorer from picking up a different politician with
            # the same first or last name when the real person has no article.
            if not _name_matches(name, title):
                continue
            candidate = await _get_summary(session, title)
            if not candidate or _is_disambiguation(candidate):
                continue
            score = _score_bio(candidate, state_name, context)
            if score > best_score:
                best = candidate
                best_score = score
        # Short-circuit if we already found a strong state-confirmed politician
        if best_score >= 15:
            break

    # Fallback: the original direct hit even if it didn't mention the state,
    # but only if the title actually matches the name.
    if (
        best is None
        and direct
        and not _is_disambiguation(direct)
        and direct.get("extract")
        and _name_matches(name, direct.get("title") or "")
    ):
        return _format_bio(direct)

    if best is None or best_score < 5:
        return None
    return _format_bio(best)


# ============================================================================
# US state-level history (Senate / gubernatorial / presidential / House cycles)
# ============================================================================


async def fetch_state_political_summary(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
) -> Optional[dict]:
    """Get a US state's political-history summary from Wikipedia.

    Tries 'Politics of {state}' first, then 'Government of {state}', then
    the state article itself. Handles GA/WA/NY title disambiguation.
    """
    qualified = _wiki_state_name(state_postal, state_name)
    candidates = [
        f"Politics of {qualified}",
        f"Government of {qualified}",
        qualified,
    ]
    # If the state has a qualified name, also try the bare form as a fallback
    if qualified != state_name:
        candidates.extend([f"Politics of {state_name}", state_name])

    for title in candidates:
        data = await _get_summary(session, title)
        if data and data.get("extract") and not _is_disambiguation(data):
            return {
                "title": data.get("title"),
                "extract": data.get("extract"),
                "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
                "thumbnail": data.get("thumbnail", {}).get("source"),
            }
    return None


async def fetch_state_elections(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    max_results: int = 8,
) -> list[dict]:
    """Enumerate Senate, gubernatorial, presidential, and House-cycle elections
    for a US state via Wikipedia categories.

    Returns the most recent `max_results` elections, newest-first, each with
    a Wikipedia summary, year, type, and thumbnail.
    """
    qualified = _wiki_state_name(state_postal, state_name)
    # Each tuple: (category name, election type label)
    category_specs = [
        (f"United States Senate elections in {qualified}", "senate"),
        (f"{qualified} gubernatorial elections", "governor"),
        (f"United States presidential elections in {qualified}", "president"),
        (f"United States House of Representatives elections in {qualified}", "house_cycle"),
    ]

    seen_titles: set[str] = set()
    elections: list[dict] = []
    state_lower = state_name.lower()
    for category, etype in category_specs:
        members = await _category_members(session, category, limit=500)
        for t in members:
            if t in seen_titles:
                continue
            seen_titles.add(t)
            year_match = re.match(r"^(\d{4})\s", t)
            if not year_match:
                continue  # skip "List of ..." index pages
            t_lower = t.lower()
            # Wikipedia sometimes parents the cycle-wide article (e.g.
            # "2028 United States Senate elections") into per-state subcategories.
            # Require the state name in the title to keep results state-specific.
            if state_lower not in t_lower:
                continue
            if any(skip in t_lower for skip in (
                "results by", "by county", "by congressional", "polling for",
                "endorsements", "candidates in", "redistricting",
            )):
                continue
            elections.append({"year": int(year_match.group(1)), "title": t, "type": etype})

    # Newest first, then truncate
    elections.sort(key=lambda e: -e["year"])
    elections = elections[:max_results]

    # Hydrate each with a summary
    enriched: list[dict] = []
    for el in elections:
        summary = await _get_summary(session, el["title"])
        if not summary or not summary.get("extract"):
            continue
        # Wikipedia may redirect a not-yet-written state-specific article
        # (e.g. "2028 United States Senate election in Georgia") to a
        # cycle-wide page. Drop the result if the redirect lost the state name.
        resolved_title = (summary.get("title") or "").lower()
        if state_lower not in resolved_title:
            continue
        enriched.append({
            "year": el["year"],
            "type": el["type"],
            "title": summary.get("title"),
            "extract": summary.get("extract"),
            "url": summary.get("content_urls", {}).get("desktop", {}).get("page"),
            "thumbnail": summary.get("thumbnail", {}).get("source"),
        })
    return enriched


# ============================================================================
# US House district history (article summary + state-cycle context)
# ============================================================================


def _district_article_title(state_name: str, district: str) -> str:
    """Build the canonical Wikipedia article title for a House district."""
    d = (district or "").upper()
    if d in ("AL", "AT-LARGE", "0", "00"):
        return f"{state_name}'s at-large congressional district"
    try:
        n = int(d.lstrip("0") or "0")
    except ValueError:
        return f"{state_name}'s {d} congressional district"
    return f"{state_name}'s {_ordinal(n)} congressional district"


async def fetch_house_district_summary(
    session: aiohttp.ClientSession,
    state_name: str,
    district: str,
) -> Optional[dict]:
    """Fetch the Wikipedia summary for a House district article.

    Returns the lead paragraph (current rep, redistricting history, geography)
    plus URL and thumbnail. Returns None if the district article doesn't exist.
    """
    title = _district_article_title(state_name, district)
    data = await _get_summary(session, title)
    if not data or not data.get("extract") or _is_disambiguation(data):
        return None
    return {
        "title": data.get("title"),
        "extract": data.get("extract"),
        "description": data.get("description"),
        "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
        "thumbnail": data.get("thumbnail", {}).get("source"),
    }


async def _fetch_article_wikitext(
    session: aiohttp.ClientSession, page_title: str
) -> Optional[str]:
    """Fetch the full wikitext of a Wikipedia article via the parse API."""
    params = {
        "action": "parse",
        "page": page_title,
        "format": "json",
        "prop": "wikitext",
        "redirects": "1",
    }
    headers = {"User-Agent": USER_AGENT, "accept": "application/json"}
    try:
        async with session.get(
            ACTION_BASE,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("Wikipedia parse %s failed: %s", page_title, e)
        return None
    return data.get("parse", {}).get("wikitext", {}).get("*")


def _extract_district_section(wikitext: str, district: str) -> Optional[str]:
    """Return the wikitext slice for the ==District N== section of a state-cycle article."""
    if not wikitext:
        return None
    try:
        n = int(str(district).lstrip("0") or "0")
    except ValueError:
        return None
    pattern = re.compile(rf"==\s*District\s+0*{n}\s*==", re.IGNORECASE)
    match = pattern.search(wikitext)
    if not match:
        return None
    start = match.end()
    rest = wikitext[start:]
    next_header = re.search(r"\n==[^=][^\n]*==\n", rest)
    end = next_header.start() if next_header else len(rest)
    return rest[:end]


def _find_winning_candidate_templates(section: str) -> list[str]:
    """Return raw `{{Election box winning candidate ...}}` template strings."""
    templates: list[str] = []
    if not section:
        return templates
    idx = 0
    while True:
        marker = section.find("{{Election box winning candidate", idx)
        if marker == -1:
            break
        depth = 0
        j = marker
        while j < len(section) - 1:
            if section[j : j + 2] == "{{":
                depth += 1
                j += 2
            elif section[j : j + 2] == "}}":
                depth -= 1
                j += 2
                if depth == 0:
                    templates.append(section[marker:j])
                    idx = j
                    break
            else:
                j += 1
        if depth != 0:
            break
    return templates


def _split_template_pipes(body: str) -> list[str]:
    """Split a template body on top-level `|` (ignoring pipes inside [[...]] / {{...}})."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    bracket = 0
    brace = 0
    while i < len(body):
        nxt = body[i : i + 2]
        if nxt == "[[":
            bracket += 1
            cur.append("[[")
            i += 2
            continue
        if nxt == "]]":
            bracket -= 1
            cur.append("]]")
            i += 2
            continue
        if nxt == "{{":
            brace += 1
            cur.append("{{")
            i += 2
            continue
        if nxt == "}}":
            brace -= 1
            cur.append("}}")
            i += 2
            continue
        ch = body[i]
        if ch == "|" and bracket == 0 and brace == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur))
    return parts


def _clean_wiki_link(text: str) -> str:
    """Strip [[Page|Display]] / [[Page]] / {{nowrap|...}} / triple-quotes from wikitext."""
    if not text:
        return ""
    # Multi-name templates (gov+lt.gov): {{ubl|Name1|Name2}} → Name1
    text = re.sub(
        r"\{\{ubl\|([^|}]+)[^}]*\}\}",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    # Inline templates like {{nowrap|Name}} or {{small|...}} — keep the last arg
    text = re.sub(
        r"\{\{(?:nowrap|nbsp|small|big|sortname)\|([^}]+)\}\}",
        lambda m: m.group(1).split("|")[-1],
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"'{2,3}", "", text)
    # Strip <br /> lt.gov suffixes (e.g. "Mike Braun<br />Micah Beckwith")
    text = re.sub(r"<br\s*/?>.*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _short_party(party: str) -> str:
    """'Democratic Party (United States)' → 'Democratic'."""
    p = re.sub(r"\s*Party\s*\(United States\)\s*$", "", party).strip()
    p = re.sub(r"\s*\(US\)\s*$", "", p).strip()
    p = re.sub(r"\s*Party\s*$", "", p).strip()
    # Normalize state-prefixed party names (e.g. "Michigan Republican" → "Republican")
    if "republican" in p.lower():
        p = "Republican"
    elif "democrat" in p.lower():
        p = "Democratic"
    elif "libertarian" in p.lower():
        p = "Libertarian"
    elif "green" in p.lower():
        p = "Green"
    elif "independent" in p.lower():
        p = "Independent"
    return p


def _parse_winning_template(template: str) -> Optional[dict]:
    """Parse a single Election-box winning-candidate template into structured fields."""
    if not template or not template.startswith("{{") or not template.endswith("}}"):
        return None
    body = template[2:-2]
    parts = _split_template_pipes(body)
    if not parts:
        return None
    template_name = parts[0].strip()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        key_norm = key.strip().lower()
        params[key_norm] = val.strip()

    candidate = _clean_wiki_link(params.get("candidate", ""))
    incumbent = bool(re.search(r"\(incumbent\)", candidate, re.IGNORECASE))
    candidate = re.sub(
        r"\s*\((incumbent|presumptive)\)\s*", "", candidate, flags=re.IGNORECASE
    ).strip()

    party = _short_party(_clean_wiki_link(params.get("party", "")))

    votes_str = _clean_wiki_link(params.get("votes", "")).replace(",", "").strip()
    try:
        votes = int(votes_str)
    except (ValueError, TypeError):
        votes = None

    pct_str = _clean_wiki_link(params.get("percentage", "")).replace("%", "").strip()
    try:
        percentage = float(pct_str)
    except (ValueError, TypeError):
        percentage = None

    if not candidate:
        return None
    return {
        "candidate": candidate,
        "party": party,
        "votes": votes,
        "percentage": percentage,
        "incumbent": incumbent,
        "_template_name": template_name,
    }


def _find_named_template(section: str, template_name: str) -> Optional[str]:
    """Return the first `{{template_name ...}}` block in the section, balanced braces."""
    if not section:
        return None
    needle = "{{" + template_name
    marker = section.find(needle)
    if marker == -1:
        return None
    depth = 0
    j = marker
    while j < len(section) - 1:
        if section[j : j + 2] == "{{":
            depth += 1
            j += 2
        elif section[j : j + 2] == "}}":
            depth -= 1
            j += 2
            if depth == 0:
                return section[marker:j]
        else:
            j += 1
    return None


def _parse_infobox_election(template: str) -> Optional[dict]:
    """Fallback parser for `{{Infobox election}}` templates used in competitive races.

    Picks nominee1 as the winner (nominee1 is conventionally bolded — Wikipedia
    style places the winner in slot 1).
    """
    if not template:
        return None
    body = template[2:-2]
    parts = _split_template_pipes(body)
    if not parts:
        return None
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        params[key.strip().lower()] = val.strip()

    candidate = _clean_wiki_link(params.get("nominee1") or params.get("candidate1") or "")
    candidate = re.sub(r"\s*\(incumbent\)\s*", "", candidate, flags=re.IGNORECASE).strip()
    if not candidate:
        return None

    party = _short_party(_clean_wiki_link(params.get("party1", "")))

    votes_str = _clean_wiki_link(params.get("popular_vote1") or "").replace(",", "").strip()
    try:
        votes = int(votes_str)
    except (ValueError, TypeError):
        votes = None

    pct_str = _clean_wiki_link(params.get("percentage1") or "").replace("%", "").strip()
    try:
        percentage = float(pct_str)
    except (ValueError, TypeError):
        percentage = None

    incumbent_field = (params.get("before_election") or "").strip()
    incumbent = bool(candidate) and candidate.lower() in incumbent_field.lower()

    return {
        "candidate": candidate,
        "party": party,
        "votes": votes,
        "percentage": percentage,
        "incumbent": incumbent,
        "_template_name": "Infobox election",
    }


async def fetch_house_district_winner(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    district: str,
    year: int,
) -> Optional[dict]:
    """Extract the general-election winner of a single House district for one cycle.

    Pulls the cycle-wide article (e.g. '2024 United States House of Representatives
    elections in Georgia'), finds the ==District N== section, and parses the LAST
    `{{Election box winning candidate}}` template inside it (general election sits
    after primaries). Falls back to `{{Infobox election}}` for competitive races
    that use the more elaborate template. Returns None if nothing parses.
    """
    # Cycle articles use the plain state name (not the (U.S. state) qualifier).
    # Try the plain name first, then fall back to the qualified form for safety.
    qualified = _wiki_state_name(state_postal, state_name)
    candidate_titles = [
        f"{year} United States House of Representatives elections in {state_name}",
    ]
    if qualified != state_name:
        candidate_titles.append(
            f"{year} United States House of Representatives elections in {qualified}"
        )
    wikitext = None
    page_title = candidate_titles[0]
    for title in candidate_titles:
        wikitext = await _fetch_article_wikitext(session, title)
        if wikitext:
            page_title = title
            break
    if not wikitext:
        return None
    section = _extract_district_section(wikitext, district)
    if not section:
        return None

    parsed: Optional[dict] = None
    # Primary parser: per-row Election box winning candidate templates
    templates = _find_winning_candidate_templates(section)
    if templates:
        parsed = _parse_winning_template(templates[-1])
    # Fallback parser: Infobox election (used by competitive races with images)
    if not parsed:
        infobox = _find_named_template(section, "Infobox election")
        if infobox:
            parsed = _parse_infobox_election(infobox)
    if not parsed:
        return None

    parsed["year"] = year
    parsed["url"] = (
        "https://en.wikipedia.org/wiki/" + urllib.parse.quote(page_title.replace(" ", "_"))
    )
    return parsed


async def fetch_house_district_past_winners(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    district: str,
    cycles: int = 3,
    latest_year: int = 2024,
) -> list[dict]:
    """Fetch the past `cycles` general-election winners for a single House district.

    Walks back from `latest_year` in two-year steps. Drops cycles where parsing fails.
    Adds a `flip_from` field when the party changed vs. the previous cycle.
    """
    winners: list[dict] = []
    for offset in range(cycles):
        year = latest_year - offset * 2
        winner = await fetch_house_district_winner(
            session, state_postal, state_name, district, year
        )
        if winner:
            winners.append(winner)
    _add_flips(winners)
    return winners


async def _parse_statewide_winner(
    session: aiohttp.ClientSession, article_title: str
) -> Optional[dict]:
    """Generic: parse the winning candidate from any statewide election article.

    Works for Senate, gubernatorial, and other whole-article election pages that
    use Election box or Infobox election templates. Returns None for future
    elections that have no actual results (no votes and no percentage).
    """
    wikitext = await _fetch_article_wikitext(session, article_title)
    if not wikitext:
        return None
    parsed: Optional[dict] = None
    templates = _find_winning_candidate_templates(wikitext)
    if templates:
        parsed = _parse_winning_template(templates[-1])
    if not parsed:
        infobox = _find_named_template(wikitext, "Infobox election")
        if infobox:
            parsed = _parse_infobox_election(infobox)
    if not parsed:
        return None
    # Skip future elections — no votes AND no percentage means no results yet
    if parsed.get("votes") is None and parsed.get("percentage") is None:
        return None
    parsed["url"] = (
        "https://en.wikipedia.org/wiki/"
        + urllib.parse.quote(article_title.replace(" ", "_"))
    )
    return parsed


def _add_flips(winners: list[dict]) -> None:
    """Mutate: set `flip_from` when party changes between consecutive cycles."""
    for i in range(len(winners) - 1):
        cur = winners[i]
        prev = winners[i + 1]
        if cur.get("party") and prev.get("party") and cur["party"] != prev["party"]:
            cur["flip_from"] = prev["party"]


async def fetch_senate_past_winners(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    max_results: int = 4,
) -> list[dict]:
    """Fetch the most recent Senate election winners for a state.

    Enumerates the Wikipedia category for the state's Senate elections, picks the
    newest `max_results` articles, and parses each one's winner.
    """
    qualified = _wiki_state_name(state_postal, state_name)
    state_lower = state_name.lower()
    members = await _category_members(
        session, f"United States Senate elections in {qualified}", limit=500
    )
    elections: list[dict] = []
    for t in members:
        m = re.match(r"^(\d{4})\s", t)
        if not m:
            continue
        t_lower = t.lower()
        if state_lower not in t_lower:
            continue
        if any(skip in t_lower for skip in (
            "results by", "polling for", "endorsements", "candidates in",
        )):
            continue
        elections.append({"year": int(m.group(1)), "title": t})
    elections.sort(key=lambda e: -e["year"])
    elections = elections[:max_results]

    winners: list[dict] = []
    for el in elections:
        parsed = await _parse_statewide_winner(session, el["title"])
        if parsed:
            parsed["year"] = el["year"]
            parsed["race_type"] = "special" if "special" in el["title"].lower() else "regular"
            winners.append(parsed)
    _add_flips(winners)
    return winners


async def fetch_governor_past_winners(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    max_results: int = 4,
) -> list[dict]:
    """Fetch the most recent gubernatorial election winners for a state.

    Wikipedia pattern: '{State} gubernatorial elections' category →
    '{year} {State} gubernatorial election' articles.
    """
    qualified = _wiki_state_name(state_postal, state_name)
    state_lower = state_name.lower()
    members = await _category_members(
        session, f"{qualified} gubernatorial elections", limit=500
    )
    elections: list[dict] = []
    for t in members:
        m = re.match(r"^(\d{4})\s", t)
        if not m:
            continue
        t_lower = t.lower()
        if state_lower not in t_lower:
            continue
        if any(skip in t_lower for skip in (
            "results by", "polling for", "endorsements", "candidates in",
        )):
            continue
        elections.append({"year": int(m.group(1)), "title": t})
    elections.sort(key=lambda e: -e["year"])
    elections = elections[:max_results]

    winners: list[dict] = []
    for el in elections:
        parsed = await _parse_statewide_winner(session, el["title"])
        if parsed:
            parsed["year"] = el["year"]
            winners.append(parsed)
    _add_flips(winners)
    return winners


async def fetch_house_district_elections(
    session: aiohttp.ClientSession,
    state_postal: str,
    state_name: str,
    district: str,
    max_results: int = 6,
) -> list[dict]:
    """Return state-cycle House election summaries that cover this district.

    Wikipedia doesn't have per-district categories — every cycle has one
    state-level article (e.g. '2024 United States House of Representatives
    elections in Georgia'). We pull the most recent few of those, since they
    give a clean year-by-year history of the district's slot in the cycle.
    """
    qualified = _wiki_state_name(state_postal, state_name)
    state_lower = state_name.lower()
    members = await _category_members(
        session,
        f"United States House of Representatives elections in {qualified}",
        limit=500,
    )
    elections: list[dict] = []
    for t in members:
        m = re.match(r"^(\d{4})\s", t)
        if not m:
            continue
        t_lower = t.lower()
        if state_lower not in t_lower:
            continue  # drop cycle-wide articles that get parented in by mistake
        if any(skip in t_lower for skip in ("redistricting", "by county", "by congressional")):
            continue
        elections.append({"year": int(m.group(1)), "title": t})

    elections.sort(key=lambda e: -e["year"])
    elections = elections[:max_results]

    enriched: list[dict] = []
    for el in elections:
        summary = await _get_summary(session, el["title"])
        if not summary or not summary.get("extract"):
            continue
        resolved_title = (summary.get("title") or "").lower()
        if state_lower not in resolved_title:
            continue
        enriched.append({
            "year": el["year"],
            "type": "house_cycle",
            "title": summary.get("title"),
            "extract": summary.get("extract"),
            "url": summary.get("content_urls", {}).get("desktop", {}).get("page"),
            "thumbnail": summary.get("thumbnail", {}).get("source"),
        })
    return enriched
