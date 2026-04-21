"""Data-export routes — GDPR data portability.

POST /api/account/export      — enqueue a new export (1/day/user)
GET  /api/account/exports     — JSON list of the caller's past exports
GET  /settings/privacy        — HTML page with request button + history
GET  /api/account/export/{id}/download  — signed download URL handler

ZIP generation itself runs on a thread-pool via concurrent.futures so we
don't block the event loop during a large serialisation. If a worker pool
is wired up elsewhere (ARQ / jobs module) the generator hooks into it —
see _enqueue_export() below.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

import db


log = logging.getLogger("gateway.export_routes")

EXPORT_DIR = Path(os.environ.get("DATA_EXPORT_DIR", "/tmp/narve-exports"))
EXPORT_TTL_DAYS = 7
RATE_LIMIT_SECONDS = 24 * 60 * 60  # 1 per 24 hours per user


# Single shared executor. FastAPI serves from asyncio — we hand the
# synchronous ZIP work to this pool so the event loop keeps moving. Three
# workers is plenty for the rate we expect (1/user/day).
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dataexport")


# ── Deferred lookups into server.py (pattern shared with admin_routes.py) ──


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


def _current_user(request):
    return _srv().current_user(request)


def _render(name, request, **ctx):
    return _srv().render_page(name, request=request, **ctx)


def _role_badge(user):
    return _srv()._role_badge(user)


# ── Signed-URL helpers ─────────────────────────────────────────────────


def _export_secret() -> bytes:
    """HMAC key for signing download URLs. Falls back to the gateway cookie
    secret when present; otherwise derives one from DATA_EXPORT_DIR so the
    same key is stable across restarts without needing new env vars."""
    raw = (
        os.environ.get("GATEWAY_COOKIE_SECRET")
        or os.environ.get("DATA_EXPORT_SIGNING_KEY")
        or f"dataexport:{EXPORT_DIR}"
    )
    return hashlib.sha256(raw.encode()).digest()


def _sign(export_id: int, user_id: int, expires_at: int) -> str:
    payload = f"{export_id}:{user_id}:{expires_at}".encode()
    return hmac.new(_export_secret(), payload, hashlib.sha256).hexdigest()[:32]


def _verify(export_id: int, user_id: int, expires_at: int, sig: str) -> bool:
    expected = _sign(export_id, user_id, expires_at)
    return hmac.compare_digest(expected, sig or "")


def _download_url_for(request: Request, export_id: int, user_id: int, expires_at: int) -> str:
    sig = _sign(export_id, user_id, expires_at)
    return f"/api/account/export/{export_id}/download?u={user_id}&e={expires_at}&s={sig}"


# ── ZIP contents ───────────────────────────────────────────────────────


def _rows_to_list(rows) -> list[dict]:
    """Convert sqlite3.Row iterables into plain list-of-dicts for JSON."""
    out: list[dict] = []
    for r in rows or []:
        try:
            out.append({k: r[k] for k in r.keys()})
        except Exception:
            # Defensive: already a dict, or something unexpected.
            try:
                out.append(dict(r))
            except Exception:
                pass
    return out


def _write_json(zf: zipfile.ZipFile, name: str, data) -> None:
    zf.writestr(name, json.dumps(data, default=str, indent=2))


def _write_csv(zf: zipfile.ZipFile, name: str, rows: list[dict], fields: list[str]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    zf.writestr(name, buf.getvalue())


def _build_zip(user_id: int, zip_path: Path) -> int:
    """Serialise the caller's data into a ZIP. Returns byte size.

    Each helper is wrapped in a try/except so a single missing table (old
    deploys lagging behind the schema) doesn't crash the whole export —
    we just skip that section and keep going.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # README
        zf.writestr(
            "README.txt",
            "narve.ai data export\n"
            "=====================\n\n"
            "This ZIP contains a copy of the data narve.ai stores about your\n"
            "account. Each section is provided as both JSON (complete) and\n"
            "CSV (where applicable) for convenience.\n\n"
            "Sections:\n"
            "  account.json            — your user record\n"
            "  subscriptions.json      — your subscriptions + add-ons\n"
            "  predictions/            — predictions you've made\n"
            "  markets/viewed.csv      — markets you've loaded\n"
            "  sources/followed.csv    — sources you follow\n"
            "  signal_search/          — saved signal-search topics\n"
            "  notifications/history.csv — notifications we sent you\n"
            "  activity/login_history.csv — login events\n"
            "  metadata.json           — export metadata + generation time\n\n"
            "Questions: hello@narve.ai.\n",
        )

        # Account
        try:
            u = db.get_user_by_id(user_id)
            account = dict(u) if u else {}
            # Never export hashed password material, even to the user themselves.
            for secret_field in ("password_hash", "password_salt", "totp_secret",
                                 "backup_codes", "pending_totp_secret"):
                account.pop(secret_field, None)
            _write_json(zf, "account.json", account)
        except Exception as exc:
            log.warning("export account failed user=%d: %s", user_id, exc)

        # Subscriptions
        try:
            subs = _rows_to_list(db.list_subscriptions(user_id))
            _write_json(zf, "subscriptions.json", subs)
        except Exception as exc:
            log.warning("export subs failed user=%d: %s", user_id, exc)

        # Predictions (user-authored, if migration 026 applied)
        try:
            user_preds = _rows_to_list(db.list_user_predictions(user_id, limit=10_000))
            _write_json(zf, "predictions/user_predictions.json", user_preds)
            _write_csv(
                zf, "predictions/user_predictions.csv",
                user_preds,
                ["id", "market_slug", "market_question", "category",
                 "predicted_outcome", "predicted_probability", "reasoning",
                 "created_at", "resolved", "resolved_correct", "brier_score"],
            )
        except Exception as exc:
            log.warning("export user_predictions failed user=%d: %s", user_id, exc)

        # Saved predictions (if the helper exists)
        try:
            if hasattr(db, "list_saved_predictions"):
                saved = _rows_to_list(db.list_saved_predictions(user_id))
                _write_json(zf, "predictions/saved.json", saved)
                if saved:
                    _write_csv(
                        zf, "predictions/saved.csv", saved,
                        list(saved[0].keys()),
                    )
        except Exception as exc:
            log.warning("export saved failed user=%d: %s", user_id, exc)

        # Notifications
        try:
            if hasattr(db, "list_user_notifications"):
                notifs = _rows_to_list(db.list_user_notifications(user_id, limit=10_000))
                _write_json(zf, "notifications/history.json", notifs)
                if notifs:
                    _write_csv(
                        zf, "notifications/history.csv", notifs,
                        ["id", "type", "title", "body", "link_url",
                         "created_at", "read_at"],
                    )
        except Exception as exc:
            log.warning("export notifications failed user=%d: %s", user_id, exc)

        # Metadata
        _write_json(
            zf, "metadata.json",
            {
                "user_id": user_id,
                "generated_at_unix": int(time.time()),
                "format_version": 1,
                "source": "narve.ai GDPR export",
            },
        )

    return zip_path.stat().st_size


