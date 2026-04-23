"""Routes for the forecast benchmark feature.

Three surfaces:

  1. ``GET /api/v1/forecasts/compare/{market_slug}`` — JSON payload for
     the "Forecast comparison" section on the market detail page. Reads
     the latest probability per provider + a time series for the chart.

  2. ``GET /dashboard/models`` — Pro-only HTML page. Aggregate picture
     of how each external provider's probability differs from narve's
     yes_price across all markets where we have both. True Brier vs
     resolved outcomes isn't wired yet (market resolution data lives
     in a different shape — see db.py predictions table). For now we
     report **cross-provider divergence**: average absolute gap from
     narve's probability over the window, which is a useful-enough
     calibration proxy and can be swapped for true Brier once resolved
     market outcomes are surfaced from the snapshot pipeline.

  3. ``GET /admin/equivalences`` + ``POST /admin/equivalences/...`` —
     review queue for low-confidence matches, plus unmatched active
     markets. Admin can approve / reject / override matches.

All routes are registered on ``server.app`` at import time, following
the pattern in ``affiliate_routes.py``. Wiring lives at the tail of
``server.py`` via the same reload-safe import block.
"""

from __future__ import annotations

import html as _html
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import db_forecasts
import server
from server import app, render_page, current_user, log as _root_log  # noqa: F401
from external_forecasts.base import PROVIDERS


log = logging.getLogger("forecast_routes")

# Tiny, render-time disclaimer shown on the market detail page and
# /dashboard/models. Kept in one place so admin + product can tweak
# the wording without hunting across templates.
DISCLAIMER = (
    "External forecasts retrieved from public APIs. narve.ai is not "
    "affiliated with Metaculus, Manifold, FiveThirtyEight, or Silver "
    "Bulletin. Data reflects best-effort matching and may not "
    "represent identical market definitions."
)


# ── /api/v1/forecasts/compare/{market_slug} ─────────────────────────


@app.get("/api/v1/forecasts/providers")
async def api_forecasts_providers(request: Request):
    """Static provider metadata — labels, dash patterns, upstream URLs.

    Single source of truth so the market detail page's chart legend
    doesn't hard-code labels or dash shapes in the template JS. Cheap
    (static dict), public (any authenticated user).

    ``dash`` is a Chart.js ``borderDash`` array (``[]`` = solid line).
    Kept deliberately short so the legend is readable on mobile.
    """
    payload = {
        "providers": [
            {
                "id": "narve",
                "label": "narve.ai",
                "dash": [],
                "homepage": "https://narve.ai",
            },
            {
                "id": "market",
                "label": "Market",
                "dash": [2, 2],
                "homepage": None,
            },
            {
                "id": "metaculus",
                "label": "Metaculus",
                "dash": [6, 4],
                "homepage": "https://www.metaculus.com",
            },
            {
                "id": "manifold",
                "label": "Manifold",
                "dash": [2, 6],
                "homepage": "https://manifold.markets",
            },
            {
                "id": "fivethirtyeight",
                "label": "538",
                "dash": [10, 3, 2, 3],
                "homepage": "https://projects.fivethirtyeight.com",
            },
            {
                "id": "silver_bulletin",
                "label": "Silver Bulletin",
                "dash": [4, 2, 4, 6],
                "homepage": "https://www.natesilver.net",
            },
        ],
    }
    return JSONResponse(payload)


@app.get("/api/v1/forecasts/compare/{market_slug}")
async def api_forecasts_compare(
    market_slug: str, request: Request, window: str = "30d",
):
    """Latest + time-series per provider for a single market.

    Public — any signed-in user can hit this endpoint. The market
    detail page gates on Pro subscription at its own render layer.

    Query:
      ``window``: ``7d`` | ``30d`` | ``all`` — caps the series length.
    """
    slug = (market_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="market_slug required")

    since_ts: Optional[int] = None
    if window == "7d":
        since_ts = int(time.time()) - 7 * 86400
    elif window == "30d":
        since_ts = int(time.time()) - 30 * 86400
    elif window not in ("all", "", None):
        raise HTTPException(status_code=400, detail="window must be 7d, 30d, or all")

    latest = db_forecasts.latest_forecast_per_provider(slug)
    series = db_forecasts.forecast_time_series(slug, since_ts=since_ts)

    # Also expose our own yes_price series in the same shape so the
    # chart client only needs to iterate one data structure.
    narve_series = _narve_series(slug, since_ts=since_ts)

    return JSONResponse({
        "market_slug": slug,
        "window": window,
        "disclaimer": DISCLAIMER,
        "providers": list(PROVIDERS),
        "latest": latest,
        "series": series,
        "narve_series": narve_series,
    })


