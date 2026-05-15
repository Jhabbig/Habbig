"""Collections — Spotify-style playlists for markets/sources/predictions.

Routes registered via ``register(app)``:

  JSON API (auth required unless noted)
    POST   /api/collections
    GET    /api/collections/me
    GET    /api/collections/{id}
    PATCH  /api/collections/{id}
    DELETE /api/collections/{id}
    POST   /api/collections/{id}/items
    DELETE /api/collections/{id}/items/{item_id}
    POST   /api/collections/{id}/items/reorder
    POST   /api/collections/{id}/follow
    DELETE /api/collections/{id}/follow
    GET    /api/collections/follows/me
    GET    /api/collections/{id}/items  (only if viewer can see)

  HTML pages
    GET /collections               — dashboard, user's own + followed
    GET /collections/{id}          — edit/view
    GET /c/{handle}/{slug}         — public page (SEO-indexable if public)
    GET /explore                   — featured + most-followed + recent

  Admin
    GET    /admin/collections      — HTML curation list
    POST   /admin/api/collections/{id}/feature  — toggle is_featured

The HTML is kept in this file rather than split to templates so it's
easy to review + deploy in one go. Inline CSS is scoped to the page
classes so it can't bleed into the rest of the dashboard shell.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
from queries import collections as coll
from security.rate_limiter import rate_limit


log = logging.getLogger("collections_routes")


def _follow_rate_key(request: Request) -> str:
    """Per-user rate-limit bucket for follow/unfollow.

    AUDIT (MED): the follow/unfollow endpoints had no per-user throttle,
    so a logged-in attacker could thrash ``follower_count`` on a board
    by spamming POST/DELETE pairs — inflating the most-followed list,
    burning DB writes, and spamming the underlying ``collection_follows``
    table. 30 actions per minute per user matches the realistic ceiling
    for a human clicking the Follow button and is shared between POST
    and DELETE so the attacker can't dodge it by alternating verbs.

    Anonymous requests (which will 401 in the handler anyway) fall back
    to the canonicalised client IP so a burst of unauthed hits can't
    burn the decorator without a counter.
    """
    import server
    user = server.current_user(request)
    if user:
        return f"coll-follow:user:{user['user_id']}"
    from security.rate_limiter import get_client_ip
    return f"coll-follow:anon:{get_client_ip(request)}"


# ── Helpers ───────────────────────────────────────────────────────────────


def _require_user(request: Request) -> dict:
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    return user


def _optional_user(request: Request) -> Optional[dict]:
    import server
    return server.current_user(request)


def _require_admin(request: Request) -> dict:
    import server
    return server._require_admin_user(request)


def _owner_handle(user_id: int) -> str:
    """Resolve a user's public handle for the ``/c/{handle}/{slug}`` URL."""
    with db.conn() as c:
        row = c.execute(
            "SELECT username FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return row["username"] if row else ""


def _notify_followers_async(collection_id: int, title: str, item_type: str, item_ref: str) -> None:
    """Fan-out notifications to everyone who follows this collection.

    Fire-and-forget; persistence failures don't block the HTTP response.
    Runs inside the route's event loop via asyncio.create_task.
    """
    try:
        from notifications import create_notification
    except Exception:
        return
    followers = coll.list_followers(collection_id, only_notifiable=True)
    if not followers:
        return

    async def _fanout():
        body = f"New {item_type} added to “{title}”"
        for uid in followers:
            try:
                await create_notification(
                    user_id=uid,
                    type="collection_update",
                    title=f"{title} got a new {item_type}",
                    body=body,
                    link_url=f"/collections/{collection_id}",
                    metadata={
                        "collection_id": collection_id,
                        "item_type": item_type,
                        "item_ref": item_ref,
                    },
                )
            except Exception:
                log.exception("collections: notify follower failed uid=%s", uid)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_fanout())
        else:
            loop.run_until_complete(_fanout())
    except Exception:
        log.exception("collections: notification fan-out scheduling failed")


# ── Item resolution — fetch metadata for rendering ──────────────────────