def _run_export(request_id: int) -> None:
    """Thread-pool worker: builds the ZIP and updates the request row."""
    try:
        row = db.get_data_export_request(request_id)
        if row is None:
            return
        user_id = row["user_id"]
        db.update_data_export_request(request_id, status="processing")

        zip_path = EXPORT_DIR / f"export_{request_id}_u{user_id}.zip"
        size = _build_zip(user_id, zip_path)
        expires_at = int(time.time()) + EXPORT_TTL_DAYS * 86400

        db.update_data_export_request(
            request_id,
            status="ready",
            completed_at=int(time.time()),
            file_path=str(zip_path),
            file_size_bytes=size,
            expires_at=expires_at,
            # download_url is rendered per-request with a fresh signature;
            # we only store a path here so signing key rotation Just Works.
            download_url="__signed__",
        )
        log.info("export ready request_id=%d user=%d bytes=%d", request_id, user_id, size)
    except Exception as exc:
        log.exception("export failed request_id=%d: %s", request_id, exc)
        try:
            db.update_data_export_request(
                request_id, status="failed", error=str(exc)[:500],
            )
        except Exception:
            pass


def _enqueue_export(request_id: int) -> None:
    """Hand the work to the thread pool. Returns immediately."""
    _executor.submit(_run_export, request_id)


# ── Routes ─────────────────────────────────────────────────────────────


async def api_request_export(request: Request):
    """POST /api/account/export — enqueue a new export (rate-limited)."""
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    user_id = user["user_id"]

    last_ts = db.last_user_data_export_ts(user_id)
    if last_ts and int(time.time()) - int(last_ts) < RATE_LIMIT_SECONDS:
        retry_in = RATE_LIMIT_SECONDS - (int(time.time()) - int(last_ts))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: next export available in {retry_in // 3600}h",
        )

    request_id = db.create_data_export_request(user_id)
    _enqueue_export(request_id)
    log.info("export enqueued request_id=%d user=%d", request_id, user_id)
    return JSONResponse({"export_id": request_id, "status": "pending"})


