"""Analyst Mode — entity/event/source persistence layer.

SQLite-backed ontology for the Gotham-style analyst features. Tables:
    entities        — Actors, places, assets, orgs (typed, with optional geo).
    sources         — RSS items / X posts that backed an event extraction.
    events          — Typed occurrences (Strike, Statement, …) with time + geo.
    event_actors    — Many-to-many: which entities played which role.
    event_sources   — Many-to-many: which sources backed an event.
    pinboards       — Saved analyst views (filters + bbox + time range).

Every event has at least one source row — that is the provenance contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).parent / "analyst.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    aliases     TEXT,
    lat         REAL,
    lon         REAL,
    metadata    TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT,
    publisher     TEXT,
    title         TEXT,
    snippet       TEXT,
    published_at  REAL,
    fetched_at    REAL NOT NULL,
    UNIQUE(publisher, title)
);
CREATE INDEX IF NOT EXISTS idx_sources_published ON sources(published_at);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    occurred_at  REAL NOT NULL,
    lat          REAL,
    lon          REAL,
    confidence   REAL DEFAULT 0.5,
    severity     INTEGER DEFAULT 1,
    dedupe_key   TEXT UNIQUE,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_geo ON events(lat, lon);

CREATE TABLE IF NOT EXISTS event_actors (
    event_id   INTEGER NOT NULL,
    entity_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    PRIMARY KEY (event_id, entity_id, role),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_actors_entity ON event_actors(entity_id);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id   INTEGER NOT NULL,
    source_id  INTEGER NOT NULL,
    PRIMARY KEY (event_id, source_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pinboards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    filters     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    market_id    TEXT NOT NULL,
    ts           REAL NOT NULL,
    top_price    REAL NOT NULL,
    top_outcome  TEXT,
    volume_24h   REAL,
    PRIMARY KEY (market_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_msnap_ts ON market_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_msnap_market ON market_snapshots(market_id, ts);
"""

_lock = threading.RLock()
_initialized = False


@contextmanager
def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # filesystems without fcntl-style locking can't WAL — fall back to default journal
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
        with _conn() as c:
            c.executescript(_SCHEMA)
        _seed_baseline_entities()
        _initialized = True


# ── Entities ────────────────────────────────────────────────────────────────

def upsert_entity(entity_id: str, kind: str, name: str,
                  aliases: list[str] | None = None,
                  lat: float | None = None, lon: float | None = None,
                  metadata: dict | None = None) -> None:
    now = time.time()
    aliases_json = json.dumps(aliases or [])
    meta_json = json.dumps(metadata or {})
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO entities (id, kind, name, aliases, lat, lon, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,
                name=excluded.name,
                aliases=excluded.aliases,
                lat=COALESCE(excluded.lat, entities.lat),
                lon=COALESCE(excluded.lon, entities.lon),
                metadata=excluded.metadata,
                updated_at=excluded.updated_at
            """,
            (entity_id, kind, name, aliases_json, lat, lon, meta_json, now, now),
        )


def get_entity(entity_id: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            return None
        return _entity_row_to_dict(row)


def search_entities(query: str, limit: int = 20) -> list[dict]:
    q = f"%{query.lower()}%"
    with _lock, _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM entities
            WHERE LOWER(name) LIKE ? OR LOWER(aliases) LIKE ? OR LOWER(id) LIKE ?
            ORDER BY name
            LIMIT ?
            """,
            (q, q, q, limit),
        ).fetchall()
        return [_entity_row_to_dict(r) for r in rows]


def _entity_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "aliases": json.loads(row["aliases"] or "[]"),
        "lat": row["lat"],
        "lon": row["lon"],
        "metadata": json.loads(row["metadata"] or "{}"),
    }


# ── Sources ─────────────────────────────────────────────────────────────────

def upsert_source(publisher: str, title: str, url: str,
                  snippet: str = "", published_at: float | None = None) -> int:
    now = time.time()
    with _lock, _conn() as c:
        cur = c.execute(
            """
            INSERT INTO sources (url, publisher, title, snippet, published_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(publisher, title) DO UPDATE SET
                url=COALESCE(excluded.url, sources.url),
                snippet=COALESCE(excluded.snippet, sources.snippet),
                fetched_at=excluded.fetched_at
            RETURNING id
            """,
            (url, publisher, title, snippet, published_at, now),
        )
        row = cur.fetchone()
        return row["id"]


