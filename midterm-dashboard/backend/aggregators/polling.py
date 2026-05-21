import aiohttp
import asyncio
import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 538 was shut down by Disney in March 2025 but the static CSV endpoints kept
# serving for a while afterwards. We try them first (free, no key), and if the
# fetch returns 404 / 403 / 5xx we fall back to a lightweight RealClearPolitics
# scrape. Production deployments that need authoritative live polling should
# add a paid source (Decision Desk HQ, Cook Political, or polling.com) and
# wire it in alongside these.
FIVETHIRTYEIGHT_URLS = {
    "senate": "https://projects.fivethirtyeight.com/polls-page/data/senate_polls.csv",
    "house": "https://projects.fivethirtyeight.com/polls-page/data/house_polls.csv",
    "governor": "https://projects.fivethirtyeight.com/polls-page/data/governor_polls.csv",
    "generic_ballot": "https://projects.fivethirtyeight.com/polls-page/data/generic_ballot_polls.csv",
}

# RealClearPolling (renamed from RealClearPolitics) — public polling tables.
# These pages are HTML; we extract embedded JSON.
RCP_URLS = {
    "generic_ballot": "https://www.realclearpolling.com/polls/2026-generic-congressional-vote",
    "senate_overview": "https://www.realclearpolling.com/polls/2026-senate",
    "house_overview": "https://www.realclearpolling.com/polls/2026-house",
}


