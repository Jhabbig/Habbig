"""Queries for the collections feature (migrations 120-121).

API contract used by ``collections_routes``:

  collections CRUD
    ``create_collection(owner_id, title, ...)``
    ``list_user_collections(owner_id, *, include_system=True)``
    ``get_collection(id, *, viewer_user_id=None)``  — enforces visibility
    ``get_collection_by_slug(owner_handle, slug, *, viewer_user_id=None)``
    ``update_collection(id, *, owner_id, title=..., visibility=...)``
    ``delete_collection(id, *, owner_id)``

  items
    ``add_item(collection_id, *, owner_id, item_type, item_ref, note=None)``
    ``list_items(collection_id)``
    ``remove_item(collection_id, item_id, *, owner_id)``
    ``reorder_items(collection_id, *, owner_id, ordering)``

  follows
    ``follow_collection(user_id, collection_id)``
    ``unfollow_collection(user_id, collection_id)``
    ``list_followers(collection_id)``
    ``list_user_follows(user_id)``
    ``is_following(user_id, collection_id)``

  explore / admin
    ``featured_collections(limit=20)``
    ``most_followed_collections(limit=20)``
    ``recently_updated_collections(limit=20)``
    ``set_featured(collection_id, flag, *, admin_user_id)``

  auto-collections
    ``ensure_system_collections(user_id)`` — idempotent; creates the
        system "saved" + "watchlist" rows if missing and returns their
        ids keyed by slug.
    ``rebuild_system_collection_items(user_id, slug)`` — rewrites the
        items for the "saved" / "watchlist" system collection from the
        authoritative source tables (saved_predictions / followed_sources).

Every helper takes an ``owner_id`` kwarg where ownership is required.
Routes translate HTTPException; this layer just raises
``PermissionError`` / ``LookupError`` so the DB layer stays framework-free.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Iterable, Optional

import db


# ── Internals ─────────────────────────────────────────────────────────────


SYSTEM_SLUGS = {"saved", "watchlist"}
VALID_VISIBILITY = {"private", "shared", "public"}
VALID_ITEM_TYPES = {"market", "source", "prediction"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# AUDIT (MED): a viewer hitting the same collection twice inside the
# throttle window only bumps ``view_count`` once. Pre-fix every refresh
# / scroll-back / tab restore inflated the public most-followed sort
# and let any signed-in user farm "popularity" on a public board by
# scripting reloads. The bucket is per (viewer, collection); anonymous
# views aren't deduplicated here (they fall back to the raw bump path)
# because we don't have a stable per-anon identity to key on without
# fingerprinting.
_VIEW_BUMP_WINDOW_SECONDS = 600


# Process-local most-recent-view cache: maps (viewer_user_id, collection_id)
# to the unix timestamp of the last bump we counted. Sized small — the
# entries naturally age out after _VIEW_BUMP_WINDOW_SECONDS, but a
# long-running process accreting one entry per (viewer, collection) pair
# would eventually dominate memory. The simple cap below evicts the
# oldest entries when the dict exceeds the limit; deterministic eviction
# keeps the dedup honest under burst-y load without a separate sweeper.
_VIEW_BUMP_CACHE: dict[tuple[int, int], int] = {}
_VIEW_BUMP_CACHE_CAP = 50_000


def _should_bump_view(viewer_user_id: int, collection_id: int) -> bool:
    """True if this (viewer, collection) pair hasn't been counted in the
    last ``_VIEW_BUMP_WINDOW_SECONDS``.

    Records the timestamp on a True return so the next call in the
    window short-circuits. The owner-skip + visibility checks remain the
    caller's responsibility — this helper only handles dedup.
    """
    now = int(time.time())
    key = (int(viewer_user_id), int(collection_id))
    last = _VIEW_BUMP_CACHE.get(key)
    if last is not None and (now - last) < _VIEW_BUMP_WINDOW_SECONDS:
        return False
    if len(_VIEW_BUMP_CACHE) >= _VIEW_BUMP_CACHE_CAP:
        # Cheap LRU-ish eviction: drop the oldest 10% so we don't pay
        # the dict resize cost on every call near the cap.
        victims = sorted(_VIEW_BUMP_CACHE.items(), key=lambda kv: kv[1])
        for victim_key, _ in victims[: _VIEW_BUMP_CACHE_CAP // 10]:
            _VIEW_BUMP_CACHE.pop(victim_key, None)
    _VIEW_BUMP_CACHE[key] = now
    return True


def _slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    return slug[:64] if slug else f"untitled-{int(time.time())}"


def _unique_slug(c, owner_id: int, base: str) -> str:
    """Append a short numeric suffix if the base slug is already taken
    by the same owner. We only scan the first 200 matches because past
    that the user is either abusing the API or has 200 identically-named
    boards — either way, a random tail is fine."""
    rows = c.execute(
        "SELECT slug FROM collections WHERE owner_user_id = ? AND slug LIKE ?",
        (owner_id, base + "%"),
    ).fetchall()
    taken = {r["slug"] for r in rows}
    if base not in taken:
        return base
    for i in range(2, 200):
        candidate = f"{base}-{i}"
        if candidate not in taken:
            return candidate
    return f"{base}-{int(time.time())}"


def _row_to_dict(r: sqlite3.Row, *, viewer_user_id: Optional[int] = None) -> dict:
    d = dict(r)
    d["is_system"] = bool(d.get("is_system"))
    d["is_featured"] = bool(d.get("is_featured"))
    d["is_owner"] = bool(viewer_user_id) and viewer_user_id == d["owner_user_id"]
    return d


def _can_view(row: sqlite3.Row, viewer_user_id: Optional[int]) -> bool:
    """Enforce visibility: private → owner-only; shared → any signed-in
    user; public → everyone."""
    if viewer_user_id is not None and row["owner_user_id"] == viewer_user_id:
        return True
    vis = row["visibility"]
    if vis == "public":
        return True
    if vis == "shared" and viewer_user_id is not None:
        return True
    return False


# ── Collections CRUD ─────────────────────────────────────────────────────


def create_collection(
    owner_id: int,
    title: str,
    *,
    description: Optional[str] = None,
    visibility: str = "private",
    slug: Optional[str] = None,
    is_system: bool = False,
    cover_image_url: Optional[str] = None,
) -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")
    if visibility not in VALID_VISIBILITY:
        raise ValueError("invalid visibility")
    if is_system and slug not in SYSTEM_SLUGS:
        raise ValueError("system collection must use a reserved slug")

    now = int(time.time())
    with db.conn() as c:
        final_slug = _unique_slug(c, owner_id, slug or _slugify(title))
        cur = c.execute(
            "INSERT INTO collections "
            "(owner_user_id, slug, title, description, visibility, is_system, "
            " cover_image_url, item_count, view_count, follower_count, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,0,0,0,?,?)",
            (owner_id, final_slug, title, description, visibility,
             1 if is_system else 0, cover_image_url, now, now),
        )
        return int(cur.lastrowid)


def list_user_collections(
    owner_id: int, *, include_system: bool = True,
) -> list[dict]:
    with db.conn() as c:
        if include_system:
            rows = c.execute(
                "SELECT * FROM collections WHERE owner_user_id = ? "
                "ORDER BY is_system DESC, updated_at DESC",
                (owner_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM collections WHERE owner_user_id = ? "
                "AND is_system = 0 ORDER BY updated_at DESC",
                (owner_id,),
            ).fetchall()
    return [_row_to_dict(r, viewer_user_id=owner_id) for r in rows]


def get_collection(
    collection_id: int,
    *,
    viewer_user_id: Optional[int] = None,
    bump_views: bool = False,
) -> Optional[dict]:
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM collections WHERE id = ?", (collection_id,),
        ).fetchone()
        if not row:
            return None
        if not _can_view(row, viewer_user_id):
            raise PermissionError("not visible to viewer")
        # AUDIT (MED): de-dup the bump per (viewer, collection) inside the
        # throttle window so a refresh-spammer can't inflate view_count.
        # Owners are always skipped; anonymous viewers don't have a
        # stable id to key on, so they still hit the raw bump path (the
        # rate-limit middleware already bounds anon traffic up-stream).
        if bump_views and viewer_user_id != row["owner_user_id"]:
            should_bump = (
                viewer_user_id is None
                or _should_bump_view(viewer_user_id, collection_id)
            )
            if should_bump:
                c.execute(
                    "UPDATE collections SET view_count = view_count + 1 WHERE id = ?",
                    (collection_id,),
                )
    return _row_to_dict(row, viewer_user_id=viewer_user_id)


def get_collection_by_slug(
    owner_handle: str,
    slug: str,
    *,
    viewer_user_id: Optional[int] = None,
    bump_views: bool = False,
) -> Optional[dict]:
    """Resolve the public URL ``/c/{handle}/{slug}`` to a row."""
    with db.conn() as c:
        row = c.execute(
            "SELECT c.* FROM collections c "
            "JOIN users u ON u.id = c.owner_user_id "
            "WHERE u.username = ? AND c.slug = ?",
            (owner_handle, slug),
        ).fetchone()
        if not row:
            return None
        if not _can_view(row, viewer_user_id):
            raise PermissionError("not visible to viewer")
        # AUDIT (MED): mirror the per-(viewer, collection) throttle from
        # get_collection so the public /c/{handle}/{slug} surface can't
        # be reload-spammed for popularity inflation either.
        if bump_views and viewer_user_id != row["owner_user_id"]:
            should_bump = (
                viewer_user_id is None
                or _should_bump_view(viewer_user_id, row["id"])
            )
            if should_bump:
                c.execute(
                    "UPDATE collections SET view_count = view_count + 1 WHERE id = ?",
                    (row["id"],),
                )
    return _row_to_dict(row, viewer_user_id=viewer_user_id)


def update_collection(
    collection_id: int,
    *,
    owner_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    visibility: Optional[str] = None,
    cover_image_url: Optional[str] = None,
) -> Optional[dict]:
    sets: list[str] = []
    params: list = []
    if title is not None:
        t = title.strip()
        if not t:
            raise ValueError("title cannot be empty")
        sets.append("title = ?")
        params.append(t)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if visibility is not None:
        if visibility not in VALID_VISIBILITY:
            raise ValueError("invalid visibility")
        sets.append("visibility = ?")
        params.append(visibility)
    if cover_image_url is not None:
        sets.append("cover_image_url = ?")
        params.append(cover_image_url)
    if not sets:
        return get_collection(collection_id, viewer_user_id=owner_id)

    with db.conn() as c:
        row = c.execute(
            "SELECT owner_user_id, is_system FROM collections WHERE id = ?",
            (collection_id,),
        ).fetchone()
        if not row:
            return None
        if row["owner_user_id"] != owner_id:
            raise PermissionError("not owner")
        if row["is_system"] and title is not None:
            # System boards keep their canonical title.
            raise PermissionError("system collections cannot be renamed")
        params.extend([int(time.time()), collection_id])
        c.execute(
            f"UPDATE collections SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
            tuple(params),
        )
    return get_collection(collection_id, viewer_user_id=owner_id)


def delete_collection(collection_id: int, *, owner_id: int) -> bool:
    with db.conn() as c:
        row = c.execute(
            "SELECT owner_user_id, is_system FROM collections WHERE id = ?",
            (collection_id,),
        ).fetchone()
        if not row:
            return False
        if row["owner_user_id"] != owner_id:
            raise PermissionError("not owner")
        if row["is_system"]:
            raise PermissionError("system collections cannot be deleted")
        c.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    return True


# ── Items ─────────────────────────────────────────────────────────────────


def _assert_collection_mutable(c, collection_id: int, owner_id: int) -> sqlite3.Row:
    row = c.execute(
        "SELECT id, owner_user_id, is_system FROM collections WHERE id = ?",
        (collection_id,),
    ).fetchone()
    if not row:
        raise LookupError("collection not found")
    if row["owner_user_id"] != owner_id:
        raise PermissionError("not owner")
    if row["is_system"]:
        raise PermissionError("system collections are read-only")
    return row


def add_item(
    collection_id: int,
    *,
    owner_id: int,
    item_type: str,
    item_ref: str,
    note: Optional[str] = None,
) -> int:
    if item_type not in VALID_ITEM_TYPES:
        raise ValueError("invalid item_type")
    ref = (item_ref or "").strip()
    if not ref:
        raise ValueError("item_ref required")

    now = int(time.time())
    with db.conn() as c:
        _assert_collection_mutable(c, collection_id, owner_id)
        # Deduplicate: same (collection, type, ref) maps to one item.
        existing = c.execute(
            "SELECT id FROM collection_items "
            "WHERE collection_id = ? AND item_type = ? AND item_ref = ?",
            (collection_id, item_type, ref),
        ).fetchone()
        if existing:
            return int(existing["id"])
        # Append at the end.
        max_pos = c.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM collection_items "
            "WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()["p"]
        cur = c.execute(
            "INSERT INTO collection_items "
            "(collection_id, item_type, item_ref, position, note, added_at) "
            "VALUES (?,?,?,?,?,?)",
            (collection_id, item_type, ref, int(max_pos), note, now),
        )
        c.execute(
            "UPDATE collections SET item_count = ("
            " SELECT COUNT(*) FROM collection_items WHERE collection_id = ?), "
            " updated_at = ? WHERE id = ?",
            (collection_id, now, collection_id),
        )
        return int(cur.lastrowid)


def list_items(collection_id: int) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM collection_items WHERE collection_id = ? "
            "ORDER BY position ASC, id ASC",
            (collection_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_item(
    collection_id: int, item_id: int, *, owner_id: int,
) -> bool:
    now = int(time.time())
    with db.conn() as c:
        _assert_collection_mutable(c, collection_id, owner_id)
        cur = c.execute(
            "DELETE FROM collection_items WHERE id = ? AND collection_id = ?",
            (item_id, collection_id),
        )
        if cur.rowcount == 0:
            return False
        c.execute(
            "UPDATE collections SET item_count = ("
            " SELECT COUNT(*) FROM collection_items WHERE collection_id = ?), "
            " updated_at = ? WHERE id = ?",
            (collection_id, now, collection_id),
        )
    return True


def reorder_items(
    collection_id: int,
    *,
    owner_id: int,
    ordering: Iterable[dict],
) -> int:
    """Rewrite positions from the payload ``[{item_id, position}, ...]``.

    Ignores ids that don't belong to *collection_id* rather than raising
    mid-batch — the frontend may send stale ids during rapid drag events.
    Returns the rowcount of items actually updated.
    """
    updates = []
    for entry in ordering:
        try:
            iid = int(entry["item_id"])
            pos = int(entry["position"])
        except (TypeError, ValueError, KeyError):
            continue
        updates.append((pos, iid, collection_id))
    if not updates:
        return 0
    now = int(time.time())
    total = 0
    with db.conn() as c:
        _assert_collection_mutable(c, collection_id, owner_id)
        for pos, iid, cid in updates:
            cur = c.execute(
                "UPDATE collection_items SET position = ? "
                "WHERE id = ? AND collection_id = ?",
                (pos, iid, cid),
            )
            total += cur.rowcount
        c.execute(
            "UPDATE collections SET updated_at = ? WHERE id = ?",
            (now, collection_id),
        )
    return total


# ── Follows ─────────────────────────────────────────────────────────────


def follow_collection(user_id: int, collection_id: int) -> bool:
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT owner_user_id, visibility FROM collections WHERE id = ?",
            (collection_id,),
        ).fetchone()
        if not row:
            raise LookupError("collection not found")
        if row["owner_user_id"] == user_id:
            # Owners implicitly "follow" their own boards.
            return False
        # You can only follow shared or public boards.
        if row["visibility"] not in ("shared", "public"):
            raise PermissionError("collection is private")
        c.execute(
            "INSERT OR IGNORE INTO collection_follows "
            "(user_id, collection_id, followed_at, notifications_on) "
            "VALUES (?, ?, ?, 1)",
            (user_id, collection_id, now),
        )
        c.execute(
            "UPDATE collections SET follower_count = ("
            "  SELECT COUNT(*) FROM collection_follows WHERE collection_id = ?) "
            "WHERE id = ?",
            (collection_id, collection_id),
        )
    return True


def unfollow_collection(user_id: int, collection_id: int) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM collection_follows "
            "WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        )
        if cur.rowcount:
            c.execute(
                "UPDATE collections SET follower_count = ("
                "  SELECT COUNT(*) FROM collection_follows WHERE collection_id = ?) "
                "WHERE id = ?",
                (collection_id, collection_id),
            )
        return cur.rowcount > 0


def is_following(user_id: int, collection_id: int) -> bool:
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM collection_follows "
            "WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        ).fetchone()
    return row is not None


def set_follow_notifications(
    user_id: int, collection_id: int, notifications_on: bool,
) -> bool:
    """Toggle notifications_on for an existing follow row. Returns True if
    the row was found and updated, False if the user isn't following."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE collection_follows SET notifications_on = ? "
            "WHERE user_id = ? AND collection_id = ?",
            (1 if notifications_on else 0, user_id, collection_id),
        )
        return cur.rowcount > 0


