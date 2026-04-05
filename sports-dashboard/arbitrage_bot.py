"""
Polymarket Arbitrage Signal Bot

Compares odds from The Odds API (bookmakers) with Polymarket CLOB prices
to find divergences that may represent arbitrage opportunities.

Signals only — no trading logic.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz, process
from rich.console import Console
from rich.table import Table

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
DIVERGENCE_THRESHOLD = float(os.getenv("DIVERGENCE_THRESHOLD", "10"))
SPORT_KEY = os.getenv("SPORT_KEY", "soccer_epl")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.json")

console = Console()

# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------

def fetch_odds(sport_key: str) -> list[dict]:
    """Fetch upcoming odds from The Odds API using h2h (moneyline) market."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",          # European (decimal) odds
        "markets": "h2h",         # moneyline / 1X2
        "oddsFormat": "decimal",
        "bookmakers": "pinnacle", # Pinnacle is sharpest book
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    console.print(f"[dim]Odds API: fetched {len(data)} events for {sport_key}[/dim]")
    return data


def parse_odds_events(raw_events: list[dict]) -> list[dict]:
    """
    Parse Odds API response into a clean list of events with implied probs.
    Each event has home_team, away_team, and outcomes with decimal odds + implied prob.
    """
    events = []
    for ev in raw_events:
        bookmakers = ev.get("bookmakers", [])
        if not bookmakers:
            continue
        # Use first bookmaker (we requested pinnacle only)
        bk = bookmakers[0]
        markets = bk.get("markets", [])
        if not markets:
            continue
        h2h = markets[0]  # h2h market
        outcomes = {}
        for o in h2h.get("outcomes", []):
            name = o["name"]
            price = o["price"]
            implied = 1.0 / price * 100  # implied probability %
            outcomes[name] = {"decimal_odds": price, "implied_prob": implied}

        events.append({
            "id": ev["id"],
            "home_team": ev["home_team"],
            "away_team": ev["away_team"],
            "commence_time": ev["commence_time"],
            "bookmaker": bk["key"],
            "outcomes": outcomes,  # keys: team names or "Draw"
        })
    return events


# ---------------------------------------------------------------------------
# Polymarket CLOB API
# ---------------------------------------------------------------------------

def fetch_polymarket_markets(search_term: str = "") -> list[dict]:
    """
    Fetch active markets from Polymarket CLOB API.
    Uses the /markets endpoint with a text search.
    """
    all_markets = []
    next_cursor = None

    # Search for sports / football / soccer related markets
    search_terms = [search_term] if search_term else ["EPL", "Premier League", "football", "soccer"]

    for term in search_terms:
        next_cursor = None
        for _ in range(5):  # max 5 pages per term
            url = f"{POLYMARKET_HOST}/markets"
            params = {"limit": 100, "active": "true"}
            if next_cursor:
                params["next_cursor"] = next_cursor

            try:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                console.print(f"[yellow]Warning: Polymarket API error: {e}[/yellow]")
                break

            markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            if not markets:
                break

            for m in markets:
                question = (m.get("question") or m.get("description") or "").lower()
                if any(kw in question for kw in [term.lower(), "epl", "premier league", "football", "soccer"]):
                    all_markets.append(m)

            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break

    # Deduplicate by condition_id
    seen = set()
    unique = []
    for m in all_markets:
        cid = m.get("condition_id", m.get("id", id(m)))
        if cid not in seen:
            seen.add(cid)
            unique.append(m)

    console.print(f"[dim]Polymarket: found {len(unique)} potentially matching markets[/dim]")
    return unique


