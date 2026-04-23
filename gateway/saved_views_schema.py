"""Filter schema + validator + SQL builder for saved-views.

Four scopes — ``markets``, ``feed``, ``sources``, ``predictions`` — share
one declarative dimension model. Each dimension is a ``FilterField`` with
a type, optional bounds, and the SQL column it maps to.

``validate_filters(scope, raw)`` is the single sanitation gate: it coerces
query strings / JSON into typed Python values, drops any unknown fields,
and replaces malformed values with their defaults (the spec explicitly
requires graceful fallback, never 500).

``build_where(scope, filters)`` returns ``(where_sql, params, joins)`` the
caller splices into the list query. The joins tuple is empty for the
simple columns; complex dimensions (source count, min-credibility-of-any-
source-on-market) emit a ``LEFT JOIN predictions …`` plus a HAVING clause.

``filters_from_query(scope, query_params)`` parses a ``Request.query_params``
MultiDict into the canonical filter dict, which is what the client also
writes into ``saved_views.filter_json``. This means the URL ↔ saved view
round-trip is identity — copy the URL, save the view, reload → same state.
"""

from __future__ import annotations

import json
import time
from typing import Any


# ── Scope / dimension catalogue ──────────────────────────────────────────────
#
# The dimension entries below drive validation, SQL generation, and the
# front-end panel's control widgets — the single source of truth.

SCOPES = frozenset({"markets", "feed", "sources", "predictions"})

# Known category + platform domains so strings can be whitelisted cheaply.
KNOWN_CATEGORIES = frozenset({
    "politics", "geopolitics", "economics", "crypto", "sports",
    "tech", "climate", "elections", "ai", "health", "science",
    "entertainment", "other",
})
KNOWN_PLATFORMS = frozenset({"polymarket", "kalshi", "both"})
KNOWN_RESOLUTION_STATES = frozenset({"pending", "resolved", "any"})


class FilterField:
    """Declarative dimension. Has a kind + validator + SQL fragment.

    ``kind`` is one of:
      - ``str``       — single whitelisted string
      - ``str_list``  — comma-separated whitelisted strings
      - ``float``     — numeric, optional ``min`` / ``max`` clamp
      - ``int``       — integer, optional ``min`` / ``max`` clamp
      - ``range``     — (lo, hi) tuple, both clamped
      - ``bool``      — truthy strings ("1"/"true"/"yes") → True
      - ``duration``  — "7d" / "48h" → seconds; caller translates to cutoff
      - ``handle_list`` — comma-separated @-less handles (loose)

    ``sql`` is a format-string template; ``{col}`` is replaced by ``column``
    and parameter placeholders are positional ``?``. Fields with
    ``sql=None`` are metadata-only (e.g. ``view_id``) and skipped by the
    builder.
    """

    __slots__ = ("name", "kind", "column", "sql", "options")

    def __init__(
        self,
        name: str,
        kind: str,
        column: str = "",
        sql: str | None = "",
        options: dict | None = None,
    ):
        self.name = name
        self.kind = kind
        self.column = column
        self.sql = sql
        self.options = options or {}


# ── Per-scope dimension tables ───────────────────────────────────────────────

_MARKETS_FIELDS: list[FilterField] = [
    FilterField("categories", "str_list", "m.category",
                "m.category IN ({placeholders})",
                {"allowed": KNOWN_CATEGORIES}),
    FilterField("platform", "str", "m.platform",
                "m.platform = ?",
                {"allowed": KNOWN_PLATFORMS}),
    FilterField("close_within", "duration", "m.close_time",
                # now() + N seconds.  SQLite stores close_time as unix-epoch int.
                "m.close_time IS NOT NULL AND m.close_time <= ?"),
    FilterField("min_volume", "float", "m.volume_usd",
                "m.volume_usd >= ?",
                {"min": 0.0, "max": 1e12}),
    FilterField("max_volume", "float", "m.volume_usd",
                "m.volume_usd <= ?",
                {"min": 0.0, "max": 1e12}),
    FilterField("market_prob_range", "range", "m.current_probability",
                "m.current_probability BETWEEN ? AND ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("narve_prob_range", "range", "m.narve_probability",
                "m.narve_probability BETWEEN ? AND ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("min_edge", "float", "m.edge_pp",
                # Edge stored as absolute probability delta (0.10 = 10pp).
                "ABS(m.edge_pp) >= ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("min_source_count", "int", "",
                # Emitted as HAVING — handled by the builder specially.
                "<having:source_count>",
                {"min": 0, "max": 100}),
    FilterField("min_source_cred", "float", "",
                "<join:source_cred>",
                {"min": 0.0, "max": 1.0}),
    FilterField("has_insider_signal", "bool", "m.has_insider_signal",
                "m.has_insider_signal = 1"),
    FilterField("has_environmental", "bool", "m.has_environmental",
                "m.has_environmental = 1"),
    FilterField("tags", "str_list", "m.tags",
                # tags stored as JSON array; each selected tag must appear.
                "<json:tags>"),
]