def list_public_by_owner(owner_user_id: int, *, limit: int = 20) -> list[dict]:
    """Public collections authored by a given user — powers the
    /profile page section and owner-page listings."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM collections "
            "WHERE owner_user_id = ? AND visibility = 'public' "
            "ORDER BY is_featured DESC, updated_at DESC LIMIT ?",
            (owner_user_id, max(1, min(int(limit), 100))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_followers(
    collection_id: int, *, only_notifiable: bool = False,
) -> list[int]:
    with db.conn() as c:
        if only_notifiable:
            rows = c.execute(
                "SELECT user_id FROM collection_follows "
                "WHERE collection_id = ? AND notifications_on = 1",
                (collection_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT user_id FROM collection_follows WHERE collection_id = ?",
                (collection_id,),
            ).fetchall()
    return [int(r["user_id"]) for r in rows]


def list_user_follows(user_id: int) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT c.*, f.followed_at, f.notifications_on "
            "FROM collection_follows f "
            "JOIN collections c ON c.id = f.collection_id "
            "WHERE f.user_id = ? ORDER BY f.followed_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r, viewer_user_id=user_id) for r in rows]


# ── Explore / admin ─────────────────────────────────────────────────────


def featured_collections(limit: int = 20) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM collections "
            "WHERE is_featured = 1 AND visibility = 'public' "
            "ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def most_followed_collections(limit: int = 20) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM collections "
            "WHERE visibility = 'public' "
            "ORDER BY follower_count DESC, view_count DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def recently_updated_collections(limit: int = 20) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM collections "
            "WHERE visibility = 'public' AND item_count > 0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_featured(collection_id: int, flag: bool) -> bool:
    """Admin-only. Caller must have already validated the admin session."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE collections SET is_featured = ?, updated_at = ? WHERE id = ?",
            (1 if flag else 0, int(time.time()), collection_id),
        )
        return cur.rowcount > 0