# ── Events ──────────────────────────────────────────────────────────────────

def _make_dedupe_key(event_type: str, summary: str, occurred_at: float,
                     lat: float | None, lon: float | None,
                     actor_ids: list[str]) -> str:
    bucket = int(occurred_at // 3600) if occurred_at else 0
    geo = ""
    if lat is not None and lon is not None:
        geo = f"{round(lat, 1)}:{round(lon, 1)}"
    actors = ",".join(sorted(actor_ids))
    raw = f"{event_type}|{bucket}|{geo}|{actors}|{summary[:80].lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def insert_event(event_type: str, summary: str, occurred_at: float,
                 lat: float | None, lon: float | None,
                 confidence: float, severity: int,
                 actors: list[tuple[str, str]],
                 source_ids: list[int]) -> int | None:
    """Insert an event. Returns event id, or None if deduped.

    actors: list of (entity_id, role).
    Source rows must already exist (call upsert_source first).
    Dedupe by (type, hour-bucket, ~0.1deg geo, actors, summary head).
    """
    actor_ids = [a[0] for a in actors]
    key = _make_dedupe_key(event_type, summary, occurred_at, lat, lon, actor_ids)
    now = time.time()
    with _lock, _conn() as c:
        try:
            cur = c.execute(
                """
                INSERT INTO events (type, summary, occurred_at, lat, lon,
                                    confidence, severity, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_type, summary, occurred_at, lat, lon,
                 confidence, severity, key, now),
            )
            event_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # dedupe collision
        for entity_id, role in actors:
            c.execute(
                "INSERT OR IGNORE INTO event_actors (event_id, entity_id, role) VALUES (?,?,?)",
                (event_id, entity_id, role),
            )
        for sid in source_ids:
            c.execute(
                "INSERT OR IGNORE INTO event_sources (event_id, source_id) VALUES (?,?)",
                (event_id, sid),
            )
        return event_id


def query_events(since: float | None = None, until: float | None = None,
                 types: Iterable[str] | None = None,
                 actor_id: str | None = None,
                 bbox: tuple[float, float, float, float] | None = None,
                 limit: int = 200) -> list[dict]:
    """bbox = (min_lon, min_lat, max_lon, max_lat). None matches any geo (incl. NULL)."""
    where = []
    params: list[Any] = []
    if since is not None:
        where.append("e.occurred_at >= ?")
        params.append(since)
    if until is not None:
        where.append("e.occurred_at <= ?")
        params.append(until)
    if types:
        ph = ",".join("?" for _ in types)
        where.append(f"e.type IN ({ph})")
        params.extend(list(types))
    if bbox:
        where.append("e.lon BETWEEN ? AND ? AND e.lat BETWEEN ? AND ?")
        params.extend([bbox[0], bbox[2], bbox[1], bbox[3]])
    join = ""
    if actor_id:
        join = "JOIN event_actors a ON a.event_id = e.id"
        where.append("a.entity_id = ?")
        params.append(actor_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT e.* FROM events e {join}
        {where_sql}
        ORDER BY e.occurred_at DESC
        LIMIT ?
    """
    params.append(limit)
    with _lock, _conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [_hydrate_event(c, r) for r in rows]


def get_event(event_id: int) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        return _hydrate_event(c, row)


def _hydrate_event(c: sqlite3.Connection, row: sqlite3.Row) -> dict:
    actors = c.execute(
        """
        SELECT a.entity_id, a.role, e.name, e.kind
        FROM event_actors a JOIN entities e ON e.id = a.entity_id
        WHERE a.event_id = ?
        """,
        (row["id"],),
    ).fetchall()
    sources = c.execute(
        """
        SELECT s.id, s.publisher, s.title, s.url, s.published_at
        FROM event_sources es JOIN sources s ON s.id = es.source_id
        WHERE es.event_id = ?
        ORDER BY s.published_at DESC
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "type": row["type"],
        "summary": row["summary"],
        "occurred_at": row["occurred_at"],
        "lat": row["lat"],
        "lon": row["lon"],
        "confidence": row["confidence"],
        "severity": row["severity"],
        "actors": [
            {"id": a["entity_id"], "role": a["role"], "name": a["name"], "kind": a["kind"]}
            for a in actors
        ],
        "sources": [
            {"id": s["id"], "publisher": s["publisher"], "title": s["title"],
             "url": s["url"], "published_at": s["published_at"]}
            for s in sources
        ],
    }


def timeline_buckets(since: float, until: float, bucket_seconds: int = 3600,
                     bbox: tuple[float, float, float, float] | None = None) -> dict:
    """Bucketed event counts by type. Returns {buckets: [ts...], series: {type: [counts]}}."""
    n = max(1, int((until - since) / bucket_seconds))
    bucket_ts = [since + i * bucket_seconds for i in range(n)]
    where = ["occurred_at >= ?", "occurred_at < ?"]
    params: list[Any] = [since, until]
    if bbox:
        where.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?")
        params.extend([bbox[0], bbox[2], bbox[1], bbox[3]])
    sql = f"""
        SELECT type,
               CAST((occurred_at - ?) / ? AS INTEGER) AS bucket,
               COUNT(*) AS n
        FROM events
        WHERE {' AND '.join(where)}
        GROUP BY type, bucket
    """
    with _lock, _conn() as c:
        rows = c.execute(sql, [since, bucket_seconds, *params]).fetchall()
    series: dict[str, list[int]] = {}
    for r in rows:
        s = series.setdefault(r["type"], [0] * n)
        idx = r["bucket"]
        if 0 <= idx < n:
            s[idx] = r["n"]
    return {"buckets": bucket_ts, "bucket_seconds": bucket_seconds, "series": series}


def entity_graph(entity_id: str, depth: int = 1, limit_per_hop: int = 25) -> dict:
    """Return co-occurrence subgraph: entities that appear in the same events."""
    visited: set[str] = {entity_id}
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    frontier = {entity_id}
    with _lock, _conn() as c:
        for _ in range(max(0, depth)):
            next_frontier: set[str] = set()
            for eid in frontier:
                ent = c.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
                if ent and eid not in nodes:
                    nodes[eid] = _entity_row_to_dict(ent)
                rows = c.execute(
                    """
                    SELECT a2.entity_id AS other, COUNT(DISTINCT a1.event_id) AS weight
                    FROM event_actors a1
                    JOIN event_actors a2 ON a1.event_id = a2.event_id
                    WHERE a1.entity_id = ? AND a2.entity_id != ?
                    GROUP BY a2.entity_id
                    ORDER BY weight DESC
                    LIMIT ?
                    """,
                    (eid, eid, limit_per_hop),
                ).fetchall()
                for r in rows:
                    other = r["other"]
                    edges.append({"source": eid, "target": other, "weight": r["weight"]})
                    if other not in visited:
                        visited.add(other)
                        next_frontier.add(other)
                        ent2 = c.execute("SELECT * FROM entities WHERE id=?", (other,)).fetchone()
                        if ent2:
                            nodes[other] = _entity_row_to_dict(ent2)
            frontier = next_frontier
            if not frontier:
                break
    return {"nodes": list(nodes.values()), "edges": edges}


# ── Pinboards ───────────────────────────────────────────────────────────────

def create_pinboard(name: str, filters: dict) -> int:
    now = time.time()
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO pinboards (name, filters, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(filters), now),
        )
        return cur.lastrowid


def list_pinboards() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, name, filters, created_at FROM pinboards ORDER BY created_at DESC"
        ).fetchall()
        return [
            {"id": r["id"], "name": r["name"],
             "filters": json.loads(r["filters"]),
             "created_at": r["created_at"]}
            for r in rows
        ]


def delete_pinboard(pin_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM pinboards WHERE id=?", (pin_id,))
        return cur.rowcount > 0


# ── Market snapshots (Polymarket price history) ─────────────────────────────
# Recorded by the server's polymarket fetch hook (debounced to ~5 minutes per
# market). Used to surface "this market moved Npt while the event was
# breaking" badges on event cards.

_SNAPSHOT_MIN_INTERVAL = 5 * 60  # seconds between snapshots per market
_SNAPSHOT_RETENTION = 14 * 24 * 3600  # 14 days


def record_market_snapshot(market_id: str, top_price: float,
                           top_outcome: str | None, volume_24h: float | None,
                           now: float | None = None) -> bool:
    """Insert a snapshot if the last one for this market is older than the
    debounce interval. Returns True if a row was written."""
    if not market_id:
        return False
    now = now if now is not None else time.time()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT ts FROM market_snapshots WHERE market_id=? ORDER BY ts DESC LIMIT 1",
            (market_id,),
        ).fetchone()
        if row and (now - row["ts"]) < _SNAPSHOT_MIN_INTERVAL:
            return False
        c.execute(
            "INSERT OR IGNORE INTO market_snapshots (market_id, ts, top_price, top_outcome, volume_24h) "
            "VALUES (?, ?, ?, ?, ?)",
            (market_id, now, float(top_price), top_outcome, volume_24h),
        )
        return True


def bulk_insert_market_snapshots(market_id: str, top_outcome: str | None,
                                  points: Iterable[tuple[float, float]]) -> int:
    """Bulk-insert historical snapshots (skips duplicates by PK).

    `points` is an iterable of (timestamp, price). Volume is unknown for
    backfilled data so it's stored as NULL.
    """
    if not market_id:
        return 0
    rows = [(market_id, float(t), float(p), top_outcome, None) for t, p in points if t and p is not None]
    if not rows:
        return 0
    with _lock, _conn() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO market_snapshots "
            "(market_id, ts, top_price, top_outcome, volume_24h) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        return cur.rowcount


def market_snapshot_count(market_id: str) -> int:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM market_snapshots WHERE market_id=?",
            (market_id,),
        ).fetchone()
        return int(row["n"] if row else 0)


def prune_market_snapshots(now: float | None = None) -> int:
    now = now if now is not None else time.time()
    cutoff = now - _SNAPSHOT_RETENTION
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM market_snapshots WHERE ts < ?", (cutoff,))
        return cur.rowcount


def _price_at(c: sqlite3.Connection, market_id: str, target_ts: float,
              max_gap: float) -> float | None:
    """Return the snapshot price closest to `target_ts`, within `max_gap` seconds.
    Prefers a snapshot at or before the target; falls back to the nearest after."""
    before = c.execute(
        "SELECT ts, top_price FROM market_snapshots "
        "WHERE market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (market_id, target_ts),
    ).fetchone()
    after = c.execute(
        "SELECT ts, top_price FROM market_snapshots "
        "WHERE market_id=? AND ts>? ORDER BY ts ASC LIMIT 1",
        (market_id, target_ts),
    ).fetchone()
    candidates: list[tuple[float, float]] = []
    if before and (target_ts - before["ts"]) <= max_gap:
        candidates.append((target_ts - before["ts"], float(before["top_price"])))
    if after and (after["ts"] - target_ts) <= max_gap:
        candidates.append((after["ts"] - target_ts, float(after["top_price"])))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def market_movement(market_id: str, around_ts: float,
                    half_window: float = 6 * 3600,
                    max_gap: float = 3 * 3600) -> dict | None:
    """Δprice (in points) for `market_id` measured T−h vs T+h around `around_ts`.

    Returns {"delta_pts": float, "before": float, "after": float, "window_s": h}
    or None if there's no usable history bracketing the timestamp.
    """
    with _lock, _conn() as c:
        before = _price_at(c, market_id, around_ts - half_window, max_gap)
        after = _price_at(c, market_id, around_ts + half_window, max_gap)
        if before is None or after is None:
            # Fall back to "movement since closest pre-event snapshot vs now"
            # only when after-side is missing AND the event is recent enough
            # that there *is* a "now" snapshot.
            return None
        return {
            "delta_pts": round((after - before) * 100, 1),
            "before": round(before, 4),
            "after": round(after, 4),
            "window_s": half_window,
        }


def market_movement_24h(market_id: str, now: float | None = None,
                        max_gap: float = 3 * 3600) -> float | None:
    """Δprice in points over the last 24h. Returns None if no usable history."""
    now = now if now is not None else time.time()
    with _lock, _conn() as c:
        before = _price_at(c, market_id, now - 24 * 3600, max_gap)
        latest = c.execute(
            "SELECT top_price FROM market_snapshots "
            "WHERE market_id=? ORDER BY ts DESC LIMIT 1",
            (market_id,),
        ).fetchone()
        if before is None or not latest:
            return None
        return round((float(latest["top_price"]) - before) * 100, 1)


# ── Stats ───────────────────────────────────────────────────────────────────

def stats() -> dict:
    with _lock, _conn() as c:
        return {
            "entities": c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"],
            "events":   c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
            "sources":  c.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"],
            "pinboards": c.execute("SELECT COUNT(*) AS n FROM pinboards").fetchone()["n"],
        }


# ── Baseline gazetteer ──────────────────────────────────────────────────────
# A small starter set so the heuristic extractor has something to match the
# moment the dashboard boots. Coordinates are capital-city centroids — good
# enough for an MVP map pin. Aliases drive the matcher in event_extractor.

_BASELINE_ENTITIES: list[dict] = [
    # ── States ─────────────────────────────────────────────────────────────
    {"id": "country:us",  "kind": "state", "name": "United States",
     "aliases": ["us", "u.s.", "usa", "america", "washington", "white house", "pentagon"],
     "lat": 38.9072, "lon": -77.0369},
    {"id": "country:ru",  "kind": "state", "name": "Russia",
     "aliases": ["russia", "russian", "moscow", "kremlin", "putin"],
     "lat": 55.7558, "lon": 37.6173},
    {"id": "country:cn",  "kind": "state", "name": "China",
     "aliases": ["china", "chinese", "beijing", "xi jinping", "pla"],
     "lat": 39.9042, "lon": 116.4074},
    {"id": "country:ua",  "kind": "state", "name": "Ukraine",
     "aliases": ["ukraine", "ukrainian", "kyiv", "kiev", "zelensky", "zelenskyy"],
     "lat": 50.4501, "lon": 30.5234},
    {"id": "country:il",  "kind": "state", "name": "Israel",
     "aliases": ["israel", "israeli", "tel aviv", "jerusalem", "idf", "netanyahu"],
     "lat": 31.7683, "lon": 35.2137},
    {"id": "country:ir",  "kind": "state", "name": "Iran",
     "aliases": ["iran", "iranian", "tehran", "irgc", "khamenei"],
     "lat": 35.6892, "lon": 51.3890},
    {"id": "country:ps",  "kind": "place", "name": "Gaza",
     "aliases": ["gaza", "gaza strip", "gazan", "palestinian", "hamas"],
     "lat": 31.5, "lon": 34.47},
    {"id": "country:lb",  "kind": "state", "name": "Lebanon",
     "aliases": ["lebanon", "lebanese", "beirut", "hezbollah"],
     "lat": 33.8938, "lon": 35.5018},
    {"id": "country:sy",  "kind": "state", "name": "Syria",
     "aliases": ["syria", "syrian", "damascus"],
     "lat": 33.5138, "lon": 36.2765},
    {"id": "country:ye",  "kind": "state", "name": "Yemen",
     "aliases": ["yemen", "yemeni", "sanaa", "houthi", "houthis"],
     "lat": 15.3694, "lon": 44.1910},
    {"id": "country:tw",  "kind": "state", "name": "Taiwan",
     "aliases": ["taiwan", "taiwanese", "taipei"],
     "lat": 25.0330, "lon": 121.5654},
    {"id": "country:kp",  "kind": "state", "name": "North Korea",
     "aliases": ["north korea", "dprk", "pyongyang", "kim jong"],
     "lat": 39.0392, "lon": 125.7625},
    {"id": "country:kr",  "kind": "state", "name": "South Korea",
     "aliases": ["south korea", "rok", "seoul"],
     "lat": 37.5665, "lon": 126.9780},
    {"id": "country:jp",  "kind": "state", "name": "Japan",
     "aliases": ["japan", "japanese", "tokyo"],
     "lat": 35.6762, "lon": 139.6503},
    {"id": "country:in",  "kind": "state", "name": "India",
     "aliases": ["india", "indian", "new delhi", "modi"],
     "lat": 28.6139, "lon": 77.2090},
    {"id": "country:pk",  "kind": "state", "name": "Pakistan",
     "aliases": ["pakistan", "pakistani", "islamabad"],
     "lat": 33.6844, "lon": 73.0479},
    {"id": "country:gb",  "kind": "state", "name": "United Kingdom",
     "aliases": ["uk", "u.k.", "britain", "british", "london", "downing street"],
     "lat": 51.5074, "lon": -0.1278},
    {"id": "country:fr",  "kind": "state", "name": "France",
     "aliases": ["france", "french", "paris", "macron", "elysee"],
     "lat": 48.8566, "lon": 2.3522},
    {"id": "country:de",  "kind": "state", "name": "Germany",
     "aliases": ["germany", "german", "berlin", "merz", "scholz"],
     "lat": 52.5200, "lon": 13.4050},
    {"id": "country:tr",  "kind": "state", "name": "Turkey",
     "aliases": ["turkey", "turkish", "ankara", "erdogan"],
     "lat": 39.9334, "lon": 32.8597},
    {"id": "country:sa",  "kind": "state", "name": "Saudi Arabia",
     "aliases": ["saudi arabia", "saudi", "riyadh", "mbs"],
     "lat": 24.7136, "lon": 46.6753},
    {"id": "country:ve",  "kind": "state", "name": "Venezuela",
     "aliases": ["venezuela", "venezuelan", "caracas", "maduro"],
     "lat": 10.4806, "lon": -66.9036},
    {"id": "country:mx",  "kind": "state", "name": "Mexico",
     "aliases": ["mexico", "mexican", "mexico city"],
     "lat": 19.4326, "lon": -99.1332},
    {"id": "country:br",  "kind": "state", "name": "Brazil",
     "aliases": ["brazil", "brazilian", "brasilia", "lula"],
     "lat": -15.8267, "lon": -47.9218},
    {"id": "country:sd",  "kind": "state", "name": "Sudan",
     "aliases": ["sudan", "sudanese", "khartoum"],
     "lat": 15.5007, "lon": 32.5599},
    # ── Orgs ───────────────────────────────────────────────────────────────
    {"id": "org:nato",    "kind": "org", "name": "NATO",
     "aliases": ["nato", "north atlantic"], "lat": 50.8798, "lon": 4.4194},
    {"id": "org:un",      "kind": "org", "name": "United Nations",
     "aliases": ["un ", "united nations", "security council", "unsc"],
     "lat": 40.7489, "lon": -73.9680},
    {"id": "org:eu",      "kind": "org", "name": "European Union",
     "aliases": ["eu ", "european union", "brussels", "european commission"],
     "lat": 50.8503, "lon": 4.3517},
    {"id": "org:opec",    "kind": "org", "name": "OPEC",
     "aliases": ["opec", "opec+"], "lat": 48.2082, "lon": 16.3738},
    {"id": "org:fed",     "kind": "org", "name": "Federal Reserve",
     "aliases": ["fed ", "federal reserve", "fomc", "powell"],
     "lat": 38.8921, "lon": -77.0455},
    {"id": "org:ecb",     "kind": "org", "name": "European Central Bank",
     "aliases": ["ecb", "european central bank", "lagarde"],
     "lat": 50.1109, "lon": 8.6821},
    {"id": "org:hamas",   "kind": "org", "name": "Hamas",
     "aliases": ["hamas"], "lat": 31.5, "lon": 34.47},
    {"id": "org:hezbollah","kind": "org", "name": "Hezbollah",
     "aliases": ["hezbollah"], "lat": 33.8938, "lon": 35.5018},
    {"id": "org:houthis", "kind": "org", "name": "Houthis",
     "aliases": ["houthi", "houthis", "ansar allah"],
     "lat": 15.3694, "lon": 44.1910},
]


def _seed_baseline_entities() -> None:
    with _lock, _conn() as c:
        existing = c.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
    if existing >= len(_BASELINE_ENTITIES):
        return
    for e in _BASELINE_ENTITIES:
        upsert_entity(
            entity_id=e["id"], kind=e["kind"], name=e["name"],
            aliases=e["aliases"], lat=e.get("lat"), lon=e.get("lon"),
        )


def baseline_aliases() -> list[tuple[str, str]]:
    """Flat list of (alias_lower, entity_id) for the heuristic matcher."""
    out: list[tuple[str, str]] = []
    for e in _BASELINE_ENTITIES:
        out.append((e["name"].lower(), e["id"]))
        for a in e["aliases"]:
            out.append((a.lower(), e["id"]))
    return out
