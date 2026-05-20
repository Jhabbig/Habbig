"""OFAC SDN delta tracker — v1.4.

Fetches Treasury's SDN list daily, parses to a compact per-entry digest
(uid + name + type + programs + country), persists today's snapshot to
disk, and computes the delta vs the most recent prior snapshot.

Source: https://www.treasury.gov/ofac/downloads/sdn.xml

The full XML is ~50MB+ with ~14k entries. We `iterparse` it (via
defusedxml — same XXE protection as the RSS parser) and `.clear()`
each `sdnEntry` after digesting so peak memory stays bounded. Per-day
digests serialize to ~2-3MB JSON; we keep the last 14 and prune older.

Cache: 12 h on the fetch path. OFAC publishes the list roughly weekly
plus ad-hoc same-day for breaking sanctions packages. Tighter polling
wastes bandwidth without surfacing new signal.

First-boot semantics: if no prior snapshot exists on disk, the delta
returns `first_snapshot=true` with empty add/remove arrays so the UI
renders "no prior snapshot to diff" rather than implying zero changes.

Persistence path is configurable via `SDN_SNAPSHOT_DIR` env var. In
Docker, mount a persistent volume there so deltas survive restart;
otherwise the default `tempfile.gettempdir()` path works for local
dev (deltas reseed from scratch after restart, which is honest).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import date
from threading import Lock

try:
    from defusedxml.ElementTree import iterparse as xml_iterparse
except ImportError:  # pragma: no cover
    raise ImportError("defusedxml is required: pip install defusedxml")

log = logging.getLogger(__name__)

SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
UA = "regulators-dashboard/1.4"

# Default snapshot dir lives under tempfile root unless overridden — production
# deployments should set SDN_SNAPSHOT_DIR to a mounted persistent volume so
# day-over-day deltas survive container restart.
SNAPSHOT_DIR = os.environ.get(
    "SDN_SNAPSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "regulators-sdn-snapshots"),
)
SNAPSHOT_KEEP_DAYS = 14

_CACHE_TTL = 12 * 3600
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


# ── XML parsing ────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _normalize_date(s: str | None) -> str | None:
    """OFAC publish_date is "MM/DD/YYYY" — normalize to ISO YYYY-MM-DD."""
    if not s:
        return None
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return s.strip()


def _digest_entry(elem) -> dict | None:
    uid = None
    sdn_type = None
    first = last = None
    programs: list[str] = []
    country = None
    for child in elem:
        tag = _local(child.tag)
        if tag == "uid":
            uid = (child.text or "").strip()
        elif tag == "sdntype":
            sdn_type = (child.text or "").strip()
        elif tag == "firstname":
            first = (child.text or "").strip()
        elif tag == "lastname":
            last = (child.text or "").strip()
        elif tag == "programlist":
            for prog in child:
                if _local(prog.tag) == "program" and prog.text:
                    programs.append(prog.text.strip())
        elif tag == "addresslist":
            # Address > country (first non-empty country wins)
            for addr in child:
                if country:
                    break
                for addr_child in addr:
                    if _local(addr_child.tag) == "country" and addr_child.text:
                        country = addr_child.text.strip()
                        break
    if not uid:
        return None
    if first and last:
        name = f"{first} {last}"
    elif last:
        name = last
    elif first:
        name = first
    else:
        name = "(unknown)"
    return {
        "uid": uid,
        "name": name,
        "type": sdn_type or "",
        "programs": programs,
        "country": country or "",
    }


def parse_xml(xml_bytes: bytes) -> dict:
    """Stream-parse the SDN XML to a compact dict.

    Returns:
        {publish_date: "YYYY-MM-DD"|None, record_count: int|None,
         entries: [{uid, name, type, programs, country}, ...]}
    """
    publish_date: str | None = None
    record_count: int | None = None
    entries: list[dict] = []
    # iterparse with events=("end",) so each closing tag fires once
    src = io.BytesIO(xml_bytes)
    for event, elem in xml_iterparse(src, events=("end",)):
        tag = _local(elem.tag)
        if tag == "publish_date" and publish_date is None:
            publish_date = _normalize_date(elem.text)
        elif tag == "record_count" and record_count is None:
            try:
                record_count = int((elem.text or "").strip())
            except (TypeError, ValueError):
                pass
        elif tag == "sdnentry":
            digest = _digest_entry(elem)
            if digest:
                entries.append(digest)
            elem.clear()  # release memory now that we've digested
    return {
        "publish_date": publish_date,
        "record_count": record_count,
        "entries": entries,
    }


# ── Persistence ────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _snapshot_path(publish_date: str) -> str:
    safe = re.sub(r"[^0-9-]", "", publish_date)
    return os.path.join(SNAPSHOT_DIR, f"{safe}.json")


def persist(snapshot: dict) -> str | None:
    """Atomically write today's snapshot keyed by publish_date. Returns the
    path, or None if no publish_date was parseable."""
    pd = snapshot.get("publish_date")
    if not pd:
        return None
    _ensure_dir()
    path = _snapshot_path(pd)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f)
    os.replace(tmp, path)
    return path


def prior_snapshot(today_publish_date: str) -> dict | None:
    """Return the most-recent on-disk snapshot strictly older than
    `today_publish_date`, or None if none exist."""
    _ensure_dir()
    try:
        today = date.fromisoformat(today_publish_date)
    except ValueError:
        return None
    candidates: list[tuple[date, str]] = []
    for f in os.listdir(SNAPSHOT_DIR):
        if not f.endswith(".json"):
            continue
        try:
            d = date.fromisoformat(f[:-5])
        except ValueError:
            continue
        if d < today:
            candidates.append((d, f))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, filename = candidates[0]
    try:
        with open(os.path.join(SNAPSHOT_DIR, filename)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to load prior SDN snapshot %s: %s", filename, exc)
        return None


def prune(keep: int = SNAPSHOT_KEEP_DAYS) -> int:
    """Delete snapshots older than the `keep` most-recent. Returns count deleted."""
    _ensure_dir()
    files: list[tuple[date, str]] = []
    for f in os.listdir(SNAPSHOT_DIR):
        if not f.endswith(".json"):
            continue
        try:
            files.append((date.fromisoformat(f[:-5]), f))
        except ValueError:
            continue
    files.sort(reverse=True)
    deleted = 0
    for _, f in files[keep:]:
        try:
            os.remove(os.path.join(SNAPSHOT_DIR, f))
            deleted += 1
        except OSError:
            pass
    return deleted


# ── Delta ──────────────────────────────────────────────────────────────────

def compute_delta(today: dict, yesterday: dict | None) -> dict:
    """Diff today's entries against yesterday's by uid.

    First-snapshot path: returns empty arrays with `first_snapshot=true`."""
    if not yesterday:
        return {
            "first_snapshot": True,
            "added": [],
            "removed": [],
            "added_count": 0,
            "removed_count": 0,
            "program_deltas": [],
            "yesterday_publish_date": None,
        }
    today_uids = {e["uid"] for e in today["entries"]}
    yesterday_by_uid = {e["uid"]: e for e in yesterday["entries"]}
    yesterday_uids = set(yesterday_by_uid.keys())
    added_uids = today_uids - yesterday_uids
    removed_uids = yesterday_uids - today_uids
    added = [e for e in today["entries"] if e["uid"] in added_uids]
    removed = [yesterday_by_uid[u] for u in removed_uids]

    program_deltas: dict[str, dict] = defaultdict(lambda: {"added": 0, "removed": 0})
    for e in added:
        for p in e["programs"]:
            program_deltas[p]["added"] += 1
    for e in removed:
        for p in e["programs"]:
            program_deltas[p]["removed"] += 1
    program_rows = [
        {"program": p, **counts}
        for p, counts in program_deltas.items()
    ]
    program_rows.sort(key=lambda r: -(r["added"] + r["removed"]))

    return {
        "first_snapshot": False,
        "added": added,
        "removed": removed,
        "added_count": len(added),
        "removed_count": len(removed),
        "program_deltas": program_rows,
        "yesterday_publish_date": yesterday.get("publish_date"),
    }


# ── Fetch + cache ──────────────────────────────────────────────────────────

def _fetch_xml(timeout: float = 90.0) -> bytes:
    req = urllib.request.Request(
        SDN_URL,
        headers={"User-Agent": UA, "Accept": "application/xml, text/xml"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        # SDN XML is ~50MB. Allow up to 200MB defensively.
        return resp.read(200_000_000)


def fetch_and_diff() -> dict:
    """End-to-end: fetch, parse, persist today, diff vs prior, prune.

    Returns a dict with `ok`, `today`, `delta`, `error`. The full entry
    lists live on `today["entries"]` and `delta["added"|"removed"]`; the
    caller (server) trims to top-N for the response."""
    try:
        raw = _fetch_xml()
    except Exception as exc:
        log.warning("OFAC fetch failed: %s", exc)
        return {"ok": False, "today": None, "delta": None, "error": str(exc)}
    try:
        today = parse_xml(raw)
    except Exception as exc:
        log.warning("OFAC parse failed: %s", exc)
        return {"ok": False, "today": None, "delta": None, "error": f"parse: {exc}"}
    if not today.get("publish_date"):
        return {"ok": False, "today": today, "delta": None, "error": "missing publish_date"}
    yesterday = prior_snapshot(today["publish_date"])
    persist(today)
    prune()
    return {
        "ok": True,
        "today": today,
        "delta": compute_delta(today, yesterday),
        "error": None,
    }


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    payload = fetch_and_diff()
    payload["fetched_at"] = now
    with _lock:
        _CACHE["data"] = payload
        _CACHE["fetched_at"] = now
    return payload


# --- Self-test --------------------------------------------------------------

_FIXTURE_XML_DAY1 = b"""<?xml version="1.0"?>
<sdnList>
  <publshInformation>
    <Publish_Date>05/18/2026</Publish_Date>
    <Record_Count>2</Record_Count>
  </publshInformation>
  <sdnEntry>
    <uid>1001</uid>
    <firstName>Ivan</firstName>
    <lastName>Petrov</lastName>
    <sdnType>Individual</sdnType>
    <programList>
      <program>RUSSIA-EO14024</program>
    </programList>
    <addressList>
      <address><country>Russia</country></address>
    </addressList>
  </sdnEntry>
  <sdnEntry>
    <uid>1002</uid>
    <lastName>Acme Holdings LLC</lastName>
    <sdnType>Entity</sdnType>
    <programList>
      <program>SDGT</program>
    </programList>
  </sdnEntry>