def _resolve_items(items: list[dict]) -> list[dict]:
    """Hydrate each item with display metadata. Lazy lookups so a board
    with 100 items stays fast — markets come from the unified cache,
    predictions hit the predictions table, sources hit source_credibility.
    """
    if not items:
        return []

    market_ids = [it["item_ref"] for it in items if it["item_type"] == "market"]
    source_handles = [it["item_ref"] for it in items if it["item_type"] == "source"]
    prediction_ids = [int(it["item_ref"]) for it in items if it["item_type"] == "prediction"
                      and str(it["item_ref"]).isdigit()]

    market_map: dict = {}
    if market_ids:
        try:
            from backend.markets import unified_markets
            # Read from the enrichment cache — we don't want to pay a
            # Polymarket/Kalshi fetch on every board view.
            cached = unified_markets._get_cached("enriched_markets", 120)
            if cached is None:
                cached = unified_markets._get_cached("unified_markets", 300) or []
            for m in cached:
                if m.id in market_ids:
                    market_map[m.id] = {
                        "title": m.title,
                        "source": m.source,
                        "yes_price": m.yes_price,
                        "url": m.url,
                        "category": m.category,
                    }
        except Exception as exc:
            log.warning("resolve_items: markets lookup failed: %s", exc)

    source_map: dict = {}
    if source_handles:
        try:
            with db.conn() as c:
                placeholders = ",".join("?" * len(source_handles))
                rows = c.execute(
                    f"SELECT source_handle, global_credibility, total_predictions, "
                    f"       correct_predictions "
                    f"FROM source_credibility WHERE source_handle IN ({placeholders})",
                    tuple(source_handles),
                ).fetchall()
                for r in rows:
                    source_map[r["source_handle"]] = dict(r)
        except Exception:
            pass

    prediction_map: dict = {}
    if prediction_ids:
        try:
            with db.conn() as c:
                placeholders = ",".join("?" * len(prediction_ids))
                rows = c.execute(
                    f"SELECT id, source_handle, content, direction, "
                    f"       predicted_probability, resolved, resolved_correct "
                    f"FROM predictions WHERE id IN ({placeholders})",
                    tuple(prediction_ids),
                ).fetchall()
                for r in rows:
                    prediction_map[int(r["id"])] = dict(r)
        except Exception:
            pass

    out = []
    for it in items:
        meta = None
        t = it["item_type"]
        ref = it["item_ref"]
        if t == "market":
            meta = market_map.get(ref)
        elif t == "source":
            meta = source_map.get(ref)
        elif t == "prediction" and str(ref).isdigit():
            meta = prediction_map.get(int(ref))
        out.append({**it, "meta": meta})
    return out


# ── JSON API handlers ───────────────────────────────────────────────────