def parse_polymarket_markets(markets: list[dict]) -> list[dict]:
    """
    Parse Polymarket markets into a structured format.
    Each market has a question, tokens with prices (= implied prob).
    """
    parsed = []
    for m in markets:
        question = m.get("question") or m.get("description") or ""
        tokens = m.get("tokens", [])

        outcomes = {}
        for t in tokens:
            outcome_name = t.get("outcome", "")
            price = float(t.get("price", 0))
            outcomes[outcome_name] = {
                "price": price,
                "implied_prob": price * 100,  # price on Polymarket IS the implied prob
                "token_id": t.get("token_id", ""),
            }

        if outcomes:
            parsed.append({
                "condition_id": m.get("condition_id", ""),
                "question": question,
                "outcomes": outcomes,
                "end_date": m.get("end_date_iso", ""),
            })
    return parsed


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def normalize_team_name(name: str) -> str:
    """Normalize team names for fuzzy matching."""
    # Common abbreviations / expansions
    replacements = {
        "man utd": "manchester united",
        "man city": "manchester city",
        "man united": "manchester united",
        "spurs": "tottenham hotspur",
        "tottenham": "tottenham hotspur",
        "wolves": "wolverhampton wanderers",
        "wolverhampton": "wolverhampton wanderers",
        "newcastle utd": "newcastle united",
        "brighton": "brighton and hove albion",
        "west ham": "west ham united",
        "nott'm forest": "nottingham forest",
        "nottm forest": "nottingham forest",
        "leicester": "leicester city",
        "ipswich": "ipswich town",
        "crystal palace": "crystal palace",
        "afc bournemouth": "bournemouth",
        "luton": "luton town",
    }
    lower = name.lower().strip()
    return replacements.get(lower, lower)


def match_events_to_markets(
    odds_events: list[dict],
    poly_markets: list[dict],
    score_threshold: int = 60,
) -> list[dict]:
    """
    Match Odds API events to Polymarket markets via fuzzy matching on team names.
    Returns list of matched pairs with both sets of implied probs.
    """
    matched = []

    for event in odds_events:
        home = normalize_team_name(event["home_team"])
        away = normalize_team_name(event["away_team"])
        match_str = f"{home} {away}"

        # Build choices from poly markets
        choices = []
        for pm in poly_markets:
            q = pm["question"].lower()
            choices.append(q)

        if not choices:
            continue

        result = process.extractOne(match_str, choices, scorer=fuzz.token_set_ratio)
        if result is None:
            continue

        best_match, score, idx = result
        if score < score_threshold:
            continue

        poly = poly_markets[idx]
        matched.append({
            "odds_event": event,
            "poly_market": poly,
            "match_score": score,
        })

    console.print(f"[dim]Matched {len(matched)} events between sources[/dim]")
    return matched


# ---------------------------------------------------------------------------
# Divergence analysis
# ---------------------------------------------------------------------------