_FEED_FIELDS: list[FilterField] = [
    # Inherit all market dimensions + feed-specific filters.
    *_MARKETS_FIELDS,
    FilterField("sources", "handle_list", "p.source_handle",
                "p.source_handle IN ({placeholders})"),
    FilterField("source_cred_range", "range", "sc.global_credibility",
                "sc.global_credibility BETWEEN ? AND ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("posted_within", "duration", "p.extracted_at",
                # extracted_at is unix-epoch. Client sends duration; builder
                # converts to cutoff = now() - N.
                "p.extracted_at >= ?"),
    FilterField("resolution", "str", "p.resolved",
                "<resolution>",
                {"allowed": KNOWN_RESOLUTION_STATES}),
]

_SOURCES_FIELDS: list[FilterField] = [
    FilterField("min_credibility", "float", "sc.global_credibility",
                "sc.global_credibility >= ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("max_credibility", "float", "sc.global_credibility",
                "sc.global_credibility <= ?",
                {"min": 0.0, "max": 1.0}),
    FilterField("min_predictions", "int", "sc.total_predictions",
                "sc.total_predictions >= ?",
                {"min": 0, "max": 1_000_000}),
    FilterField("categories_active", "str_list", "scc.category",
                "<join:categories_active>",
                {"allowed": KNOWN_CATEGORIES}),
    FilterField("handles", "handle_list", "sc.source_handle",
                "sc.source_handle IN ({placeholders})"),
]

_PREDICTIONS_FIELDS: list[FilterField] = [
    # Standalone predictions list (as opposed to feed which joins markets).
    FilterField("categories", "str_list", "p.category",
                "p.category IN ({placeholders})",
                {"allowed": KNOWN_CATEGORIES}),
    FilterField("sources", "handle_list", "p.source_handle",
                "p.source_handle IN ({placeholders})"),
    FilterField("resolution", "str", "p.resolved",
                "<resolution>",
                {"allowed": KNOWN_RESOLUTION_STATES}),
    FilterField("posted_within", "duration", "p.extracted_at",
                "p.extracted_at >= ?"),
    FilterField("source_cred_range", "range", "sc.global_credibility",
                "sc.global_credibility BETWEEN ? AND ?",
                {"min": 0.0, "max": 1.0}),
]


SCHEMAS: dict[str, list[FilterField]] = {
    "markets":     _MARKETS_FIELDS,
    "feed":        _FEED_FIELDS,
    "sources":     _SOURCES_FIELDS,
    "predictions": _PREDICTIONS_FIELDS,
}