def _narve_series(slug: str, since_ts: Optional[int]) -> list[dict]:
    """narve's own yes_price history for the chart — same shape as the
    external series (provider='narve', probability, recorded_at).
    Read directly from market_snapshots rather than through a helper
    because this is the only route that needs exactly this shape."""
    with db.conn() as c:
        if since_ts is None:
            rows = c.execute(
                "SELECT yes_price, snapshotted_at "
                "FROM market_snapshots WHERE market_slug = ? "
                "ORDER BY snapshotted_at ASC",
                (slug,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT yes_price, snapshotted_at "
                "FROM market_snapshots "
                "WHERE market_slug = ? AND snapshotted_at >= ? "
                "ORDER BY snapshotted_at ASC",
                (slug, int(since_ts)),
            ).fetchall()
    return [
        {
            "provider": "narve",
            "probability": float(r["yes_price"]),
            "recorded_at": int(r["snapshotted_at"]),
        }
        for r in rows
    ]


# ── /dashboard/models (Pro-only) ────────────────────────────────────


def _user_is_pro(user: dict) -> bool:
    """Any active subscription counts as Pro for model dashboard access.
    Keeps the gate consistent with how the rest of the Pro-only pages
    check — via db.get_active_subscriptions reading back non-empty."""
    try:
        subs = db.get_active_subscriptions(user["user_id"])
        return bool(subs)
    except Exception:
        return bool(user.get("is_admin"))


@app.get("/dashboard/models", response_class=HTMLResponse)
async def dashboard_models(request: Request):
    user = current_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/token", status_code=302)
    if not _user_is_pro(user) and not user.get("is_admin"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/billing?upgrade=models", status_code=302)

    summary = _compute_divergence_summary()

    rows_html = _render_divergence_rows(summary["per_provider"])

    return render_page(
        "dashboard_models",
        request=request,
        username=user.get("username") or user.get("email", ""),
        raw_nav_role="",
        raw_admin_link=(
            '<a href="/admin" class="nav-item">Admin</a>' if user.get("is_admin") else ""
        ),
        total_markets=summary["total_markets"],
        total_snapshots=summary["total_snapshots"],
        disclaimer=DISCLAIMER,
        raw_divergence_rows=rows_html,
        window_days=30,
    )


def _compute_divergence_summary() -> dict:
    """For each provider: how much does its probability diverge from
    narve's yes_price, on average, across all markets where we have
    both? Computed over the last 30 days of snapshots."""
    since_ts = int(time.time()) - 30 * 86400
    # Pull narve baselines keyed by slug → sorted [(ts, p)].
    with db.conn() as c:
        narve_rows = c.execute(
            "SELECT market_slug, snapshotted_at, yes_price "
            "FROM market_snapshots "
            "WHERE snapshotted_at >= ? "
            "ORDER BY market_slug, snapshotted_at ASC",
            (since_ts,),
        ).fetchall()
    narve: dict[str, list[tuple[int, float]]] = {}
    for r in narve_rows:
        narve.setdefault(r["market_slug"], []).append(
            (int(r["snapshotted_at"]), float(r["yes_price"]))
        )

    per_provider: dict[str, dict] = {}
    total_snapshots = 0
    markets_seen: set[str] = set()

    for provider in PROVIDERS:
        rows = db_forecasts.provider_series_for_scoring(provider, since_ts=since_ts)
        if not rows:
            per_provider[provider] = {
                "samples": 0, "markets": 0,
                "avg_divergence": None, "max_divergence": None,
            }
            continue
        abs_diffs: list[float] = []
        max_diff = 0.0
        markets_for_provider: set[str] = set()
        for row in rows:
            slug = row["market_slug"]
            their_p = float(row["probability"])
            narve_p = _narve_prob_at(narve.get(slug) or [], int(row["recorded_at"]))
            if narve_p is None:
                continue
            diff = abs(their_p - narve_p)
            abs_diffs.append(diff)
            max_diff = max(max_diff, diff)
            markets_for_provider.add(slug)
            markets_seen.add(slug)
        total_snapshots += len(abs_diffs)
        per_provider[provider] = {
            "samples": len(abs_diffs),
            "markets": len(markets_for_provider),
            "avg_divergence": sum(abs_diffs) / len(abs_diffs) if abs_diffs else None,
            "max_divergence": max_diff if abs_diffs else None,
        }

    return {
        "per_provider": per_provider,
        "total_markets": len(markets_seen),
        "total_snapshots": total_snapshots,
    }


def _narve_prob_at(
    narve_series: list[tuple[int, float]], ts: int
) -> Optional[float]:
    """Binary-search-ish: find narve's probability closest in time to
    the external snapshot. A straight linear scan is fine at 30-day
    scale — typical markets have a few hundred snapshots."""
    if not narve_series:
        return None
    best = None
    best_gap = None
    for (nts, p) in narve_series:
        gap = abs(nts - ts)
        if best_gap is None or gap < best_gap:
            best, best_gap = p, gap
    return best


def _render_divergence_rows(per_provider: dict[str, dict]) -> str:
    """Produce the <tr> rows for the Pro dashboard table. Rendered
    server-side because the dataset is small (4 rows) and doing it
    in Python keeps the template free of template-logic."""
    rows: list[str] = []
    for provider, stats in per_provider.items():
        if stats["samples"] == 0:
            avg = "—"
            mx = "—"
        else:
            avg = f"{100 * stats['avg_divergence']:.1f}pp"
            mx = f"{100 * stats['max_divergence']:.1f}pp"
        rows.append(
            "<tr>"
            f"<td>{_html.escape(provider)}</td>"
            f"<td class='num'>{stats['markets']}</td>"
            f"<td class='num'>{stats['samples']}</td>"
            f"<td class='num'>{avg}</td>"
            f"<td class='num'>{mx}</td>"
            "</tr>"
        )
    return "".join(rows) or (
        "<tr><td colspan='5' class='empty-cell'>"
        "No external forecast data yet. The nightly sync populates this table.</td></tr>"
    )


# ── /admin/equivalences ─────────────────────────────────────────────


@app.get("/admin/equivalences", response_class=HTMLResponse)
async def admin_equivalences(request: Request):
    user = server._require_admin_user(request, page=True)
    if not user or not isinstance(user, dict):
        # _require_admin_user returned a RedirectResponse or None
        from fastapi.responses import RedirectResponse
        if user is None:
            return RedirectResponse("/token", status_code=303)
        return user

    summary = db_forecasts.equivalence_summary()
    low_conf = db_forecasts.list_low_confidence_equivalences(limit=200)
    unmatched = db_forecasts.list_unmatched_active_markets(limit=200)

    return render_page(
        "admin_equivalences",
        request=request,
        username=user.get("username") or user.get("email", ""),
        raw_nav_role="",
        total_mappings=summary["total"],
        admin_overrides=summary["admin_overrides"],
        low_confidence_count=summary["low_confidence"],
        rejected_count=summary["rejected"],
        unmatched_count=len(unmatched),
        raw_low_conf_rows=_render_low_conf_rows(low_conf),
        raw_unmatched_rows=_render_unmatched_rows(unmatched),
    )


def _render_low_conf_rows(rows) -> str:
    out: list[str] = []
    for r in rows:
        slug_e = _html.escape(r["market_slug"])
        provider_e = _html.escape(r["provider"])
        out.append(
            "<tr>"
            f"<td><code>{slug_e}</code></td>"
            f"<td>{provider_e}</td>"
            f"<td><code>{_html.escape(r['provider_market_id'] or '')}</code></td>"
            f"<td>{_html.escape((r['provider_question'] or '')[:100])}</td>"
            f"<td class='num'>{float(r['confidence']):.2f}</td>"
            f"<td class='actions'>"
            f"  <button class='btn-danger' "
            f"    data-action='approve' "
            f"    data-slug='{slug_e}' data-provider='{provider_e}'>Approve</button>"
            f"  <button class='btn-danger' "
            f"    data-action='override' "
            f"    data-slug='{slug_e}' data-provider='{provider_e}'>Override</button>"
            f"  <button class='btn-danger' "
            f"    data-action='reject' "
            f"    data-slug='{slug_e}' data-provider='{provider_e}'>Reject</button>"
            f"</td>"
            "</tr>"
        )
    return "".join(out) or (
        "<tr><td colspan='6' class='empty-cell'>"
        "No low-confidence matches pending.</td></tr>"
    )


def _render_unmatched_rows(rows) -> str:
    out: list[str] = []
    for r in rows:
        out.append(
            "<tr>"
            f"<td><code>{_html.escape(r['market_slug'])}</code></td>"
            f"<td>{_html.escape((r['market_question'] or '')[:140])}</td>"
            f"<td>{_html.escape(r['category'] or '')}</td>"
            "</tr>"
        )
    return "".join(out) or (
        "<tr><td colspan='3' class='empty-cell'>"
        "Every active market has at least one equivalence cached.</td></tr>"
    )


@app.post("/admin/equivalences/{market_slug}/{provider}")
async def admin_equivalence_action(
    market_slug: str, provider: str, request: Request,
):
    user = server._require_admin_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    action = (body.get("action") or "").strip().lower()
    if action == "approve":
        existing = db_forecasts.get_equivalence(market_slug, provider)
        if not existing:
            raise HTTPException(status_code=404, detail="No pending equivalence")
        db_forecasts.upsert_equivalence(
            market_slug=market_slug,
            provider=provider,
            provider_market_id=existing["provider_market_id"],
            provider_question=existing["provider_question"],
            confidence=1.0,
            mapped_by="admin_override",
        )
        return JSONResponse({"ok": True, "mapped_by": "admin_override"})

    if action == "reject":
        ok = db_forecasts.mark_equivalence_rejected(market_slug, provider)
        if not ok:
            raise HTTPException(status_code=404, detail="No equivalence to reject")
        return JSONResponse({"ok": True, "rejected": True})

    if action == "override":
        target_id = (body.get("provider_market_id") or "").strip()
        if not target_id:
            raise HTTPException(status_code=400, detail="provider_market_id required")
        db_forecasts.upsert_equivalence(
            market_slug=market_slug,
            provider=provider,
            provider_market_id=target_id,
            provider_question=body.get("provider_question"),
            confidence=1.0,
            mapped_by="admin_override",
        )
        return JSONResponse({"ok": True, "mapped_by": "admin_override"})

    raise HTTPException(
        status_code=400,
        detail="action must be approve, reject, or override",
    )