class PollingAggregator:
    """Fetches polling data from multiple public sources.

    Order of precedence per poll type:
      1. 538 CSV (legacy, may 404 post-shutdown)
      2. RealClearPolling HTML (current, scraped)
    Each layer is tried independently — a partial 538 outage doesn't block
    the others.
    """

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None
        self._polling_cache = {}
        self._cache_time = None
        self._cache_ttl = 3600  # 1 hour

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_all_polls(self) -> dict:
        """Fetch all available polling data, merging multiple sources."""
        now = datetime.now(timezone.utc)
        if self._cache_time and (now - self._cache_time).total_seconds() < self._cache_ttl:
            return self._polling_cache

        results: dict[str, list[dict]] = {}

        # Fetch all 538 CSVs in parallel
        fivethirtyeight_tasks = {
            poll_type: asyncio.create_task(self._fetch_538_csv(url, poll_type))
            for poll_type, url in FIVETHIRTYEIGHT_URLS.items()
        }
        for poll_type, task in fivethirtyeight_tasks.items():
            try:
                polls = await task
            except Exception as e:
                logger.warning(f"538 {poll_type} fetch raised: {e}")
                polls = []
            if polls:
                results.setdefault(poll_type, []).extend(polls)
                logger.info(f"Fetched {len(polls)} {poll_type} polls from 538")

        # RealClearPolling fallback / supplement
        rcp_tasks = {
            poll_type: asyncio.create_task(self._fetch_rcp(url, poll_type))
            for poll_type, url in RCP_URLS.items()
        }
        for poll_type, task in rcp_tasks.items():
            try:
                polls = await task
            except Exception as e:
                logger.warning(f"RCP {poll_type} fetch raised: {e}")
                polls = []
            if polls:
                results.setdefault(poll_type, []).extend(polls)
                logger.info(f"Fetched {len(polls)} {poll_type} polls from RealClearPolling")

        # Sources with no data still get logged but excluded from results
        if not results:
            logger.warning(
                "All polling sources returned empty. Check that 538 / RCP "
                "endpoints are reachable, or configure a paid replacement."
            )

        self._polling_cache = results
        self._cache_time = now
        return results

    async def _fetch_538_csv(self, url: str, poll_type: str) -> list[dict]:
        """Fetch and parse a 538 CSV polling file."""
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.info(f"538 {poll_type} CSV returned {resp.status} — falling through to other sources")
                    return []
                text = await resp.text()
                return self._parse_538_csv(text, poll_type)
        except Exception as e:
            logger.warning(f"538 CSV fetch error ({poll_type}): {e}")
            return []

    async def _fetch_rcp(self, url: str, poll_type: str) -> list[dict]:
        """Fetch RealClearPolling page and extract polls from embedded data.

        RCP no longer ships a public JSON API; the page is rendered server-side
        and serializes the poll table into a Next.js ``__NEXT_DATA__`` JSON
        blob. We extract that and pull out the row data. If the markup ever
        changes this returns an empty list and logs a warning, but the
        dashboard keeps running on the other sources.
        """
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), headers={
                "User-Agent": "Mozilla/5.0 (compatible; narve.ai/midterm-dashboard)",
            }) as resp:
                if resp.status != 200:
                    logger.info(f"RCP {poll_type} returned {resp.status}")
                    return []
                html = await resp.text()
                return self._parse_rcp_html(html, poll_type)
        except Exception as e:
            logger.warning(f"RCP fetch error ({poll_type}): {e}")
            return []

    @staticmethod
    def _parse_rcp_html(html: str, poll_type: str) -> list[dict]:
        """Pull poll rows out of a RealClearPolling page.

        Strategy: locate the ``__NEXT_DATA__`` script tag, parse it, and walk
        the serialized poll rows. RCP's exact JSON shape varies by page so we
        defensively extract any list of dicts that look like poll rows
        (containing pollster + sample size + dates + percentages).
        """
        import json as _json

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>',
            html,
        )
        if not m:
            logger.debug(f"RCP {poll_type}: __NEXT_DATA__ not found")
            return []

        try:
            blob = _json.loads(m.group(1))
        except _json.JSONDecodeError:
            logger.debug(f"RCP {poll_type}: __NEXT_DATA__ unparseable")
            return []

        # Walk the JSON looking for arrays of poll-like dicts.
        polls: list[dict] = []

        def looks_like_poll(d: dict) -> bool:
            keys = {k.lower() for k in d.keys()}
            return bool({"pollster", "sample", "spread", "moe"} & keys) or bool(
                {"pollster_name", "sample_size", "date"} & keys
            )

        def walk(node):
            if isinstance(node, list):
                if node and all(isinstance(x, dict) for x in node) and any(looks_like_poll(x) for x in node):
                    polls.extend(node)
                else:
                    for x in node:
                        walk(x)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(blob)

        normalized: list[dict] = []
        for raw in polls:
            try:
                pollster = raw.get("pollster") or raw.get("pollster_name") or ""
                sample_size = raw.get("sample_size") or raw.get("sample") or ""
                end_date = raw.get("end_date") or raw.get("date") or raw.get("date_end") or ""
                start_date = raw.get("start_date") or raw.get("date_start") or ""

                # RCP rows often store candidate values under dynamic keys —
                # capture every numeric percent we can find.
                averages: dict[str, float] = {}
                for k, v in raw.items():
                    if k.lower() in {"pollster", "pollster_name", "moe", "sample_size",
                                       "sample", "date", "start_date", "end_date",
                                       "date_start", "date_end", "type", "spread"}:
                        continue
                    if isinstance(v, (int, float)):
                        averages[str(k)] = float(v)
                    elif isinstance(v, str):
                        try:
                            averages[str(k)] = float(v.strip().rstrip("%"))
                        except ValueError:
                            pass

                # Emit one row per candidate so the data shape matches 538's.
                for cand, pct in averages.items():
                    if pct == 0:
                        continue
                    normalized.append({
                        "poll_type": poll_type,
                        "state": "National",
                        "candidate": cand,
                        "party": "",
                        "percentage": pct,
                        "pollster": str(pollster).strip(),
                        "sample_size": int(sample_size) if str(sample_size).isdigit() else None,
                        "population": "",
                        "start_date": str(start_date),
                        "end_date": str(end_date),
                        "race_id": "",
                        "source": "rcp",
                    })
            except (ValueError, TypeError, AttributeError):
                continue

        return normalized

    def _parse_538_csv(self, csv_text: str, poll_type: str) -> list[dict]:
        """Parse 538 CSV into normalized poll records."""
        polls = []
        reader = csv.DictReader(io.StringIO(csv_text))

        for row in reader:
            try:
                # 538 CSVs have varying column names
                state = row.get("state", "").strip()
                candidate = row.get("candidate_name") or row.get("answer") or ""
                party = row.get("party") or ""
                pct = row.get("pct") or row.get("yes") or "0"

                pollster = row.get("pollster") or row.get("sponsor") or ""
                sample_size = row.get("sample_size") or row.get("n") or ""

                end_date_str = row.get("end_date") or row.get("enddate") or ""
                start_date_str = row.get("start_date") or row.get("startdate") or ""

                population = row.get("population") or ""  # lv, rv, a

                poll = {
                    "poll_type": poll_type,
                    "state": state or "National",
                    "candidate": candidate.strip(),
                    "party": party.strip(),
                    "percentage": float(pct) if pct else 0,
                    "pollster": pollster.strip(),
                    "sample_size": int(sample_size) if sample_size and sample_size.isdigit() else None,
                    "population": population.strip(),
                    "start_date": start_date_str,
                    "end_date": end_date_str,
                    "race_id": row.get("race_id") or row.get("question_id") or "",
                    "source": "538",
                }
                polls.append(poll)
            except (ValueError, KeyError) as e:
                continue

        return polls

    def compute_polling_average(self, polls: list[dict], state: str = None,
                                 race_type: str = None, recent_days: int = 30) -> dict:
        """Compute a polling average from raw poll data."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)

        filtered = []
        for p in polls:
            if state and p.get("state", "").lower() != state.lower():
                continue

            end_date_str = p.get("end_date", "")
            try:
                # Try multiple date formats
                for fmt in ["%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"]:
                    try:
                        end_date = datetime.strptime(end_date_str, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                else:
                    continue

                if end_date >= cutoff:
                    filtered.append(p)
            except Exception:
                continue

        if not filtered:
            return {}

        # Group by candidate/party
        by_candidate = {}
        for p in filtered:
            key = p.get("candidate") or p.get("party") or "Unknown"
            if key not in by_candidate:
                by_candidate[key] = {"total": 0, "count": 0, "party": p.get("party", "")}
            by_candidate[key]["total"] += p.get("percentage", 0)
            by_candidate[key]["count"] += 1

        averages = {}
        for candidate, data in by_candidate.items():
            if data["count"] > 0:
                averages[candidate] = {
                    "average": round(data["total"] / data["count"], 1),
                    "num_polls": data["count"],
                    "party": data["party"]
                }

        return {
            "state": state or "National",
            "race_type": race_type,
            "num_polls": len(filtered),
            "averages": averages,
            "period_days": recent_days,
        }

    def compute_race_summary(self, polls: list[dict], state: str) -> dict:
        """Compute a summary for a specific race showing leader and margin."""
        avg = self.compute_polling_average(polls, state=state)
        if not avg or not avg.get("averages"):
            return {"state": state, "leader": None, "margin": 0, "num_polls": 0}

        sorted_candidates = sorted(
            avg["averages"].items(),
            key=lambda x: x[1]["average"],
            reverse=True
        )

        leader = sorted_candidates[0]
        runner_up = sorted_candidates[1] if len(sorted_candidates) > 1 else None
        margin = leader[1]["average"] - (runner_up[1]["average"] if runner_up else 0)

        return {
            "state": state,
            "leader": leader[0],
            "leader_party": leader[1]["party"],
            "leader_avg": leader[1]["average"],
            "runner_up": runner_up[0] if runner_up else None,
            "runner_up_party": runner_up[1]["party"] if runner_up else None,
            "runner_up_avg": runner_up[1]["average"] if runner_up else 0,
            "margin": round(margin, 1),
            "num_polls": avg["num_polls"],
        }