# ── Value coercion ───────────────────────────────────────────────────────────


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _coerce(field: FilterField, raw: Any) -> Any | None:
    """Return the sanitised value, or ``None`` when the input is malformed.

    Callers (validate_filters) drop ``None`` results silently — the spec
    requires graceful fallback, not a 500 on a typo in a URL.
    """
    if raw is None:
        return None
    opts = field.options
    kind = field.kind

    try:
        if kind == "str":
            value = str(raw).strip().lower()
            allowed = opts.get("allowed")
            if allowed and value not in allowed:
                return None
            return value or None

        if kind == "str_list":
            if isinstance(raw, list):
                items = [str(x).strip().lower() for x in raw]
            else:
                items = [s.strip().lower() for s in str(raw).split(",") if s.strip()]
            allowed = opts.get("allowed")
            if allowed:
                items = [s for s in items if s in allowed]
            return items or None

        if kind == "handle_list":
            if isinstance(raw, list):
                items = [str(x).strip().lstrip("@").lower() for x in raw]
            else:
                items = [
                    s.strip().lstrip("@").lower()
                    for s in str(raw).split(",")
                    if s.strip()
                ]
            # Bound the list — a filter with 10k handles becomes a pathological
            # IN clause. 500 is generous for any realistic use.
            items = [s for s in items if s and len(s) < 64][:500]
            return items or None

        if kind == "int":
            n = int(float(raw))
            lo = opts.get("min")
            hi = opts.get("max")
            if lo is not None and n < lo: n = lo
            if hi is not None and n > hi: n = hi
            return n

        if kind == "float":
            f = float(raw)
            lo = opts.get("min")
            hi = opts.get("max")
            if lo is not None and f < lo: f = lo
            if hi is not None and f > hi: f = hi
            return f

        if kind == "range":
            # Accept [lo, hi] list/tuple OR "lo,hi" string.
            if isinstance(raw, (list, tuple)) and len(raw) == 2:
                lo, hi = float(raw[0]), float(raw[1])
            elif isinstance(raw, str) and "," in raw:
                parts = raw.split(",")
                lo, hi = float(parts[0]), float(parts[1])
            else:
                return None
            min_lo = opts.get("min")
            max_hi = opts.get("max")
            if min_lo is not None: lo = max(lo, min_lo)
            if max_hi is not None: hi = min(hi, max_hi)
            if lo > hi: lo, hi = hi, lo
            return [lo, hi]

        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in _TRUTHY

        if kind == "duration":
            # "7d" / "48h" / "30m" / "1800" (seconds). Return seconds (int).
            s = str(raw).strip().lower()
            if not s: return None
            if s[-1] == "d":   seconds = int(float(s[:-1]) * 86400)
            elif s[-1] == "h": seconds = int(float(s[:-1]) * 3600)
            elif s[-1] == "m": seconds = int(float(s[:-1]) * 60)
            elif s[-1] == "s": seconds = int(float(s[:-1]))
            else:              seconds = int(float(s))
            # Clamp 1s … 365d so an absurd value can't turn the predicate
            # useless.
            return max(1, min(seconds, 365 * 86400))

    except (ValueError, TypeError):
        return None

    return None


# ── Public API ───────────────────────────────────────────────────────────────


def validate_filters(scope: str, raw: dict | None) -> dict:
    """Return the canonical, sanitised filter dict for ``scope``.

    Unknown fields are dropped. Malformed values are dropped (never raised).
    An empty dict means "no filters" — every list endpoint falls back to
    its own defaults in that case.
    """
    if scope not in SCHEMAS:
        return {}
    raw = raw or {}
    out: dict[str, Any] = {}
    fields = {f.name: f for f in SCHEMAS[scope]}
    for name, field in fields.items():
        if name not in raw:
            continue
        value = _coerce(field, raw[name])
        if value is None:
            continue
        # For list-of-strings, drop empty lists — they're "no filter", not
        # "impossible filter".
        if isinstance(value, list) and not value:
            continue
        out[name] = value
    return out


def filters_from_query(scope: str, query_params) -> dict:
    """Read a FastAPI Request.query_params into validated filters.

    ``query_params`` is a Starlette QueryParams (list-like MultiDict). We
    take the last value for repeat keys so a crafted URL can't collide two
    values, and comma-split list-kind fields so ?categories=a,b works.
    """
    if scope not in SCHEMAS:
        return {}
    raw: dict[str, Any] = {}
    for f in SCHEMAS[scope]:
        if f.name in query_params:
            values = query_params.getlist(f.name) if hasattr(query_params, "getlist") else [query_params[f.name]]
            raw[f.name] = values[-1] if values else None
    return validate_filters(scope, raw)


def filters_to_query(filters: dict) -> dict:
    """Round-trip: turn validated filters back into URL-safe string values.

    Lists become comma-joined strings, ranges become "lo,hi", bools become
    "1"/"0". Used by "Copy link" and share-token resolution.
    """
    out: dict[str, str] = {}
    for k, v in filters.items():
        if isinstance(v, list):
            if len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
                out[k] = f"{v[0]},{v[1]}"  # range
            else:
                out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            out[k] = "1" if v else "0"
        else:
            out[k] = str(v)
    return out


