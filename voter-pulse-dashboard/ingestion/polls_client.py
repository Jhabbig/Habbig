"""Polling aggregator — approval and generic ballot.

Pulls archived individual-poll CSVs from the 538 (now-shuttered) data hub
and aggregates them to monthly means. Why 538: it's the best-curated
free, structured aggregation of polls, and the static CSVs continue to
serve historically even now that the site is gone.

If a fetch fails (network blocked, file moved, etc.) we degrade gracefully
to an empty payload so the dashboard still renders.

Two outputs:
  approval  : monthly mean of `approve` and `disapprove` percentages over
              all president-approval polls in that month, plus pollster count.
  generic_ballot : same idea over the generic-ballot file ("if the election
                   were today, would you vote D or R for Congress?").

Both are returned as a list of `{date: "YYYY-MM-01", ...}` rows so the
front end can chart them on the same timeline as the FRED series.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
import urllib.request
from collections import defaultdict
from threading import Lock

log = logging.getLogger(__name__)

_UA = "voter-pulse-dashboard/0.2"

DEFAULT_APPROVAL_URL = os.environ.get(
    "VOTER_PULSE_APPROVAL_CSV",
    "https://projects.fivethirtyeight.com/polls/data/president_approval_polls.csv",
)
DEFAULT_GENERIC_BALLOT_URL = os.environ.get(
    "VOTER_PULSE_GENERIC_BALLOT_CSV",
    "https://projects.fivethirtyeight.com/polls/data/generic_ballot_polls.csv",
)


def _fetch_csv(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read().decode("utf-8", errors="replace")


def _parse_date_to_month(s: str) -> str | None:
    """538 ships dates as `M/D/YYYY` (or sometimes `YYYY-MM-DD`). Return
    `YYYY-MM-01` so we can group by month, or None if we can't parse it."""
    if not s:
        return None
    s = s.strip()
    if "-" in s and len(s) >= 10:  # ISO-ish
        return s[:7] + "-01"
    if "/" in s:
        try:
            m, d, y = s.split("/")
            m = int(m)
            y = int(y)
            if y < 100:
                y += 2000 if y < 50 else 1900
            return f"{y:04d}-{m:02d}-01"
        except (ValueError, IndexError):
            return None
    return None


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fetch_approval_monthly() -> list[dict]:
    """Aggregate the president-approval CSV into monthly means.

    Columns of interest in the 538 schema:
      start_date / end_date : poll window
      yes / no              : (in older files)
      approve / disapprove  : (newer files; we accept both)
      politician            : president name
    We average across pollsters, weighted by 1 per poll (not by sample size —
    we don't want a single huge online poll to dominate a month).
    """
    try:
        body = _fetch_csv(DEFAULT_APPROVAL_URL)
    except Exception as exc:
        log.warning("approval CSV fetch failed: %s", exc)
        return []

    reader = csv.DictReader(io.StringIO(body))
    monthly: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"approve": [], "disapprove": []}
    )
    for row in reader:
        date_field = row.get("end_date") or row.get("start_date") or row.get("modeldate")
        month = _parse_date_to_month(date_field or "")
        if not month:
            continue
        politician = (row.get("politician") or row.get("president") or "").strip()
        a = _num(row.get("approve") or row.get("yes") or row.get("approve_estimate"))
        d = _num(row.get("disapprove") or row.get("no") or row.get("disapprove_estimate"))
        if a is None or d is None:
            continue
        key = (month, politician)
        monthly[key]["approve"].append(a)
        monthly[key]["disapprove"].append(d)

    out: list[dict] = []
    for (month, politician), vals in sorted(monthly.items()):
        if not vals["approve"]:
            continue
        out.append({
            "date": month,
            "politician": politician,
            "approve": sum(vals["approve"]) / len(vals["approve"]),
            "disapprove": sum(vals["disapprove"]) / len(vals["disapprove"]),
            "n_polls": len(vals["approve"]),
        })
    return out


def fetch_generic_ballot_monthly() -> list[dict]:
    """Aggregate the generic-ballot CSV. Schema has `dem` and `rep` columns
    (or `democrat` / `republican` in some vintages)."""
    try:
        body = _fetch_csv(DEFAULT_GENERIC_BALLOT_URL)
    except Exception as exc:
        log.warning("generic-ballot CSV fetch failed: %s", exc)
        return []

    reader = csv.DictReader(io.StringIO(body))
    monthly: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"dem": [], "rep": []}
    )
    for row in reader:
        date_field = row.get("end_date") or row.get("start_date") or row.get("modeldate")
        month = _parse_date_to_month(date_field or "")
        if not month:
            continue
        dem = _num(row.get("dem") or row.get("democrat") or row.get("dem_estimate"))
        rep = _num(row.get("rep") or row.get("republican") or row.get("rep_estimate"))
        if dem is None or rep is None:
            continue
        monthly[month]["dem"].append(dem)
        monthly[month]["rep"].append(rep)

    out: list[dict] = []
    for month, vals in sorted(monthly.items()):
        if not vals["dem"]:
            continue
        out.append({
            "date": month,
            "dem": sum(vals["dem"]) / len(vals["dem"]),
            "rep": sum(vals["rep"]) / len(vals["rep"]),
            "margin_d_minus_r": (
                sum(vals["dem"]) / len(vals["dem"])
                - sum(vals["rep"]) / len(vals["rep"])
            ),
            "n_polls": len(vals["dem"]),
        })
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 6 * 3600  # polls aggregate slowly — 6h is plenty
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"] is not None
        if fresh and not force and _CACHE["fetched_at"]:
            return {**_CACHE["data"], "fetched_at": _CACHE["fetched_at"]}

    approval = fetch_approval_monthly()
    generic = fetch_generic_ballot_monthly()
    data = {
        "approval": approval,
        "generic_ballot": generic,
        "latest_approval": approval[-1] if approval else None,
        "latest_generic_ballot": generic[-1] if generic else None,
    }
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return {**data, "fetched_at": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_cached(force=True)
    print(f"approval months: {len(out['approval'])}")
    print(f"generic-ballot months: {len(out['generic_ballot'])}")
    if out["latest_approval"]:
        la = out["latest_approval"]
        print(f"latest approval ({la['politician']} {la['date']}): "
              f"approve {la['approve']:.1f} disapprove {la['disapprove']:.1f} "
              f"({la['n_polls']} polls)")
