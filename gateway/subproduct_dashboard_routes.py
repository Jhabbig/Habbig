"""Per-subproduct dashboard shells.

One route per subproduct renders its tabbed shell. The tab *content*
stays in the existing dashboard backends (reached via proxy_request
for legacy dashboards, or implemented later per-subproduct). This
module only defines:

  GET /dashboard/<slug>                  → tab shell + default tab
  GET /dashboard/<slug>/<tab>            → tab shell with ``tab`` selected
  GET /api/subproduct/<slug>/config      → JSON tab config (for the SPA)

The shell uses ``require_subproduct_access`` so non-entitled users get
a 402 that the SPA converts to a paywall modal. Apex narve.ai Pro users
(``_pro_or_better``) still have access.
"""

from __future__ import annotations

import html as _html
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("subproduct.dashboard")


# Tab layout per subproduct. First entry is the default when no tab is
# specified. Tab ids are lowercased, spaces become hyphens — matches
# what the frontend expects in the URL.
TABS: dict[str, list[str]] = {
    "sports":  ["Arbs", "Sports Feed", "Bookmakers", "Settings"],
    "weather": ["Mispricings", "Weather Feed", "Forecast Models", "Settings"],
    "world":   ["Conflicts", "Feed", "Markets", "Analysis", "Settings"],
    "crypto":  ["Signals", "Crypto Feed", "Ensemble", "Settings"],
    "midterm": ["Races", "Feed", "Polling", "Models", "Settings"],
    "traders": ["Leaderboard", "Wallet Activity", "Follow", "Settings"],
}


def _tab_slug(label: str) -> str:
    return label.lower().replace(" ", "-")


def _tabs_for(slug: str) -> list[dict]:
    return [
        {"id": _tab_slug(label), "label": label}
        for label in TABS.get(slug, ["Dashboard", "Settings"])
    ]


def _render_shell(request: Request, slug: str, tab_id: Optional[str]) -> HTMLResponse:
    from subproduct import SUBPRODUCTS
    cfg = SUBPRODUCTS.get(slug)
    if not cfg:
        raise HTTPException(status_code=404, detail="Unknown subproduct")

    tabs = _tabs_for(slug)
    default_tab = tabs[0]["id"] if tabs else "dashboard"
    active = tab_id or default_tab
    if active not in {t["id"] for t in tabs}:
        raise HTTPException(status_code=404, detail="Unknown tab")

    tabs_html = "".join(
        f'<a class="tab {"active" if t["id"] == active else ""}" '
        f'href="/dashboard/{_html.escape(slug)}/{_html.escape(t["id"])}">'
        f'{_html.escape(t["label"])}</a>'
        for t in tabs
    )

    name = _html.escape(cfg["name"])
    tagline = _html.escape(cfg.get("tagline", ""))
    slug_esc = _html.escape(slug)

    body = f"""<!DOCTYPE html><html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — narve.ai</title>
<link rel="stylesheet" href="/_gateway_static/gateway.css?v=5">
<style>
.narve-wordmark{{font-family:'Instrument Serif',serif;font-style:italic;font-size:18px}}
.narve-slash{{font-family:'Geist',sans-serif;font-weight:500;font-size:16px;margin:0 6px;color:var(--text-tertiary)}}
.narve-sub{{font-family:'Geist',sans-serif;font-weight:500;font-size:16px;color:var(--text-primary)}}
.topbar{{display:flex;align-items:center;gap:16px;padding:18px 28px;border-bottom:1px solid var(--border-default)}}
.tabs{{display:flex;gap:2px;padding:0 28px;border-bottom:1px solid var(--border-default)}}
.tab{{padding:12px 18px;font-size:13px;color:var(--text-secondary);text-decoration:none;border-bottom:2px solid transparent}}
.tab.active{{color:var(--text-primary);border-bottom-color:var(--text-primary)}}
#tabroot{{padding:28px}}
</style></head>
<body style="background:var(--bg-base);color:var(--text-primary);margin:0;font-family:var(--font-ui)">
<div class="topbar">
  <a href="https://narve.ai" style="text-decoration:none;color:inherit">
    <span class="narve-wordmark">narve.ai</span><span class="narve-slash">/</span><span class="narve-sub">{slug_esc}</span>
  </a>
  <span style="color:var(--text-tertiary);font-size:13px;margin-left:12px">{tagline}</span>
</div>
<nav class="tabs">{tabs_html}</nav>
<div id="tabroot" data-subproduct="{slug_esc}" data-tab="{_html.escape(active)}">
  <p style="color:var(--text-tertiary);font-size:13px">Loading…</p>
</div>
<script src="/_gateway_static/subproduct_dashboard.js?v=1" defer></script>
</body></html>"""
    return HTMLResponse(body)


def register(app) -> None:
    """Install the six dashboard routes + one config JSON route.

    Each handler composes two dependencies:
      1. The subproduct access check — 402 for non-entitled users.
      2. The shell renderer — 404 if the tab isn't in the layout.
    """
    from subproduct_access import require_subproduct_access

    # One route per subproduct. Duplicated bodies are fine: there are
    # only six, and a factory would tangle the FastAPI decorators.
    for _slug in ("sports", "weather", "world", "crypto", "midterm", "traders"):
        dep = require_subproduct_access(_slug)

        @app.get(f"/dashboard/{_slug}", response_class=HTMLResponse, dependencies=[Depends(dep)])  # noqa: B008
        async def _dashboard_root(request: Request, _s: str = _slug):
            return _render_shell(request, _s, None)

        @app.get(f"/dashboard/{_slug}/{{tab}}", response_class=HTMLResponse, dependencies=[Depends(dep)])  # noqa: B008
        async def _dashboard_tab(request: Request, tab: str, _s: str = _slug):
            return _render_shell(request, _s, tab)

    @app.get("/api/subproduct/{slug}/config")
    async def subproduct_config(slug: str):
        """Return JSON tab config for the SPA to render dynamically.

        Public (no auth) so a landing-page preview can show the tabs
        without pulling the visitor through the gate. Only reveals
        static UI copy — no user data.
        """
        from subproduct import SUBPRODUCTS
        cfg = SUBPRODUCTS.get(slug)
        if not cfg:
            raise HTTPException(status_code=404, detail="Unknown subproduct")
        return JSONResponse({
            "slug": slug,
            "name": cfg["name"],
            "tagline": cfg.get("tagline", ""),
            "tabs": _tabs_for(slug),
            "price_usd": cfg.get("price_usd"),
            "price_gbp": cfg.get("price_gbp"),
        })