def list_all_public_for_admin(limit: int = 200) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT c.*, u.username AS owner_username "
            "FROM collections c JOIN users u ON u.id = c.owner_user_id "
            "WHERE c.visibility = 'public' "
            "ORDER BY c.is_featured DESC, c.updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["owner_username"] = r["owner_username"]
        out.append(d)
    return out


# ── Auto-collections (Saved + Watchlist) ────────────────────────────────


_SYSTEM_DEFS = {
    "saved": {
        "title": "Saved",
        "description": "Your saved predictions, kept in sync automatically.",
    },
    "watchlist": {
        "title": "Watchlist",
        "description": "Sources you follow, kept in sync automatically.",
    },
}


def ensure_system_collections(user_id: int) -> dict[str, int]:
    """Guarantee the user has the auto-created system rows. Idempotent —
    returns ``{slug: collection_id}`` for every system slug."""
    out: dict[str, int] = {}
    with db.conn() as c:
        for slug, defs in _SYSTEM_DEFS.items():
            row = c.execute(
                "SELECT id FROM collections "
                "WHERE owner_user_id = ? AND slug = ? AND is_system = 1",
                (user_id, slug),
            ).fetchone()
            if row:
                out[slug] = int(row["id"])
                continue
            now = int(time.time())
            cur = c.execute(
                "INSERT INTO collections "
                "(owner_user_id, slug, title, description, visibility, "
                " is_system, item_count, view_count, follower_count, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'private', 1, 0, 0, 0, ?, ?)",
                (user_id, slug, defs["title"], defs["description"], now, now),
            )
            out[slug] = int(cur.lastrowid)
    return out


