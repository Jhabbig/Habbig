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


log = logging.getLogger("collections_routes")


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


async def api_follow(request: Request, id: int):
    user = _require_user(request)
    try:
        coll.follow_collection(user["user_id"], int(id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="Collection not found")
    return JSONResponse({"following": True})


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
  font-family: inherit; font-size: 14px;
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
    title = _html.escape(c.get("title") or "Untitled")
    desc = _html.escape((c.get("description") or "").strip()[:120])
    chips = []
    if c.get("is_system"):
        chips.append('<span class="c-chip c-chip-system">System</span>')
    if c.get("is_featured"):
        chips.append('<span class="c-chip c-chip-featured">Featured</span>')
    vis = _html.escape(c.get("visibility") or "private")
    chips.append(f'<span class="c-chip">{vis}</span>')
    return (
        f'<a class="c-card" href="{_html.escape(link)}">'
        f'<div class="c-card-title">{title}</div>'
        f'<div class="c-card-desc">{desc}</div>'
        f'<div class="c-card-meta">'
        f'<span>{c.get("item_count") or 0} items</span>'
        f'<span>{c.get("follower_count") or 0} followers</span>'
        f'</div>'
        f'<div style="margin-top:10px">{" ".join(chips)}</div>'
        f'</a>'
    )


async def page_collections(request: Request):
    user = _require_user(request)
    coll.ensure_system_collections(user["user_id"])
    coll.rebuild_system_collection_items(user["user_id"], "saved")
    coll.rebuild_system_collection_items(user["user_id"], "watchlist")
    own = coll.list_user_collections(user["user_id"])
    followed = coll.list_user_follows(user["user_id"])

    own_cards = "".join(
        _card_html(c, f"/collections/{c['id']}") for c in own
    ) or '<div class="c-empty">No collections yet. Create your first board below.</div>'

    # Followed cards get a small bell toggle in the top-right corner that
    # mutes notifications without un-following (notifications_on column).
    def _followed_card_html(c: dict) -> str:
        notif_on = bool(c.get("notifications_on", 1))
        cid = c["id"]
        card = _card_html(c, f"/collections/{cid}")
        bell_color = "var(--ink)" if notif_on else "var(--muted)"
        bell_glyph = "\U0001F514" if notif_on else "\U0001F515"
        bell_title = "Mute notifications" if notif_on else "Unmute notifications"
        notif_flag = 1 if notif_on else 0
        return (
            '<div style="position:relative">'
            f'{card}'
            f'<button type="button" data-follow-id="{cid}" '
            f'data-notif-on="{notif_flag}" class="c-bell" title="{bell_title}" '
            f'style="position:absolute;top:10px;right:10px;background:var(--bg);'
            f'border:1px solid var(--border);cursor:pointer;font-size:12px;padding:4px 8px;'
            f'border-radius:6px;color:{bell_color}">{bell_glyph}</button>'
            '</div>'
        )

    if followed:
        followed_cards = "".join(_followed_card_html(c) for c in followed)
    else:
        followed_cards = '<div class="c-empty">You\'re not following any collections. Head to /explore.</div>'

    # Sidebar from the shared helper so /collections matches the rest of
    # the app (dashboards, billing, settings, etc.). Without this, the page
    # had no back-affordance — the user complaint that triggered this fix.
    try:
        from sidebar import render_sidebar as _render_sidebar
        _admin_link = ""
        try:
            import server as _srv_mod
            if user.get("is_admin"):
                _admin_link = '<a href="/admin">Admin</a>'
        except Exception:
            pass
        sidebar_html = _render_sidebar(
            request,
            active="collections",
            username=user.get("username") or user.get("email") or "",
            raw_admin_link=_admin_link,
        )
    except Exception:
        sidebar_html = ""

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Collections — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<link rel='stylesheet' href='/_gateway_static/components.css'>
<script>(function(){{try{{var m=document.cookie.match(/narve-theme=([^;]*)/)||document.cookie.match(/betyc-theme=([^;]*)/);var t=(m&&m[1])||localStorage.getItem("narve-theme")||localStorage.getItem("betyc-theme")||"light";document.documentElement.setAttribute("data-theme",t);}}catch(e){{document.documentElement.setAttribute("data-theme","light");}}}})();</script>
{_PAGE_CSS}
<style>
/* Collections wraps in the shared app-shell so the sidebar appears on
   the left and the existing .c-* page styles continue to work. */
.c-page-body {{ padding: 24px 32px; max-width: 1200px; margin: 0 auto; }}
.c-breadcrumb {{ font-size: 12px; color: var(--text-tertiary); margin-bottom: 16px; display: flex; gap: 6px; align-items: center; }}
.c-breadcrumb a {{ color: var(--text-secondary); text-decoration: none; }}
.c-breadcrumb a:hover {{ color: var(--text-primary); text-decoration: underline; }}
</style>
</head><body>
<div class="app-shell">
{sidebar_html}
<main class="main-content">
<div class="c-page-body">
<nav class="c-breadcrumb" aria-label="Breadcrumb">
  <a href="/dashboards">Dashboards</a>
  <span aria-hidden="true">/</span>
  <span aria-current="page">Collections</span>
</nav>
<div class="c-wrap">
<div class="c-head">
  <div class="c-bar">
    <div>
      <h1 class="c-title">Collections</h1>
      <p class="c-sub">Playlists for markets, sources, and predictions</p>
    </div>
    <div class="c-actions">
      <a class="c-btn c-btn-ghost" href="/explore">Browse public</a>
      <button class="c-btn" id="c-new-btn">New collection</button>
    </div>
  </div>
</div>

<div class="c-section-title">Your collections</div>
<div class="c-grid">{own_cards}</div>

<div class="c-section-title">Following</div>
<div class="c-grid">{followed_cards}</div>
</div>

<dialog id="c-new-dialog" style="border:1px solid var(--border);border-radius:12px;padding:24px;max-width:460px;width:90%;background:var(--bg);color:var(--ink)">
  <form method="dialog" id="c-new-form">
    <h3 style="margin:0 0 16px">New collection</h3>
    <div class="c-form-field">
      <label>Title</label>
      <input name="title" required maxlength="80" placeholder="e.g. Fed meetings Q2">
    </div>
    <div class="c-form-field">
      <label>Description</label>
      <textarea name="description" rows="3" maxlength="500"></textarea>
    </div>
    <div class="c-form-field">
      <label>Visibility</label>
      <select name="visibility">
        <option value="private">Private (only you)</option>
        <option value="shared">Shared (any signed-in narve user)</option>
        <option value="public">Public (indexed, shareable)</option>
      </select>
    </div>
    <div class="c-actions" style="justify-content:flex-end">
      <button type="button" class="c-btn c-btn-ghost" id="c-new-cancel">Cancel</button>
      <button type="submit" class="c-btn">Create</button>
    </div>
  </form>
</dialog>

<script>
(function(){{
  var dlg = document.getElementById('c-new-dialog');
  var open = document.getElementById('c-new-btn');
  var cancel = document.getElementById('c-new-cancel');
  var form = document.getElementById('c-new-form');
  if (!dlg || !open) return;
  open.addEventListener('click', function(){{ dlg.showModal(); }});
  cancel.addEventListener('click', function(){{ dlg.close(); }});
  form.addEventListener('submit', async function(ev){{
    ev.preventDefault();
    var fd = new FormData(form);
    var body = {{
      title: fd.get('title'),
      description: fd.get('description') || null,
      visibility: fd.get('visibility') || 'private',
    }};
    var r = await fetch('/api/collections', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(body),
    }});
    if (!r.ok) {{
      var err = await r.json().catch(function(){{ return {{}}; }});
      alert(err.detail || 'Create failed');
      return;
    }}
    var data = await r.json();
    location.href = '/collections/' + data.id;
  }});

  // Mute/un-mute notifications on followed cards (bell toggle).
  document.querySelectorAll('.c-bell[data-follow-id]').forEach(function(btn){{
    btn.addEventListener('click', async function(ev){{
      ev.preventDefault(); ev.stopPropagation();
      var cid = btn.dataset.followId;
      var current = btn.dataset.notifOn === '1';
      btn.disabled = true;
      try {{
        var csrf = (document.cookie.match(/(?:^|;\\s*)_csrf=([^;]*)/) || [])[1] || '';
        var r = await fetch('/api/collections/' + cid + '/follow', {{
          method: 'PATCH',
          headers: {{'Content-Type': 'application/json', 'x-csrf-token': csrf}},
          body: JSON.stringify({{ notifications_on: !current }}),
        }});
        if (!r.ok) throw new Error('failed');
        btn.dataset.notifOn = current ? '0' : '1';
        btn.textContent = current ? '\\ud83d\\udd15' : '\\ud83d\\udd14';
        btn.title = current ? 'Unmute notifications' : 'Mute notifications';
        btn.style.color = current ? 'var(--muted)' : 'var(--ink)';
      }} catch (e) {{
        alert('Could not update notifications.');
      }} finally {{
        btn.disabled = false;
      }}
    }});
  }});
}})();
</script>
</div>
</main>
</div>
</body></html>"""
    return HTMLResponse(body)


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
    is_owner = row["is_owner"]
    is_system = row["is_system"]
    is_following = bool(vid) and coll.is_following(vid, row["id"])

    def _item_html(it: dict) -> str:
        kind = it["item_type"]
        ref = _html.escape(str(it["item_ref"]))
        meta = it.get("meta") or {}
        if kind == "market":
            title = _html.escape(meta.get("title") or ref)
            sub = f"{_html.escape(meta.get('source') or '').capitalize()} · {int((meta.get('yes_price') or 0)*100)}% YES"
        elif kind == "source":
            title = f"@{ref}"
            cred = meta.get("global_credibility")
            sub = f"credibility {cred:.2f}" if cred else "—"
        else:
            content = (meta.get("content") or "")[:140]
            title = _html.escape(content or f"Prediction #{ref}")
            sub = f"by @{_html.escape(meta.get('source_handle') or '')}" if meta.get("source_handle") else ""
        drag = '<span class="c-item-drag" aria-hidden="true">⋮⋮</span>' if is_owner and not is_system else ""
        remove = ""
        if is_owner and not is_system:
            remove = (
                f'<button class="c-btn c-btn-ghost" style="margin-left:auto" '
                f'onclick="__hbColl.remove({it["id"]})">Remove</button>'
            )
        return (
            f'<div class="c-item" data-item-id="{it["id"]}">'
            f'{drag}'
            f'<div class="c-item-body">'
            f'<div class="c-item-kind">{_html.escape(kind)}</div>'
            f'<div class="c-item-title">{title}</div>'
            f'<div class="c-item-sub">{_html.escape(sub)}</div>'
            f'</div>{remove}</div>'
        )

    items_html = "".join(_item_html(it) for it in items)
    if not items_html:
        if is_owner and not is_system:
            items_html = '<div class="c-empty">No items yet. Add your first market, source, or prediction.</div>'
        else:
            items_html = '<div class="c-empty">This collection is empty.</div>'

    follow_block = ""
    if not is_owner and row["visibility"] in ("shared", "public"):
        follow_block = (
            f'<button class="c-btn" id="c-follow-btn" data-following="{"1" if is_following else "0"}">'
            f'{"Following ✓" if is_following else "Follow"}</button>'
        )

    # Share button — only makes sense once the board isn't private, and
    # we always link to the SEO canonical /c/{handle}/{slug} so the link
    # survives users changing their handle down the road.
    share_btn = ""
    if row["visibility"] in ("shared", "public"):
        share_url = f"/c/{owner_handle}/{_html.escape(row['slug'])}"
        share_btn = (
            f'<button class="c-btn c-btn-ghost" id="c-share-btn" '
            f'data-share-url="{share_url}" title="Copy link to clipboard">Share</button>'
        )

    owner_actions = ""
    if is_owner:
        delete_btn = (
            '<button class="c-btn c-btn-ghost" id="c-delete-btn">Delete</button>'
            if not is_system else ""
        )
        add_btn = (
            '<button class="c-btn" id="c-add-btn">Add items</button>'
            if not is_system else ""
        )
        owner_actions = f'<div class="c-actions">{add_btn}{delete_btn}</div>'

    # System boards get a read-only banner so the UI is never confusing.
    system_banner = ""
    if is_system:
        system_banner = (
            '<div style="padding:12px 16px;border:1px solid var(--border);'
            'border-radius:8px;color:var(--muted);font-size:12px;margin-bottom:16px">'
            'This collection is maintained by narve — items sync automatically '
            'from your saves and watchlist. It can\'t be edited or deleted.'
            '</div>'
        )

    title_editable = (not is_system) and is_owner
    title_html = _html.escape(row["title"])
    desc_html = _html.escape(row["description"] or "")
    vis = row["visibility"]
    share_url = f"/c/{_html.escape(owner_handle)}/{_html.escape(row['slug'])}"
    og_url = share_url if vis == "public" else f"/collections/{row['id']}"

    # Sidebar via shared helper, same as list page above.
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

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'>
<title>{title_html} — narve.ai Collections</title>
<meta name="description" content="{desc_html[:160]}">
<meta property="og:title" content="{title_html}">
<meta property="og:description" content="{desc_html[:160]}">
<meta property="og:url" content="{og_url}">
<meta property="og:type" content="website">
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<link rel='stylesheet' href='/_gateway_static/components.css'>
<script>(function(){{try{{var m=document.cookie.match(/narve-theme=([^;]*)/)||document.cookie.match(/betyc-theme=([^;]*)/);var t=(m&&m[1])||localStorage.getItem("narve-theme")||localStorage.getItem("betyc-theme")||"light";document.documentElement.setAttribute("data-theme",t);}}catch(e){{document.documentElement.setAttribute("data-theme","light");}}}})();</script>
{_PAGE_CSS}
<style>
.c-page-body {{ padding: 24px 32px; max-width: 1200px; margin: 0 auto; }}
.c-breadcrumb {{ font-size: 12px; color: var(--text-tertiary); margin-bottom: 16px; display: flex; gap: 6px; align-items: center; }}
.c-breadcrumb a {{ color: var(--text-secondary); text-decoration: none; }}
.c-breadcrumb a:hover {{ color: var(--text-primary); text-decoration: underline; }}
</style>
</head><body>
<div class="app-shell">
{sidebar_html}
<main class="main-content">
<div class="c-page-body">
<nav class="c-breadcrumb" aria-label="Breadcrumb">
  <a href="/dashboards">Dashboards</a>
  <span aria-hidden="true">/</span>
  <a href="/collections">Collections</a>
  <span aria-hidden="true">/</span>
  <span aria-current="page">{title_html}</span>
</nav>
<div class="c-wrap">
<a href="/collections" class="c-back">← Collections</a>
<div class="c-head" style="margin-top:14px">
  <div class="c-bar">
    <div>
      <h1 class="c-title" id="c-title"{' contenteditable="true"' if title_editable else ''}>{title_html}</h1>
      <p class="c-sub" id="c-desc"{' contenteditable="true"' if title_editable else ''}>
        {desc_html or ("Add a description…" if title_editable else "")}
      </p>
      <div style="margin-top:10px">
        <span class="c-chip">{_html.escape(vis)}</span>
        <span class="c-chip">{row.get("item_count") or 0} items</span>
        <span class="c-chip">{row.get("follower_count") or 0} followers</span>
      </div>
    </div>
    <div class="c-actions">
      {share_btn}
      {follow_block}
      {owner_actions}
    </div>
  </div>
</div>

{system_banner}

<div id="c-items">{items_html}</div>

<div id="c-add-modal" style="display:none;margin-top:18px;padding:16px;border:1px solid var(--border);border-radius:12px">
  <div class="c-form-field">
    <label>Search markets, sources, predictions</label>
    <input id="c-add-q" placeholder="Start typing…" autocomplete="off">
  </div>
  <div id="c-add-results" style="max-height:280px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px"></div>
  <div class="c-actions" style="justify-content:flex-end;margin-top:10px">
    <button class="c-btn c-btn-ghost" id="c-add-cancel">Close</button>
  </div>
</div>

<script>
(function(){{
  var COLLECTION_ID = {row["id"]};
  var IS_OWNER = {str(bool(is_owner)).lower()};
  var IS_SYSTEM = {str(bool(is_system)).lower()};

  async function api(method, path, body){{
    var opts = {{ method: method, headers: {{'Content-Type': 'application/json'}} }};
    if (body !== undefined) opts.body = JSON.stringify(body);
    var r = await fetch(path, opts);
    if (!r.ok) {{
      var err = await r.json().catch(function(){{ return {{}}; }});
      throw new Error(err.detail || ('HTTP ' + r.status));
    }}
    return r.json().catch(function(){{ return {{}}; }});
  }}

  window.__hbColl = {{
    remove: async function(itemId){{
      if (!confirm('Remove this item?')) return;
      try {{
        await api('DELETE', '/api/collections/' + COLLECTION_ID + '/items/' + itemId);
        location.reload();
      }} catch (e) {{ alert(e.message); }}
    }}
  }};

  // Inline title / description edit — PATCH on blur.
  if (IS_OWNER && !IS_SYSTEM) {{
    var titleEl = document.getElementById('c-title');
    var descEl = document.getElementById('c-desc');
    function save(field){{
      var payload = {{}};
      payload[field] = (field === 'title' ? titleEl.textContent : descEl.textContent).trim();
      api('PATCH', '/api/collections/' + COLLECTION_ID, payload)
        .catch(function(e){{ alert(e.message); }});
    }}
    if (titleEl) titleEl.addEventListener('blur', function(){{ save('title'); }});
    if (descEl) descEl.addEventListener('blur', function(){{ save('description'); }});
  }}

  var followBtn = document.getElementById('c-follow-btn');
  if (followBtn) {{
    followBtn.addEventListener('click', async function(){{
      var following = followBtn.dataset.following === '1';
      try {{
        await api(following ? 'DELETE' : 'POST',
                  '/api/collections/' + COLLECTION_ID + '/follow');
        followBtn.dataset.following = following ? '0' : '1';
        followBtn.textContent = following ? 'Follow' : 'Following \u2713';
      }} catch (e) {{ alert(e.message); }}
    }});
  }}

  var delBtn = document.getElementById('c-delete-btn');
  if (delBtn) {{
    delBtn.addEventListener('click', async function(){{
      if (!confirm('Delete this collection? This cannot be undone.')) return;
      try {{
        await api('DELETE', '/api/collections/' + COLLECTION_ID);
        location.href = '/collections';
      }} catch (e) {{ alert(e.message); }}
    }});
  }}

  var addBtn = document.getElementById('c-add-btn');
  var addModal = document.getElementById('c-add-modal');
  var addCancel = document.getElementById('c-add-cancel');
  var addQ = document.getElementById('c-add-q');
  var addResults = document.getElementById('c-add-results');
  if (addBtn && addModal) {{
    addBtn.addEventListener('click', function(){{
      addModal.style.display = 'block';
      if (addQ) addQ.focus();
    }});
    addCancel.addEventListener('click', function(){{ addModal.style.display = 'none'; }});

    // Debounced typeahead — hits /api/collections/search, renders rows,
    // each row is a button that POSTs directly to the items endpoint.
    var searchTimer = null;
    function escHtml(s){{
      return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){{
        return ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c];
      }});
    }}
    function renderResults(results){{
      if (!results || !results.length) {{
        addResults.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px;text-align:center">No matches. Try a different search.</div>';
        return;
      }}
      addResults.innerHTML = results.map(function(r){{
        return (
          '<button type="button" class="hbc-row" data-type="' + escHtml(r.item_type) +
          '" data-ref="' + escHtml(r.item_ref) + '" style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;border-radius:6px;cursor:pointer;background:transparent;border:0;width:100%;text-align:left;color:inherit;font:inherit">' +
          '<div style="flex:1;min-width:0">' +
          '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em">' + escHtml(r.item_type) + '</div>' +
          '<div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + escHtml(r.title) + '</div>' +
          '<div style="font-size:11px;color:var(--muted)">' + escHtml(r.subtitle || '') + '</div>' +
          '</div>' +
          '<span style="color:var(--muted);font-size:18px">+</span>' +
          '</button>'
        );
      }}).join('');
      addResults.querySelectorAll('button[data-type]').forEach(function(btn){{
        btn.addEventListener('click', async function(){{
          btn.disabled = true;
          try {{
            await api('POST', '/api/collections/' + COLLECTION_ID + '/items', {{
              item_type: btn.dataset.type,
              item_ref: btn.dataset.ref,
            }});
            location.reload();
          }} catch (e) {{
            btn.disabled = false;
            alert(e.message);
          }}
        }});
      }});
    }}
    if (addQ && addResults) {{
      addQ.addEventListener('input', function(){{
        clearTimeout(searchTimer);
        var q = addQ.value.trim();
        if (q.length < 2) {{
          addResults.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px;text-align:center">Type at least 2 characters…</div>';
          return;
        }}
        searchTimer = setTimeout(async function(){{
          try {{
            var data = await api('GET', '/api/collections/search?q=' + encodeURIComponent(q));
            renderResults(data.results || []);
          }} catch (e) {{
            addResults.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px;text-align:center">Search failed.</div>';
          }}
        }}, 180);
      }});
    }}
  }}

  // Share button — copies the canonical URL to clipboard.
  var shareBtn = document.getElementById('c-share-btn');
  if (shareBtn) {{
    shareBtn.addEventListener('click', async function(){{
      var url = shareBtn.dataset.shareUrl || location.href;
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(url);
        }} else {{
          // Fallback for http or older browsers — fall through to prompt.
          window.prompt('Copy this link:', url);
        }}
        var orig = shareBtn.textContent;
        shareBtn.textContent = 'Copied \u2713';
        setTimeout(function(){{ shareBtn.textContent = orig; }}, 1400);
      }} catch (e) {{
        window.prompt('Copy this link:', url);
      }}
    }});
  }}

  // Drag-to-reorder. Lightweight — no library. Pointer-based so it
  // works on touch + mouse without pulling in dragula.
  if (IS_OWNER && !IS_SYSTEM) {{
    var container = document.getElementById('c-items');
    if (container) {{
      var dragging = null;
      container.addEventListener('pointerdown', function(ev){{
        var handle = ev.target.closest('.c-item-drag');
        if (!handle) return;
        dragging = handle.closest('.c-item');
        dragging.setAttribute('aria-grabbed', 'true');
        dragging.style.opacity = '0.6';
      }});
      container.addEventListener('pointermove', function(ev){{
        if (!dragging) return;
        var after = null;
        var items = container.querySelectorAll('.c-item');
        for (var i = 0; i < items.length; i++) {{
          var item = items[i];
          if (item === dragging) continue;
          var rect = item.getBoundingClientRect();
          if (ev.clientY < rect.top + rect.height / 2) {{ after = item; break; }}
        }}
        if (after) container.insertBefore(dragging, after);
        else container.appendChild(dragging);
      }});
      container.addEventListener('pointerup', async function(){{
        if (!dragging) return;
        dragging.style.opacity = '';
        dragging.removeAttribute('aria-grabbed');
        dragging = null;
        var ordering = [];
        container.querySelectorAll('.c-item').forEach(function(el, idx){{
          ordering.push({{ item_id: parseInt(el.dataset.itemId), position: idx }});
        }});
        try {{
          await api('POST',
            '/api/collections/' + COLLECTION_ID + '/items/reorder',
            ordering);
        }} catch (e) {{ /* swallow — reload fixes divergence */ }}
      }});
    }}
  }}
}})();
</script>
</div>
</div>
</main>
</div>
</body></html>"""
    return HTMLResponse(body)