def build_where(scope: str, filters: dict, *, now: int | None = None) -> tuple[str, list, list, list]:
    """Compile validated filters into SQL fragments.

    Returns ``(where_sql, params, extra_joins, having_clauses)`` where:

      - ``where_sql`` is a string you splice after ``WHERE 1=1``; each
        clause starts with ``AND``. Empty string means no filters applied.
      - ``params`` is the positional parameter list for ``where_sql`` +
        ``having_clauses`` (in that order).
      - ``extra_joins`` is a list of JOIN clauses the caller adds to the
        base query (e.g. ``LEFT JOIN source_category_credibility scc ...``).
      - ``having_clauses`` fires after a ``GROUP BY`` when min-source-count
        is set. Empty if not used.

    The caller is responsible for the base FROM / SELECT — the builder
    never assumes a particular column set.
    """
    if scope not in SCHEMAS or not filters:
        return ("", [], [], [])

    fields = {f.name: f for f in SCHEMAS[scope]}
    now = now if now is not None else int(time.time())

    where_parts: list[str] = []
    params: list = []
    joins: list[str] = []
    having: list[str] = []
    having_params: list = []
    need_predictions_join = False
    need_source_cred_join = False

    for name, value in filters.items():
        field = fields.get(name)
        if not field or field.sql in ("", None):
            continue
        sql = field.sql

        # Specialised fragments — handle before the generic path.
        if sql == "<having:source_count>":
            having.append("COUNT(DISTINCT p.source_handle) >= ?")
            having_params.append(value)
            need_predictions_join = True
            continue
        if sql == "<join:source_cred>":
            # min credibility of ANY source that predicted on this market.
            where_parts.append("AND EXISTS ("
                               "SELECT 1 FROM predictions pfx "
                               "LEFT JOIN source_credibility scfx "
                               "ON scfx.source_handle = pfx.source_handle "
                               "WHERE pfx.market_id = m.market_id "
                               "AND scfx.global_credibility >= ?)")
            params.append(value)
            continue
        if sql == "<json:tags>":
            for tag in value:
                where_parts.append("AND EXISTS ("
                                   "SELECT 1 FROM json_each(COALESCE(m.tags, '[]')) "
                                   "WHERE json_each.value = ?)")
                params.append(tag)
            continue
        if sql == "<resolution>":
            if value == "resolved":
                where_parts.append("AND p.resolved = 1")
            elif value == "pending":
                where_parts.append("AND (p.resolved = 0 OR p.resolved IS NULL)")
            # "any" → no clause
            continue
        if sql == "<join:categories_active>":
            joins.append(
                "LEFT JOIN source_category_credibility scc "
                "ON scc.source_handle = sc.source_handle"
            )
            placeholders = ",".join("?" * len(value))
            where_parts.append(f"AND scc.category IN ({placeholders})")
            params.extend(value)
            continue

        # Generic handlers.
        if field.kind == "str_list":
            placeholders = ",".join("?" * len(value))
            where_parts.append("AND " + sql.format(placeholders=placeholders))
            params.extend(value)
        elif field.kind == "handle_list":
            placeholders = ",".join("?" * len(value))
            where_parts.append("AND " + sql.format(placeholders=placeholders))
            params.extend(value)
        elif field.kind == "range":
            where_parts.append("AND " + sql)
            params.extend(value)
        elif field.kind == "bool":
            if value:
                where_parts.append("AND " + sql)
        elif field.kind == "duration":
            if name == "close_within":
                where_parts.append("AND " + sql)
                params.append(now + value)
            elif name == "posted_within":
                where_parts.append("AND " + sql)
                params.append(now - value)
            else:
                where_parts.append("AND " + sql)
                params.append(now - value)
        else:
            where_parts.append("AND " + sql)
            params.append(value)

    # De-dup joins (two different filters may ask for the same join).
    joins = list(dict.fromkeys(joins))

    return (" ".join(where_parts), params + having_params, joins, having)


# ── Cache key ────────────────────────────────────────────────────────────────


def cache_key(scope: str, filters: dict, *, user_id: int | None = None) -> str:
    """Stable cache key for a (user, scope, filter-set) tuple.

    JSON-serialising with sort_keys guarantees ``{a:1,b:2}`` and
    ``{b:2,a:1}`` hit the same key.
    """
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    uid = user_id if user_id is not None else "anon"
    return f"saved_views:{scope}:user={uid}:filters={payload}"