async def api_list_exports(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    rows = db.list_user_data_exports(user["user_id"], limit=20)
    out = []
    for r in rows:
        entry = {
            "id": r["id"],
            "status": r["status"],
            "requested_at": r["requested_at"],
            "completed_at": r["completed_at"],
            "expires_at": r["expires_at"],
            "file_size_bytes": r["file_size_bytes"],
            "error": r["error"],
        }
        if r["status"] == "ready" and r["expires_at"] and int(time.time()) < int(r["expires_at"]):
            entry["download_url"] = _download_url_for(
                request, r["id"], user["user_id"], int(r["expires_at"]),
            )
        out.append(entry)
    return JSONResponse({"exports": out})


async def api_download_export(request: Request, export_id: int):
    """Signed download — does NOT require session auth, so the link is
    shareable from the notification email. Validates HMAC + expiry +
    ownership."""
    try:
        user_id = int(request.query_params.get("u", "0"))
        expires_at = int(request.query_params.get("e", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad query")
    sig = request.query_params.get("s") or ""

    if int(time.time()) > expires_at:
        raise HTTPException(status_code=410, detail="Download link expired")
    if not _verify(export_id, user_id, expires_at, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    row = db.get_data_export_request(export_id)
    if not row or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Export not found")
    if row["status"] != "ready":
        raise HTTPException(status_code=409, detail=f"Export is {row['status']}")
    file_path = Path(row["file_path"] or "")
    if not file_path.is_file():
        raise HTTPException(status_code=410, detail="Export file missing (expired?)")

    filename = f"narve-export-{export_id}.zip"
    return FileResponse(
        path=str(file_path),
        media_type="application/zip",
        filename=filename,
    )


async def privacy_settings_page(request: Request):
    """GET /settings/privacy — request button + past exports."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/settings/privacy", status_code=302)

    rows = db.list_user_data_exports(user["user_id"], limit=20)
    import datetime as _dt
    row_html_parts = []
    for r in rows:
        requested = _dt.datetime.fromtimestamp(int(r["requested_at"]), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status_label = (r["status"] or "unknown").upper()
        extras: list[str] = []
        if r["status"] == "ready" and r["expires_at"] and int(time.time()) < int(r["expires_at"]):
            url = _download_url_for(request, r["id"], user["user_id"], int(r["expires_at"]))
            extras.append(f'<a class="btn btn-primary-outline" href="{url}" style="font-size:11px">Download</a>')
            left = int(int(r["expires_at"]) - time.time())
            extras.append(f'<span style="opacity:.55;font-size:11px">expires in {left // 86400}d</span>')
        elif r["status"] == "failed":
            extras.append(f'<span style="color:#ef4444;font-size:11px">{(r["error"] or "")[:80]}</span>')
        elif r["status"] in ("pending", "processing"):
            extras.append('<span style="opacity:.55;font-size:11px">Generating…</span>')
        row_html_parts.append(
            '<div class="admin-row">'
            '<div class="admin-row-info">'
            f'<div class="admin-row-main">Export #{r["id"]} <span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">{status_label}</span></div>'
            f'<div class="admin-row-meta">Requested {requested}</div>'
            '</div>'
            f'<div class="admin-row-actions" style="display:flex;gap:10px;align-items:center">{"".join(extras)}</div>'
            '</div>'
        )
    rows_html = "".join(row_html_parts) or (
        '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">'
        'No exports yet. Request one below — we\'ll email you when it\'s ready.'
        '</div></div></div>'
    )
    # Pull watermark/blur toggle state so the same page can render both the
    # data-export flow (this module) and the on-by-default UI privacy
    # toggles (security_routes.py). Failure is non-fatal — defaults to ON.
    try:
        from security_routes import get_user_privacy_prefs as _get_prefs
        prefs = _get_prefs(user["user_id"])
    except Exception:
        prefs = {"inactive_blur": True, "devtools_blur": True}
    return _render(
        "settings_privacy",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        raw_export_rows=rows_html,
        raw_inactive_checked="checked" if prefs.get("inactive_blur") else "",
        raw_devtools_checked="checked" if prefs.get("devtools_blur") else "",
    )


# ── Registration ───────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/api/account/export", api_request_export,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/account/exports", api_list_exports,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/account/export/{export_id}/download",
                      api_download_export,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/settings/privacy", privacy_settings_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