async def page_public(request: Request, handle: str, slug: str):
    """Public SEO page. Indexable only when visibility=public."""
    viewer = _optional_user(request)
    vid = viewer["user_id"] if viewer else None
    try:
        row = coll.get_collection_by_slug(handle, slug, viewer_user_id=vid, bump_views=True)
    except PermissionError:
        raise HTTPException(status_code=404)
    if not row:
        raise HTTPException(status_code=404)

    items = _resolve_items(coll.list_items(row["id"]))
    title_html = _html.escape(row["title"])
    desc_html = _html.escape(row["description"] or "")
    vis = row["visibility"]
    owner_handle = _html.escape(handle)
    robots = "index,follow" if vis == "public" else "noindex,nofollow"
    # Send readers to the dashboard-owned canonical so /c/ variants don't split link equity.
    canonical = f"/c/{owner_handle}/{_html.escape(slug)}"

    def _render_item(it: dict) -> str:
        kind = it["item_type"]
        meta = it.get("meta") or {}
        ref = _html.escape(str(it["item_ref"]))
        if kind == "market":
            title = _html.escape(meta.get("title") or ref)
            url = _html.escape(meta.get("url") or "")
            sub = f"{_html.escape((meta.get('source') or '').capitalize())} · {int((meta.get('yes_price') or 0)*100)}% YES"
            link_html = f'<a href="{url}" target="_blank" rel="noopener">Open on platform ↗</a>' if url else ""
        elif kind == "source":
            title = f"@{ref}"
            sub = f"credibility {meta.get('global_credibility'):.2f}" if meta.get("global_credibility") else "—"
            link_html = f'<a href="/sources/{ref}">View profile →</a>'
        else:
            content = (meta.get("content") or "")[:200]
            title = _html.escape(content or f"Prediction #{ref}")
            sub = f"by @{_html.escape(meta.get('source_handle') or '')}" if meta.get("source_handle") else ""
            link_html = ""
        return (
            f'<div class="c-item">'
            f'<div class="c-item-body">'
            f'<div class="c-item-kind">{_html.escape(kind)}</div>'
            f'<div class="c-item-title">{title}</div>'
            f'<div class="c-item-sub">{_html.escape(sub)} {link_html}</div>'
            f'</div></div>'
        )

    items_html = "".join(_render_item(it) for it in items) or \
                 '<div class="c-empty">This collection is empty.</div>'

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'>
<title>{title_html} · @{owner_handle} — narve.ai</title>
<meta name="description" content="{desc_html[:160]}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{title_html} · @{owner_handle}">
<meta property="og:description" content="{desc_html[:160]}">
<meta property="og:type" content="website">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="narve.ai">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title_html} · @{owner_handle}">
<meta name="twitter:description" content="{desc_html[:160]}">
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
{_PAGE_CSS}
</head><body>
<div class="c-wrap">
<a href="/explore" class="c-back">← Explore</a>
<div class="c-head" style="margin-top:14px">
  <div class="c-bar">
    <div>
      <h1 class="c-title">{title_html}</h1>
      <p class="c-sub">by @{owner_handle} · {row.get("item_count") or 0} items · {row.get("follower_count") or 0} followers</p>
    </div>
    <div class="c-actions">
      <button class="c-btn c-btn-ghost" id="c-share-btn" data-share-url="{canonical}">Share</button>
      {'<a class="c-btn c-btn-ghost" href="/c/' + owner_handle + '/' + _html.escape(slug) + '.rss" style="text-decoration:none">RSS</a>' if vis == "public" else ""}
    </div>
  </div>
  <p style="margin-top:16px;color:var(--muted);font-size:14px;line-height:1.55;max-width:640px">{desc_html}</p>