def analyze_divergences(matched: list[dict], threshold: float) -> list[dict]:
    """
    Compare implied probabilities and flag divergences above threshold.
    """
    signals = []

    for pair in matched:
        event = pair["odds_event"]
        poly = pair["poly_market"]

        # Try to match individual outcomes
        for outcome_name, odds_data in event["outcomes"].items():
            odds_prob = odds_data["implied_prob"]

            # Find matching outcome in Polymarket
            # Polymarket outcomes are often "Yes"/"No" for binary markets,
            # or team names for multi-outcome markets
            poly_prob = None
            poly_outcome_key = None

            norm_outcome = normalize_team_name(outcome_name)

            for pk, pv in poly["outcomes"].items():
                norm_pk = normalize_team_name(pk)
                # Direct match or fuzzy match
                if (fuzz.ratio(norm_outcome, norm_pk) > 70
                        or norm_outcome in norm_pk
                        or norm_pk in norm_outcome):
                    poly_prob = pv["implied_prob"]
                    poly_outcome_key = pk
                    break

            # For binary Yes/No markets, "Yes" usually corresponds to a win
            if poly_prob is None and len(poly["outcomes"]) == 2:
                yes_outcome = poly["outcomes"].get("Yes")
                no_outcome = poly["outcomes"].get("No")
                if yes_outcome and outcome_name.lower() in poly["question"].lower():
                    poly_prob = yes_outcome["implied_prob"]
                    poly_outcome_key = "Yes"

            if poly_prob is None:
                continue

            divergence = odds_prob - poly_prob  # positive = poly is cheap
            abs_div = abs(divergence)

            if abs_div >= threshold:
                cheap_side = "Polymarket" if divergence > 0 else "Bookmaker"
                signals.append({
                    "event": f"{event['home_team']} vs {event['away_team']}",
                    "commence_time": event["commence_time"],
                    "outcome": outcome_name,
                    "bookmaker_prob": round(odds_prob, 2),
                    "polymarket_prob": round(poly_prob, 2),
                    "divergence_pct": round(abs_div, 2),
                    "cheap_on": cheap_side,
                    "poly_question": poly["question"],
                    "match_score": pair["match_score"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    return signals


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def display_signals(signals: list[dict]):
    """Print signals as a rich table."""
    if not signals:
        console.print("[green]No divergences above threshold found.[/green]")
        return

    table = Table(title="Arbitrage Signals", show_lines=True)
    table.add_column("Event", style="bold")
    table.add_column("Outcome")
    table.add_column("Book Prob %", justify="right")
    table.add_column("Poly Prob %", justify="right")
    table.add_column("Divergence %", justify="right", style="bold yellow")
    table.add_column("Cheap On", style="bold green")

    for s in sorted(signals, key=lambda x: x["divergence_pct"], reverse=True):
        table.add_row(
            s["event"],
            s["outcome"],
            f"{s['bookmaker_prob']:.1f}",
            f"{s['polymarket_prob']:.1f}",
            f"{s['divergence_pct']:.1f}",
            s["cheap_on"],
        )

    console.print(table)


def save_signals(signals: list[dict]):
    """Append signals to signals.json."""
    existing = []
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = []

    existing.extend(signals)

    with open(SIGNALS_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    if signals:
        console.print(f"[dim]Saved {len(signals)} signals to {SIGNALS_FILE}[/dim]")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once():
    """Single scan cycle."""
    console.rule(f"[bold]Scan at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}[/bold]")

    # 1. Fetch odds
    try:
        raw_odds = fetch_odds(SPORT_KEY)
    except requests.HTTPError as e:
        console.print(f"[red]Odds API error: {e}[/red]")
        return []
    except Exception as e:
        console.print(f"[red]Odds API error: {e}[/red]")
        return []

    odds_events = parse_odds_events(raw_odds)
    if not odds_events:
        console.print("[yellow]No odds events found.[/yellow]")
        return []

    # 2. Fetch Polymarket markets
    poly_raw = fetch_polymarket_markets()
    poly_markets = parse_polymarket_markets(poly_raw)
    if not poly_markets:
        console.print("[yellow]No matching Polymarket markets found.[/yellow]")
        return []

    # 3. Match events
    matched = match_events_to_markets(odds_events, poly_markets)
    if not matched:
        console.print("[yellow]No matches found between odds events and Polymarket.[/yellow]")
        return []

    # 4. Analyze divergences
    signals = analyze_divergences(matched, DIVERGENCE_THRESHOLD)

    # 5. Display & save
    display_signals(signals)
    save_signals(signals)

    return signals


def main():
    """Main entry point — runs scan loop."""
    if not ODDS_API_KEY:
        console.print("[red]Error: ODDS_API_KEY not set. Copy .env.example to .env and add your key.[/red]")
        return

    console.print(f"[bold]Polymarket Arbitrage Signal Bot[/bold]")
    console.print(f"Sport: {SPORT_KEY} | Threshold: {DIVERGENCE_THRESHOLD}% | Poll: {POLL_INTERVAL}s")
    console.print()

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            console.print("\n[bold]Stopped.[/bold]")
            break
        except Exception as e:
            console.print(f"[red]Unexpected error: {e}[/red]")

        console.print(f"\n[dim]Next scan in {POLL_INTERVAL}s... (Ctrl+C to stop)[/dim]\n")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            console.print("\n[bold]Stopped.[/bold]")
            break


if __name__ == "__main__":
    main()
