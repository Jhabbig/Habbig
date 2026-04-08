import aiohttp
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 538 CSV data feeds (may still be live post-shutdown)
FIVETHIRTYEIGHT_URLS = {
    "senate": "https://projects.fivethirtyeight.com/polls-page/data/senate_polls.csv",
    "house": "https://projects.fivethirtyeight.com/polls-page/data/house_polls.csv",
    "governor": "https://projects.fivethirtyeight.com/polls-page/data/governor_polls.csv",
    "generic_ballot": "https://projects.fivethirtyeight.com/polls-page/data/generic_ballot_polls.csv",
}

class PollingAggregator:
    """Fetches polling data from 538 CSVs and RealClearPolitics."""

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
        """Fetch all available polling data."""
        now = datetime.now(timezone.utc)
        if self._cache_time and (now - self._cache_time).total_seconds() < self._cache_ttl:
            return self._polling_cache

        results = {}
        for poll_type, url in FIVETHIRTYEIGHT_URLS.items():
            polls = await self._fetch_538_csv(url, poll_type)
            if polls:
                results[poll_type] = polls
                logger.info(f"Fetched {len(polls)} {poll_type} polls from 538")
            else:
                logger.warning(f"No {poll_type} polls available from 538")

        if results:
            self._polling_cache = results
            self._cache_time = now

        return results

    async def _fetch_538_csv(self, url: str, poll_type: str) -> list[dict]:
        """Fetch and parse a 538 CSV polling file."""
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning(f"538 {poll_type} CSV returned {resp.status}")
                    return []
                text = await resp.text()
                return self._parse_538_csv(text, poll_type)
        except Exception as e:
            logger.error(f"538 CSV fetch error ({poll_type}): {e}")
            return []

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