async def api_create(request: Request):
    user = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        cid = coll.create_collection(
            owner_id=user["user_id"],
            title=(body.get("title") or "").strip(),
            description=body.get("description"),
            visibility=body.get("visibility") or "private",
            cover_image_url=body.get("cover_image_url"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = coll.get_collection(cid, viewer_user_id=user["user_id"])
    return JSONResponse(row, status_code=201)


async def api_list_mine(request: Request):
    user = _require_user(request)
    # Make sure system collections exist and their mirror items are fresh
    # — cheap because they're primary-keyed reads.
    coll.ensure_system_collections(user["user_id"])
    coll.rebuild_system_collection_items(user["user_id"], "saved")
    coll.rebuild_system_collection_items(user["user_id"], "watchlist")
    own = coll.list_user_collections(user["user_id"])
    follows = coll.list_user_follows(user["user_id"])
    return JSONResponse({"own": own, "followed": follows})


async def api_get(request: Request, id: int):
    viewer = _optional_user(request)
    vid = viewer["user_id"] if viewer else None
    try:
        row = coll.get_collection(int(id), viewer_user_id=vid, bump_views=True)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Collection not found")
    if not row:
        raise HTTPException(status_code=404, detail="Collection not found")
    # Keep system boards self-hydrating — don't serve stale mirrors.
    if row["is_system"] and row["owner_user_id"] == vid:
        coll.rebuild_system_collection_items(vid, row["slug"])
    items = coll.list_items(row["id"])
    return JSONResponse({
        "collection": row,
        "items": _resolve_items(items),
        "owner_handle": _owner_handle(row["owner_user_id"]),
        "is_following": bool(vid) and coll.is_following(vid, row["id"]),
    })


async def api_update(request: Request, id: int):
    user = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        row = coll.update_collection(
            int(id), owner_id=user["user_id"],
            title=body.get("title"),
            description=body.get("description"),
            visibility=body.get("visibility"),
            cover_image_url=body.get("cover_image_url"),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not row:
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse(row)


async def api_delete(request: Request, id: int):
    user = _require_user(request)
    try:
        ok = coll.delete_collection(int(id), owner_id=user["user_id"])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse({"deleted": True})


async def api_add_item(request: Request, id: int):
    user = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        item_id = coll.add_item(
            int(id), owner_id=user["user_id"],
            item_type=(body.get("item_type") or "").strip().lower(),
            item_ref=(body.get("item_ref") or "").strip(),
            note=body.get("note"),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Collection not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Notify followers (best effort; non-blocking).
    row = coll.get_collection(int(id), viewer_user_id=user["user_id"])
    if row:
        _notify_followers_async(
            row["id"], row["title"],
            item_type=body.get("item_type") or "", item_ref=body.get("item_ref") or "",
        )
    return JSONResponse({"item_id": item_id}, status_code=201)


async def api_remove_item(request: Request, id: int, item_id: int):
    user = _require_user(request)
    try:
        ok = coll.remove_item(int(id), int(item_id), owner_id=user["user_id"])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Collection not found")
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return JSONResponse({"deleted": True})


async def api_reorder(request: Request, id: int):
    user = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    ordering = body if isinstance(body, list) else body.get("ordering") or []
    if not isinstance(ordering, list):
        raise HTTPException(status_code=400, detail="ordering must be a list")
    try:
        n = coll.reorder_items(int(id), owner_id=user["user_id"], ordering=ordering)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse({"updated": n})


@rate_limit(limit=30, window_seconds=60, key_func=_follow_rate_key)
async def api_follow(request: Request, id: int):
    user = _require_user(request)
    try:
        coll.follow_collection(user["user_id"], int(id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse({"following": True})


@rate_limit(limit=30, window_seconds=60, key_func=_follow_rate_key)
async def api_unfollow(request: Request, id: int):
    user = _require_user(request)
    coll.unfollow_collection(user["user_id"], int(id))
    return JSONResponse({"following": False})


async def api_update_follow(request: Request, id: int):
    """PATCH /api/collections/{id}/follow { notifications_on: bool }

    Users can mute noisy boards without unfollowing. Returns 404 if the
    caller isn't following, so the UI can correct a stale "following"
    indicator without a separate round-trip.
    """
    user = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if "notifications_on" not in body:
        raise HTTPException(status_code=400, detail="notifications_on required")
    ok = coll.set_follow_notifications(
        user["user_id"], int(id), bool(body.get("notifications_on")),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Not following this collection")
    return JSONResponse({
        "following": True,
        "notifications_on": bool(body.get("notifications_on")),
    })


async def api_follows_me(request: Request):
    user = _require_user(request)
    return JSONResponse({"follows": coll.list_user_follows(user["user_id"])})


async def api_explore(request: Request):
    """Public — no auth required. Powers the /explore page + future mobile clients."""
    return JSONResponse({
        "featured": coll.featured_collections(20),
        "most_followed": coll.most_followed_collections(20),
        "recent": coll.recently_updated_collections(20),
    })


async def api_search_candidates(request: Request):
    """Typeahead back-end for the "Add items" modal.

    Query params:
        q    required — search term (min 2 chars)
        kind optional — "market" | "source" | "prediction" (default: all)
        limit per-kind (default 8, max 20)

    Returns ``{results: [{item_type, item_ref, title, subtitle}]}``
    deduplicated by (item_type, item_ref). Search is case-insensitive
    and scoped to what the typeahead needs to display a row — expensive
    joins (credibility, volume) are avoided for latency.
    """
    user = _require_user(request)
    q = (request.query_params.get("q") or "").strip()
    kind = (request.query_params.get("kind") or "").strip().lower()
    try:
        limit = max(1, min(int(request.query_params.get("limit") or "8"), 20))
    except ValueError:
        limit = 8
    if len(q) < 2:
        return JSONResponse({"results": []})

    results: list[dict] = []
    like = f"%{q}%"

    # Markets — served from the already-enriched unified-markets cache so
    # we don't pay a Polymarket/Kalshi fetch on every keystroke.
    if kind in ("", "market"):
        try:
            from backend.markets import unified_markets
            cached = (unified_markets._get_cached("enriched_markets", 120)
                      or unified_markets._get_cached("unified_markets", 300)
                      or [])
            q_lower = q.lower()
            count = 0
            for m in cached:
                if count >= limit:
                    break
                if q_lower in (m.title or "").lower() or q_lower in (m.id or "").lower():
                    results.append({
                        "item_type": "market",
                        "item_ref": m.id,
                        "title": m.title,
                        "subtitle": f"{(m.source or '').capitalize()} · {int((m.yes_price or 0) * 100)}% YES",
                    })
                    count += 1
        except Exception as exc:
            log.warning("search_candidates: markets failed: %s", exc)

    # Sources — match handle prefix first, then substring.
    if kind in ("", "source"):
        try:
            with db.conn() as c:
                rows = c.execute(
                    "SELECT source_handle, global_credibility, total_predictions "
                    "FROM source_credibility "
                    "WHERE source_handle LIKE ? OR source_handle LIKE ? "
                    "ORDER BY global_credibility DESC LIMIT ?",
                    (f"{q}%", like, limit),
                ).fetchall()
            for r in rows:
                results.append({
                    "item_type": "source",
                    "item_ref": r["source_handle"],
                    "title": f"@{r['source_handle']}",
                    "subtitle": f"credibility {float(r['global_credibility'] or 0):.2f} · {r['total_predictions']} predictions",
                })
        except Exception as exc:
            log.warning("search_candidates: sources failed: %s", exc)

    # Predictions — content substring. Capped via LIMIT so an OR across
    # 300k+ rows doesn't full-scan; relies on the predictions table's
    # existing text index if present.
    if kind in ("", "prediction"):
        try:
            with db.conn() as c:
                rows = c.execute(
                    "SELECT id, content, source_handle, category "
                    "FROM predictions WHERE content LIKE ? "
                    "ORDER BY extracted_at DESC LIMIT ?",
                    (like, limit),
                ).fetchall()
            for r in rows:
                snippet = (r["content"] or "")[:120]
                results.append({
                    "item_type": "prediction",
                    "item_ref": str(r["id"]),
                    "title": snippet,
                    "subtitle": f"@{r['source_handle']} · {r['category']}",
                })
        except Exception as exc:
            log.warning("search_candidates: predictions failed: %s", exc)

    return JSONResponse({"results": results})


# ── RSS feed for public collections ─────────────────────────────────────


async def rss_feed(request: Request, handle: str, slug: str):
    """Atom-style RSS feed — public collections only.

    Each collection_item becomes a channel item with the item's title as
    <title> and a link into narve.ai where the underlying market/source/
    prediction lives. Private/shared collections 404 so feed readers
    can't fingerprint private board names.
    """
    try:
        row = coll.get_collection_by_slug(handle, slug, viewer_user_id=None)
    except PermissionError:
        raise HTTPException(status_code=404)
    if not row or row["visibility"] != "public":
        raise HTTPException(status_code=404)

    items = _resolve_items(coll.list_items(row["id"]))
    # updated_at is a unix timestamp (from collections.updated_at) — feed
    # readers expect RFC822.
    def _rfc822(ts: int) -> str:
        import email.utils as eu
        return eu.formatdate(timeval=int(ts or time.time()), localtime=False, usegmt=True)

    site = "https://narve.ai"
    feed_url = f"{site}/c/{handle}/{slug}.rss"
    page_url = f"{site}/c/{handle}/{slug}"
    title = row["title"]
    desc = row["description"] or f"A narve.ai collection by @{handle}."

    entries_xml = []
    for it in items:
        meta = it.get("meta") or {}
        if it["item_type"] == "market":
            link = meta.get("url") or f"{site}/collections/{row['id']}"
            item_title = meta.get("title") or it["item_ref"]
            item_desc = f"{(meta.get('source') or '').capitalize()} · {int((meta.get('yes_price') or 0)*100)}% YES"
        elif it["item_type"] == "source":
            link = f"{site}/sources/{it['item_ref']}"
            item_title = f"@{it['item_ref']}"
            cred = meta.get("global_credibility")
            item_desc = f"Credibility {cred:.2f}" if cred else "Source profile"
        else:
            link = f"{site}/p/{it['item_ref']}"
            item_title = (meta.get("content") or f"Prediction #{it['item_ref']}")[:160]
            item_desc = f"Prediction by @{meta.get('source_handle') or 'unknown'}"
        entries_xml.append(
            "<item>"
            f"<title>{_html.escape(item_title)}</title>"
            f"<link>{_html.escape(link)}</link>"
            f"<guid isPermaLink=\"false\">narve-coll-{row['id']}-item-{it['id']}</guid>"
            f"<pubDate>{_rfc822(it.get('added_at') or 0)}</pubDate>"
            f"<description>{_html.escape(item_desc)}</description>"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        f'    <title>{_html.escape(title)} — narve.ai</title>\n'
        f'    <link>{_html.escape(page_url)}</link>\n'
        f'    <description>{_html.escape(desc)}</description>\n'
        f'    <language>en</language>\n'
        f'    <lastBuildDate>{_rfc822(row.get("updated_at") or 0)}</lastBuildDate>\n'
        f'    <atom:link href="{_html.escape(feed_url)}" rel="self" type="application/rss+xml" />\n'
        + "\n".join(f"    {e}" for e in entries_xml) + "\n"
        '  </channel>\n'
        '</rss>\n'
    )
    from fastapi.responses import Response
    return Response(
        content=xml,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ── Admin ───────────────────────────────────────────────────────────────


async def admin_list(request: Request):
    _require_admin(request)
    return JSONResponse({"collections": coll.list_all_public_for_admin(200)})


async def admin_toggle_feature(request: Request, id: int):
    admin = _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    flag = bool(body.get("is_featured"))
    ok = coll.set_featured(int(id), flag)
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found")
    log.info("admin %s set is_featured=%s on collection %s",
             admin.get("email"), flag, id)
    return JSONResponse({"id": int(id), "is_featured": flag})


# ── HTML pages ──────────────────────────────────────────────────────────


_PAGE_CSS = """
<style>
:root { --ink:#0d0d0d; --bg:#ffffff; --muted:#666; --border:#e5e5e5;
        --soft:#fafafa; --accent:#0d0d0d; }
[data-theme='dark'] { --ink:#f5f5f5; --bg:#0d0d0d; --muted:#a3a3a3;
                       --border:#1f1f1f; --soft:#141414; }
body { background: var(--bg); color: var(--ink); font-family: 'Inter', system-ui, sans-serif;
       margin:0; padding:40px 24px; min-height:100vh; }
.c-wrap { max-width: 1080px; margin: 0 auto; }
.c-head { margin-bottom: 32px; }
.c-title { font-family: 'Playfair Display', serif; font-style: italic;
           font-size: 48px; letter-spacing: -0.02em; margin: 0 0 4px; }
.c-sub { color: var(--muted); font-size: 13px; letter-spacing: 0.08em;
         text-transform: uppercase; }
.c-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
          gap: 18px; }
.c-card { background: var(--soft); border: 1px solid var(--border);
          border-radius: 12px; padding: 18px; text-decoration: none;
          color: inherit; display: block; transition: transform 0.1s ease; }
.c-card:hover { transform: translateY(-1px); border-color: var(--ink); }
.c-card-title { font-weight: 600; font-size: 16px; margin: 0 0 6px; }
.c-card-desc { color: var(--muted); font-size: 13px; line-height: 1.45;
               min-height: 36px; margin-bottom: 12px; }
.c-card-meta { display: flex; gap: 12px; font-size: 11px;
               color: var(--muted); text-transform: uppercase;
               letter-spacing: 0.06em; }
.c-chip { display: inline-block; padding: 2px 8px; border: 1px solid var(--border);
          border-radius: 999px; font-size: 10px; text-transform: uppercase;
          letter-spacing: 0.08em; color: var(--muted); }
.c-chip-system { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.c-chip-featured { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.c-btn { display: inline-block; padding: 8px 14px; border-radius: 6px;
         background: var(--ink); color: var(--bg); font-size: 12px;
         font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
         border: 0; cursor: pointer; text-decoration: none; }
.c-btn-ghost { background: transparent; color: var(--ink);
               border: 1px solid var(--ink); }
.c-empty { padding: 60px 20px; text-align: center; color: var(--muted);
           border: 1px dashed var(--border); border-radius: 12px; }
.c-section-title { font-size: 13px; font-weight: 600; text-transform: uppercase;
                   letter-spacing: 0.1em; margin: 32px 0 14px; color: var(--ink); }
.c-item { display: flex; align-items: flex-start; gap: 14px; padding: 14px;
          border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px;
          background: var(--bg); }
.c-item-drag { color: var(--muted); font-size: 14px; cursor: grab;
               user-select: none; padding-top: 2px; }
.c-item-kind { font-size: 10px; color: var(--muted); text-transform: uppercase;
               letter-spacing: 0.1em; font-weight: 600; }
.c-item-body { flex: 1; }
.c-item-title { font-weight: 500; margin-top: 4px; }
.c-item-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
.c-form-field { margin-bottom: 14px; }
.c-form-field label { display: block; font-size: 11px; color: var(--muted);
                      text-transform: uppercase; letter-spacing: 0.08em;
                      margin-bottom: 6px; }
.c-form-field input, .c-form-field textarea, .c-form-field select {
  width: 100%; padding: 10px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--bg); color: var(--ink);
  font-family: inherit;
  /* 16px is the iOS Safari auto-zoom threshold — anything smaller
   * triggers an annoying input zoom on focus. Bump from 14px → 16px. */
  font-size: 16px;
}
.c-bar { display: flex; justify-content: space-between; align-items: center;
         gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.c-actions { display: flex; gap: 8px; }
.c-back { color: var(--muted); font-size: 12px; text-decoration: none;
          letter-spacing: 0.06em; text-transform: uppercase; }
.c-back:hover { color: var(--ink); }
</style>
"""


def _card_html(c: dict, link: str) -> str:
    """Full-width feed row for the collections list.

    Layout mirrors the editorial pattern shared by /predictions, /saved,
    and /sources: avatar (initial), title in Instrument Serif Italic,
    description in Source Serif 4 body, item count in Geist Mono on the
    right. Pure monochrome — no chips, no decoration.
    """
    title_raw = (c.get("title") or "Untitled").strip()
    title = _html.escape(title_raw)
    desc = _html.escape((c.get("description") or "").strip()[:160])
    initial = _html.escape(title_raw[:1].upper() or "?")
    vis = _html.escape((c.get("visibility") or "private").upper())
    meta_bits = [vis]
    if c.get("is_system"):
        meta_bits.append("SYSTEM")
    if c.get("is_featured"):
        meta_bits.append("FEATURED")
    meta = " · ".join(meta_bits)
    items = int(c.get("item_count") or 0)
    followers = int(c.get("follower_count") or 0)
    return (
        f'<li>'
        f'<a class="feed-row" href="{_html.escape(link)}">'
        f'<span class="feed-avatar" aria-hidden="true">{initial}</span>'
        f'<div class="feed-body">'
        f'<div class="feed-handle">{meta} · {followers} followers</div>'
        f'<h3 class="feed-row-title">{title}</h3>'
        f'<p class="feed-prose">{desc}</p>'
        f'</div>'
        f'<div class="feed-stats">'
        f'<span class="feed-stat-value">{items}</span>'
        f'<span class="feed-stat-label">Items</span>'
        f'</div>'
        f'<span class="feed-action feed-action--ghost" aria-hidden="true">Open</span>'
        f'</a>'
        f'</li>'
    )


async def page_collections(request: Request):
    user = _require_user(request)
    coll.ensure_system_collections(user["user_id"])
    coll.rebuild_system_collection_items(user["user_id"], "saved")
    coll.rebuild_system_collection_items(user["user_id"], "watchlist")
    own = coll.list_user_collections(user["user_id"])
    followed = coll.list_user_follows(user["user_id"])

    # Editorial feed rows — full-width, monochrome, no decorative chrome.
    import server as _srv

    own_rows = "".join(
        _card_html(c, f"/collections/{c['id']}") for c in own
    ) or _srv.render_empty(
        title="No collections yet",
        body="Create your first board to bundle markets, sources, and predictions into a shareable playlist.",
        actions=[
            {"label": "New collection", "href": "#", "primary": True},
            {"label": "Browse public", "href": "/explore"},
        ],
    )

    # Followed rows mirror the own-rows layout but append an inline notify
    # toggle wired up via [data-follow-id]; the bell glyph is gone — text
    # label only, monochrome.
    def _followed_row_html(c: dict) -> str:
        notif_on = bool(c.get("notifications_on", 1))
        cid = c["id"]
        row = _card_html(c, f"/collections/{cid}")
        # Inject a sibling toggle directly after the row's closing </a>. The
        # row is wrapped in <li>...<a>...</a></li>; we insert before </li>.
        toggle = (
            f'<button type="button" data-follow-id="{cid}" '
            f'data-notif-on="{1 if notif_on else 0}" '
            f'class="feed-action feed-action--ghost" '
            f'style="margin-top:var(--space-2);align-self:flex-start">'
            f'{"Notify" if notif_on else "Muted"}</button>'
        )
        return row.replace("</a></li>", f"</a>{toggle}</li>")

    followed_rows = "".join(_followed_row_html(c) for c in followed) or _srv.render_empty(
        title="Not following any collections",
        body="Discover public boards on the explore page — follow one to keep its updates in your feed.",
        actions=[{"label": "Explore", "href": "/explore", "primary": True}],
    )

    # Sidebar from the shared helper so /collections matches the rest of
    # the app (dashboards, billing, settings, etc.).
    try:
        from sidebar import render_sidebar as _render_sidebar
        _admin_link = ""
        if user.get("is_admin"):
            _admin_link = '<a href="/admin">Admin</a>'
        sidebar_html = _render_sidebar(
            request,
            active="collections",
            username=user.get("username") or user.get("email") or "",
            raw_admin_link=_admin_link,
        )
    except Exception:
        sidebar_html = ""

    return _srv.render_page(
        "collections",
        request=request,
        raw_sidebar=sidebar_html,
        raw_own_rows=own_rows,
        raw_followed_rows=followed_rows,
    )


def _render_detail_item(it: dict, *, is_owner: bool, is_system: bool) -> str:
    """One row in the collection detail items list — full-width editorial."""
    kind = it["item_type"]
    ref = str(it["item_ref"])
    meta = it.get("meta") or {}
    if kind == "market":
        title = meta.get("title") or ref
        source = (meta.get("source") or "").upper()
        yes_pct = int((meta.get("yes_price") or 0) * 100)
        handle_line = f"{source} · {ref}"
        stat_value = f"{yes_pct}%"
        stat_label = "YES"
        href = f"/market/{ref}"
    elif kind == "source":
        title = ref
        cred = meta.get("global_credibility")
        handle_line = f"@{ref}"
        stat_value = f"{cred:.2f}" if cred else "—"
        stat_label = "Credibility"
        href = f"/source/{ref}"
    else:
        content = (meta.get("content") or "")[:200]
        title = content or f"Prediction #{ref}"
        handle_line = f"@{meta.get('source_handle') or 'unknown'}"
        stat_value = "—"
        stat_label = (meta.get("status") or "open").upper()[:8]
        href = f"/predictions/{ref}"

    init = (title.strip()[:1] or "?").upper()
    drag = (
        '<span class="c-item-drag" aria-hidden="true" '
        'style="cursor:grab;color:var(--text-tertiary);'
        'font-family:var(--font-mono);padding-right:var(--space-2)">::</span>'
        if is_owner and not is_system else ""
    )
    remove = ""
    if is_owner and not is_system:
        remove = (
            f'<button type="button" class="feed-action feed-action--ghost" '
            f'onclick="__hbColl.remove({it["id"]})">Remove</button>'
        )

    return (
        f'<li data-item-id="{it["id"]}">'
        f'<a class="feed-row" href="{_html.escape(href)}">'
        f'{drag}'
        f'<span class="feed-avatar" aria-hidden="true">{_html.escape(init)}</span>'
        f'<div class="feed-body">'
        f'<div class="feed-kind">{_html.escape(kind)}</div>'
        f'<div class="feed-handle">{_html.escape(handle_line)}</div>'
        f'<p class="feed-prose">{_html.escape(title)}</p>'
        f'</div>'
        f'<div class="feed-stats">'
        f'<span class="feed-stat-value">{_html.escape(stat_value)}</span>'
        f'<span class="feed-stat-label">{_html.escape(stat_label)}</span>'
        f'</div>'
        f'{remove}'
        f'</a>'
        f'</li>'
    )


def _render_detail_page(
    request: Request,
    *,
    row: dict,
    items: list[dict],
    viewer: Optional[dict],
    is_owner: bool,
    is_system: bool,
    is_following: bool,
    owner_handle: str,
    is_public_seo: bool = False,
):
    """Shared detail renderer for both /collections/{id} and /c/{handle}/{slug}."""
    import server as _srv

    title_raw = row["title"] or "Untitled"
    desc_raw = row["description"] or ""
    vis = row["visibility"]

    items_html = "".join(
        _render_detail_item(it, is_owner=is_owner, is_system=is_system) for it in items
    )
    if not items_html:
        items_html = _srv.render_empty(
            title="No items yet" if is_owner else "This collection is empty",
            body=(
                "Add your first market, source, or prediction with the button above."
                if is_owner and not is_system
                else "The owner hasn't added anything to this collection."
            ),
            actions=(
                [{"label": "Add items", "href": "#", "primary": True}]
                if is_owner and not is_system else []
            ),
        )

    # Action buttons
    actions: list[str] = []
    if row["visibility"] in ("shared", "public"):
        share_url = f"/c/{owner_handle}/{row['slug']}"
        actions.append(
            f'<button type="button" class="feed-action feed-action--ghost" '
            f'id="c-share-btn" data-share-url="{_html.escape(share_url)}">Share</button>'
        )
    if not is_owner and row["visibility"] in ("shared", "public"):
        label = "Following" if is_following else "Follow"
        actions.append(
            f'<button type="button" class="feed-action" id="c-follow-btn" '
            f'data-following="{"1" if is_following else "0"}" '
            f'aria-pressed="{"true" if is_following else "false"}">{label}</button>'
        )
    if is_owner and not is_system:
        actions.append(
            '<button type="button" class="feed-action" id="c-add-btn">Add items</button>'
        )
        actions.append(
            '<button type="button" class="feed-action feed-action--ghost" '
            'id="c-delete-btn">Delete</button>'
        )

    actions_html = "".join(actions)

    system_banner = ""
    if is_system:
        system_banner = (
            '<div style="margin-top:var(--space-4);padding:var(--space-3) var(--space-4);'
            'border:1px solid var(--border-subtle);border-radius:var(--radius-md);'
            'color:var(--text-tertiary);font-size:var(--text-xs);font-family:var(--font-ui);'
            'font-weight:500">'
            'This collection is maintained by narve — items sync automatically '
            'from your saves and watchlist. It can\'t be edited or deleted.'
            '</div>'
        )

    title_editable = (not is_system) and is_owner
    raw_title_editable = ' contenteditable="true" spellcheck="false"' if title_editable else ""
    raw_desc_editable = raw_title_editable

    desc_for_display = desc_raw or ("Add a description…" if title_editable else "")

    share_url = f"/c/{owner_handle}/{row['slug']}"
    canonical = share_url if vis == "public" else f"/collections/{row['id']}"
    robots = "index,follow" if vis == "public" else "noindex,nofollow"

    # Sidebar (signed-in only; public SEO page hides it).
    sidebar_html = ""
    if not is_public_seo:
        try:
            from sidebar import render_sidebar as _render_sidebar
            _admin_link_d = ""
            if (viewer or {}).get("is_admin"):
                _admin_link_d = '<a href="/admin">Admin</a>'
            sidebar_html = _render_sidebar(
                request,
                active="collections",
                username=(viewer or {}).get("username")
                         or (viewer or {}).get("email") or "",
                raw_admin_link=_admin_link_d,
            )
        except Exception:
            sidebar_html = ""

    return _srv.render_page(
        "collection_detail",
        request=request,
        raw_sidebar=sidebar_html,
        collection_id=row["id"],
        collection_title=title_raw,
        collection_description=desc_for_display,
        owner_handle=owner_handle,
        visibility_label=vis.upper(),
        item_count=row.get("item_count") or 0,
        follower_count=row.get("follower_count") or 0,
        canonical_url=canonical,
        robots=robots,
        raw_title_editable=raw_title_editable,
        raw_desc_editable=raw_desc_editable,
        raw_actions=actions_html,
        raw_system_banner=system_banner,
        raw_items=items_html,
        raw_is_owner_js="true" if is_owner else "false",
        raw_is_system_js="true" if is_system else "false",
    )


async def page_collection_detail(request: Request, id: int):
    viewer = _optional_user(request)
    vid = viewer["user_id"] if viewer else None
    try:
        row = coll.get_collection(int(id), viewer_user_id=vid, bump_views=True)
    except PermissionError:
        raise HTTPException(status_code=404)
    if not row:
        raise HTTPException(status_code=404)
    # Keep system boards in sync on every view so a new save/follow shows up.
    if row["is_system"] and row["owner_user_id"] == vid:
        coll.rebuild_system_collection_items(vid, row["slug"])
    items = _resolve_items(coll.list_items(row["id"]))
    owner_handle = _owner_handle(row["owner_user_id"])
    is_owner = bool(row["is_owner"])
    is_system = bool(row["is_system"])
    is_following = bool(vid) and coll.is_following(vid, row["id"])
    return _render_detail_page(
        request,
        row=row,
        items=items,
        viewer=viewer,
        is_owner=is_owner,
        is_system=is_system,
        is_following=is_following,
        owner_handle=owner_handle,
        is_public_seo=False,
    )


async def page_public(request: Request, handle: str, slug: str):
    """Public SEO page. Indexable only when visibility=public.

    Uses the same `collection_detail.html` template as the dashboard view
    so the editorial layout, type system, and monochrome treatment are
    identical between owner and reader. Sidebar is suppressed on the
    public page so SEO crawlers see a focused, sidebar-free document.
    """
    viewer = _optional_user(request)
    vid = viewer["user_id"] if viewer else None
    try:
        row = coll.get_collection_by_slug(handle, slug, viewer_user_id=vid, bump_views=True)
    except PermissionError:
        raise HTTPException(status_code=404)
    if not row:
        raise HTTPException(status_code=404)

    items = _resolve_items(coll.list_items(row["id"]))
    is_owner = bool(row.get("is_owner"))
    is_system = bool(row.get("is_system"))
    is_following = bool(vid) and coll.is_following(vid, row["id"])

    return _render_detail_page(
        request,
        row=row,
        items=items,
        viewer=viewer,
        is_owner=is_owner,
        is_system=is_system,
        is_following=is_following,
        owner_handle=handle,
        is_public_seo=True,
    )


async def page_explore(request: Request):
    featured = coll.featured_collections(12)
    most = coll.most_followed_collections(12)
    recent = coll.recently_updated_collections(12)

    def _list(rows: list[dict]) -> str:
        if not rows:
            return '<div class="nv-empty" role="status"><p class="nv-empty__body">Nothing here yet.</p></div>'
        return '<ul class="feed-list">' + "".join(
            _card_html(c, f"/collections/{c['id']}") for c in rows
        ) + '</ul>'

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'>
<title>Explore collections — narve.ai</title>
<meta name="description" content="Browse narve.ai's editor-picked and most-followed collections of prediction markets, sources, and calls.">
<meta name="robots" content="index,follow">
<meta property="og:title" content="Explore collections — narve.ai">
<meta property="og:type" content="website">
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<link rel='stylesheet' href='/_gateway_static/pages/feeds.css'>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
</head><body>
<div class="feed-shell">
<header class="feed-hero">
  <div class="feed-eyebrow">Discover</div>
  <h1 class="feed-title">Explore</h1>
  <p class="feed-lede">Editor-picked and most-followed collections from across narve.</p>
</header>
<h2 class="feed-section-title">Editor's picks</h2>
{_list(featured)}
<h2 class="feed-section-title">Most followed</h2>
{_list(most)}
<h2 class="feed-section-title">Recently updated</h2>
{_list(recent)}
</div>
</body></html>"""
    return HTMLResponse(body)


async def page_admin_collections(request: Request):
    admin = _require_admin(request)
    rows = coll.list_all_public_for_admin(200)

    def _row(c: dict) -> str:
        feat_cls = "c-chip-featured" if c["is_featured"] else ""
        label = "Unfeature" if c["is_featured"] else "Feature"
        return (
            f'<tr data-id="{c["id"]}">'
            f'<td>{_html.escape(c["title"])}</td>'
            f'<td>@{_html.escape(c.get("owner_username") or "")}</td>'
            f'<td>{c.get("item_count") or 0}</td>'
            f'<td>{c.get("follower_count") or 0}</td>'
            f'<td>{c.get("view_count") or 0}</td>'
            f'<td><span class="c-chip {feat_cls}">{"Featured" if c["is_featured"] else "—"}</span></td>'
            f'<td><button class="c-btn c-btn-ghost" data-toggle="{0 if c["is_featured"] else 1}">{label}</button></td>'
            f'</tr>'
        )

    rows_html = "".join(_row(c) for c in rows) or \
                '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No public collections yet.</td></tr>'

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Admin · Collections</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
{_PAGE_CSS}
<style>
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; padding:10px 14px; font-size:11px; text-transform:uppercase;
      letter-spacing:0.08em; color: var(--muted); border-bottom:1px solid var(--border); }}
td {{ padding:12px 14px; border-bottom:1px solid var(--border); }}
</style>
</head><body>
<div class="c-wrap">
<div class="c-head">
  <h1 class="c-title">Admin · Collections</h1>
  <p class="c-sub">Curate the editor's picks shown on /explore</p>
</div>
<table>
<thead><tr><th>Title</th><th>Owner</th><th>Items</th><th>Followers</th><th>Views</th><th>Status</th><th></th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>
<script>
(function(){{
  document.querySelectorAll('tbody button').forEach(function(btn){{
    btn.addEventListener('click', async function(){{
      var tr = btn.closest('tr');
      var id = tr.dataset.id;
      var flag = btn.dataset.toggle === '1';
      btn.disabled = true;
      try {{
        var r = await fetch('/admin/api/collections/' + id + '/feature', {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{is_featured: flag}}),
        }});
        if (!r.ok) {{ alert('Failed'); btn.disabled = false; return; }}
        location.reload();
      }} catch (e) {{ alert(e.message); btn.disabled = false; }}
    }});
  }});
}})();
</script>
</body></html>"""
    return HTMLResponse(body)


# ── Registration ────────────────────────────────────────────────────────


def register(app) -> None:
    # JSON API
    app.add_api_route("/api/collections", api_create, methods=["POST"])
    app.add_api_route("/api/collections/me", api_list_mine, methods=["GET"])
    app.add_api_route("/api/collections/follows/me", api_follows_me, methods=["GET"])
    app.add_api_route("/api/collections/explore", api_explore, methods=["GET"])
    app.add_api_route("/api/collections/search", api_search_candidates, methods=["GET"])
    app.add_api_route("/api/collections/{id}", api_get, methods=["GET"])
    app.add_api_route("/api/collections/{id}", api_update, methods=["PATCH"])
    app.add_api_route("/api/collections/{id}", api_delete, methods=["DELETE"])
    app.add_api_route("/api/collections/{id}/items", api_add_item, methods=["POST"])
    app.add_api_route("/api/collections/{id}/items/reorder", api_reorder, methods=["POST"])
    app.add_api_route(
        "/api/collections/{id}/items/{item_id}", api_remove_item, methods=["DELETE"],
    )
    app.add_api_route("/api/collections/{id}/follow", api_follow, methods=["POST"])
    app.add_api_route("/api/collections/{id}/follow", api_update_follow, methods=["PATCH"])
    app.add_api_route("/api/collections/{id}/follow", api_unfollow, methods=["DELETE"])

    # Admin
    app.add_api_route("/admin/api/collections/{id}/feature",
                      admin_toggle_feature, methods=["POST"])

    # HTML pages
    app.add_api_route("/collections", page_collections, methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route("/collections/{id}", page_collection_detail, methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)
    # Register the RSS route BEFORE the HTML variant. FastAPI matches
    # routes in registration order and the HTML path's ``{slug}`` would
    # otherwise greedily swallow the trailing ``.rss``, leaving feed
    # readers with a 404.
    app.add_api_route("/c/{handle}/{slug}.rss", rss_feed, methods=["GET"],
                      include_in_schema=False)
    app.add_api_route("/c/{handle}/{slug}", page_public, methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route("/explore", page_explore, methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route("/admin/collections", page_admin_collections, methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)
