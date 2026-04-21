"""Topics routes — /api/topics and per-topic endpoints.

Pro-tier feature: users create up to 10 saved search topics that the
background scraper polls on a schedule; each topic accumulates matched
posts, extracts predictions, and generates a Claude analysis summary.

Extracted from server.py with zero behaviour change. All cross-module
references go through the lazy ``_srv()`` helper so imports stay one-way
(server imports this module at registration time, not the reverse).
"""

from __future__ import annotations

import json as _json
import logging
import sys

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import db


log = logging.getLogger("gateway.topics_routes")


def _srv():
    """Return the already-imported server module (for helpers + constants)."""
    return sys.modules.get("server") or sys.modules["__main__"]


async def api_list_topics(request: Request):
    user = _srv()._require_pro_user(request)
    topics = db.list_topics(user["user_id"])
    return JSONResponse({
        "topics": [
            {"id": t["id"], "name": t["name"],
             "keywords": _json.loads(t["keywords"]) if t["keywords"] else [],
             "schedule_minutes": t["schedule_minutes"],
             "last_pulled_at": t["last_pulled_at"],
             "posts_found_total": t["posts_found_total"],
             "predictions_extracted_total": t["predictions_extracted_total"],
             "is_active": bool(t["is_active"])}
            for t in topics
        ]
    })


async def api_create_topic(request: Request):
    srv = _srv()
    user = srv._require_pro_user(request)
    count = db.count_user_topics(user["user_id"])
    if count >= 10:
        return JSONResponse({"error": "Maximum 10 topics allowed for Pro tier"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    name = (body.get("name") or "").strip()
    keywords = body.get("keywords", [])
    try:
        schedule = int(body.get("schedule_minutes", 60))
    except (TypeError, ValueError):
        return JSONResponse({"error": "schedule_minutes must be an integer"}, status_code=400)
    if not name:
        return JSONResponse({"error": "Topic name required"}, status_code=400)
    field_max = srv.FIELD_MAX
    if len(name) > field_max["topic_name"]:
        return JSONResponse({"error": f"Topic name exceeds {field_max['topic_name']} characters"}, status_code=400)
    if not keywords or not isinstance(keywords, list):
        return JSONResponse({"error": "Keywords required (array)"}, status_code=400)
    if len(keywords) > 20:
        return JSONResponse({"error": "Maximum 20 keywords per topic"}, status_code=400)
    # Coerce, strip, and length-cap each keyword. Drop empties. Reject any
    # non-string element so attackers can't smuggle objects/arrays through.
    cleaned_kw = []
    for k in keywords:
        if not isinstance(k, str):
            return JSONResponse({"error": "Keywords must be strings"}, status_code=400)
        ks = k.strip()
        if not ks:
            continue
        if len(ks) > field_max["topic_keyword"]:
            return JSONResponse({"error": f"Keyword exceeds {field_max['topic_keyword']} characters"}, status_code=400)
        cleaned_kw.append(ks)
    if not cleaned_kw:
        return JSONResponse({"error": "At least one non-empty keyword is required"}, status_code=400)
    keywords = cleaned_kw
    if schedule not in (30, 60, 360, 1440):
        return JSONResponse({"error": "Schedule must be 30, 60, 360, or 1440 minutes"}, status_code=400)
    topic_id = db.create_topic(user["user_id"], name, keywords, schedule)
    log.info("User %s created topic '%s' (id=%d)", user.get("username"), name, topic_id)
    return JSONResponse({"id": topic_id, "name": name})


async def api_delete_topic(request: Request, topic_id: int):
    user = _srv()._require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.delete_topic(topic_id)
    return JSONResponse({"deleted": True})


async def api_topic_pull(request: Request, topic_id: int):
    srv = _srv()
    user = srv._require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    # Manual topic pulls trigger an upstream scrape — costly. Cap at 2
    # pulls per 30 minutes per (user, topic) pair so a single user cannot
    # spam the scraper or drain Anthropic API credits.
    rl_key = f"topic_pull:{user['user_id']}:{topic_id}"
    if srv._is_rate_limited(rl_key, limit=2, window=1800):
        return JSONResponse(
            {"error": "Topics can be manually pulled once every 30 minutes."},
            status_code=429,
            headers={"Retry-After": "1800"},
        )
    db.update_topic_pull(topic_id, posts_found=0, predictions_extracted=0)
    return JSONResponse({"pulled": True, "topic_id": topic_id})


async def api_topic_predictions(request: Request, topic_id: int):
    srv = _srv()
    user = srv._require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    preds = db.get_topic_predictions(topic_id)
    payload = {
        "predictions": [
            {"id": p["id"], "source_handle": p["source_handle"], "content": p["content"],
             "category": p["category"], "direction": p["direction"],
             "predicted_probability": p["predicted_probability"],
             "global_credibility": p["global_credibility"],
             "category_credibility": p["category_credibility"] if "category_credibility" in p.keys() else None,
             "accuracy_unlocked": bool(p["accuracy_unlocked"]) if p["accuracy_unlocked"] is not None else False}
            for p in preds
        ]
    }
    return JSONResponse(srv._forensic_sign(user, payload, "api_topic_predictions"))


async def api_topic_analysis(request: Request, topic_id: int):
    user = _srv()._require_pro_user(request)
    topic = db.get_topic(topic_id)
    if not topic or topic["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    analysis = db.get_latest_topic_analysis(topic_id)
    if not analysis:
        return JSONResponse({"analysis": None})
    return JSONResponse({
        "analysis": {
            "signal_direction": analysis["signal_direction"],
            "summary": analysis["summary"],
            "top_signals": _json.loads(analysis["top_signals"]) if analysis["top_signals"] else [],
            "contradictions": _json.loads(analysis["contradictions"]) if analysis["contradictions"] else [],
            "relevant_markets": _json.loads(analysis["relevant_markets"]) if analysis["relevant_markets"] else [],
            "confidence": analysis["confidence"],
            "confidence_reason": analysis["confidence_reason"],
            "generated_at": analysis["generated_at"],
        }
    })


def register(app) -> None:
    """Wire all topic routes into the given FastAPI app."""
    app.add_api_route("/api/topics", api_list_topics, methods=["GET"])
    app.add_api_route("/api/topics", api_create_topic, methods=["POST"])
    app.add_api_route("/api/topics/{topic_id}", api_delete_topic, methods=["DELETE"])
    app.add_api_route("/api/topics/{topic_id}/pull", api_topic_pull, methods=["POST"])
    app.add_api_route("/api/topics/{topic_id}/predictions", api_topic_predictions, methods=["GET"])
    app.add_api_route("/api/topics/{topic_id}/analysis", api_topic_analysis, methods=["GET"])