</sdnList>
"""

_FIXTURE_XML_DAY2 = b"""<?xml version="1.0"?>
<sdnList>
  <publshInformation>
    <Publish_Date>05/19/2026</Publish_Date>
    <Record_Count>2</Record_Count>
  </publshInformation>
  <sdnEntry>
    <uid>1001</uid>
    <firstName>Ivan</firstName>
    <lastName>Petrov</lastName>
    <sdnType>Individual</sdnType>
    <programList><program>RUSSIA-EO14024</program></programList>
    <addressList><address><country>Russia</country></address></addressList>
  </sdnEntry>
  <sdnEntry>
    <uid>1003</uid>
    <firstName>Boris</firstName>
    <lastName>Sokolov</lastName>
    <sdnType>Individual</sdnType>
    <programList>
      <program>RUSSIA-EO14024</program>
      <program>CYBER2</program>
    </programList>
    <addressList><address><country>Russia</country></address></addressList>
  </sdnEntry>
</sdnList>
"""


if __name__ == "__main__":
    import shutil
    logging.basicConfig(level=logging.INFO)

    # Use a sandboxed snapshot dir for the smoke test
    test_dir = os.path.join(tempfile.gettempdir(), "regulators-sdn-smoke")
    shutil.rmtree(test_dir, ignore_errors=True)
    globals()["SNAPSHOT_DIR"] = test_dir

    day1 = parse_xml(_FIXTURE_XML_DAY1)
    print(f"day1 publish={day1['publish_date']}  entries={len(day1['entries'])}")
    assert day1["publish_date"] == "2026-05-18"
    assert len(day1["entries"]) == 2
    assert day1["entries"][0]["name"] == "Ivan Petrov"
    assert day1["entries"][1]["name"] == "Acme Holdings LLC"

    persist(day1)

    day2 = parse_xml(_FIXTURE_XML_DAY2)
    print(f"day2 publish={day2['publish_date']}  entries={len(day2['entries'])}")

    yesterday = prior_snapshot(day2["publish_date"])
    assert yesterday is not None
    assert yesterday["publish_date"] == "2026-05-18"

    delta = compute_delta(day2, yesterday)
    print(f"delta first_snapshot={delta['first_snapshot']}  "
          f"+{delta['added_count']} -{delta['removed_count']}")
    print(f"  program_deltas={delta['program_deltas']}")
    assert delta["first_snapshot"] is False
    assert delta["added_count"] == 1 and delta["added"][0]["name"] == "Boris Sokolov"
    assert delta["removed_count"] == 1 and delta["removed"][0]["name"] == "Acme Holdings LLC"
    assert delta["yesterday_publish_date"] == "2026-05-18"

    # First-snapshot path
    fresh_delta = compute_delta(day1, None)
    assert fresh_delta["first_snapshot"] is True
    assert fresh_delta["added"] == [] and fresh_delta["removed"] == []

    # Cleanup smoke dir
    shutil.rmtree(test_dir, ignore_errors=True)
    print("smoke OK")