def rebuild_system_collection_items(user_id: int, slug: str) -> int:
    """Rewrite the items in a system collection from the authoritative
    source tables. Cheap on small result sets; callers invoke this on
    read rather than on every save/follow event. Returns the item count.
    """
    if slug not in SYSTEM_SLUGS:
        raise ValueError("unknown system slug")
    ids = ensure_system_collections(user_id)
    cid = ids[slug]
    now = int(time.time())

    with db.conn() as c:
        c.execute(
            "DELETE FROM collection_items WHERE collection_id = ?", (cid,),
        )
        if slug == "saved":
            rows = c.execute(
                "SELECT prediction_id FROM saved_predictions "
                "WHERE user_id = ? ORDER BY saved_at DESC",
                (user_id,),
            ).fetchall()
            for pos, r in enumerate(rows):
                c.execute(
                    "INSERT INTO collection_items "
                    "(collection_id, item_type, item_ref, position, added_at) "
                    "VALUES (?, 'prediction', ?, ?, ?)",
                    (cid, str(r["prediction_id"]), pos, now),
                )
            item_count = len(rows)
        else:  # watchlist
            rows = c.execute(
                "SELECT source_handle FROM followed_sources "
                "WHERE user_id = ? ORDER BY followed_at DESC",
                (user_id,),
            ).fetchall()
            for pos, r in enumerate(rows):
                c.execute(
                    "INSERT INTO collection_items "
                    "(collection_id, item_type, item_ref, position, added_at) "
                    "VALUES (?, 'source', ?, ?, ?)",
                    (cid, r["source_handle"], pos, now),
                )
            item_count = len(rows)
        c.execute(
            "UPDATE collections SET item_count = ?, updated_at = ? WHERE id = ?",
            (item_count, now, cid),
        )
    return item_count