</div>
<div>{items_html}</div>
</div>
<script>
(function(){{
  var btn = document.getElementById('c-share-btn');
  if (!btn) return;
  btn.addEventListener('click', async function(){{
    var url = location.origin + btn.dataset.shareUrl;
    try {{
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        await navigator.clipboard.writeText(url);
      }} else {{
        window.prompt('Copy this link:', url);
      }}
      var orig = btn.textContent;
      btn.textContent = 'Copied \u2713';
      setTimeout(function(){{ btn.textContent = orig; }}, 1400);
    }} catch (e) {{
      window.prompt('Copy this link:', url);
    }}
  }});
}})();
</script>
</body></html>"""
    return HTMLResponse(body)


async def page_explore(request: Request):
    featured = coll.featured_collections(12)
    most = coll.most_followed_collections(12)
    recent = coll.recently_updated_collections(12)

    def _grid(rows: list[dict]) -> str:
        if not rows:
            return '<div class="c-empty">Nothing here yet.</div>'
        return '<div class="c-grid">' + "".join(
            _card_html(c, f"/collections/{c['id']}") for c in rows
        ) + '</div>'

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'>
<title>Explore collections — narve.ai</title>
<meta name="description" content="Browse narve.ai's editor-picked and most-followed collections of prediction markets, sources, and calls.">
<meta name="robots" content="index,follow">
<meta property="og:title" content="Explore collections — narve.ai">
<meta property="og:type" content="website">
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
{_PAGE_CSS}
</head><body>
<div class="c-wrap">
<div class="c-head">
  <h1 class="c-title">Explore</h1>
  <p class="c-sub">Editor-picked + most-followed collections</p>
</div>
<div class="c-section-title">Editor's picks</div>
{_grid(featured)}
<div class="c-section-title">Most followed</div>
{_grid(most)}
<div class="c-section-title">Recently updated</div>
{_grid(recent)}
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
