"""Scenario-tool routes — Pro-gated conditional-probability explorer.

  GET  /tools/scenario                HTML picker + table shell
  GET  /tools/correlations            HTML matrix heatmap (top-30 active)
  GET  /api/scenario/markets          active-market picker source
  GET  /api/scenario/correlations     anchor_slug → correlated markets JSON
  POST /api/scenario/compute          run scenario (anchor, yes/no)
  POST /api/scenario/save             persist a scenario for the user
  GET  /api/scenario/saved            list the user's saved scenarios
  GET  /api/scenario/heatmap          top-N × top-N correlation matrix

Wire via ``scenarios_routes.register(app)``. No server.py or db.py edits.
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scenarios.correlation import compute_market_correlations, pearson, align_snapshot_series, deltas
from scenarios.scenario import compute_scenario, DISCLAIMER


log = logging.getLogger("scenarios_routes")


MATRIX_MARKET_CAP = 30


# ── DB ──────────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


# ── Auth / Pro gate ─────────────────────────────────────────────────────────


def _require_pro_user(request: Request) -> dict:
    """Lazy-import server helpers so the route module can be loaded in tests
    without booting the full FastAPI app.
    """
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")

    # Pro/enterprise/admin passes. Use the canonical _user_plan_info when
    # it exists; otherwise accept any active subscription as a Pro proxy
    # (the gate is cosmetic — the feature still works without the check).
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user["user_id"],),
            ).fetchall()
            subs = {r["dashboard_key"]: dict(r) for r in rows}
        finally:
            conn.close()
        plan = server._user_plan_info(user, subs, int(time.time())).get("plan") or "none"
    except Exception:
        plan = "none"

    if plan not in ("pro", "enterprise") and not user.get("is_admin"):
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


# ── API: market picker ─────────────────────────────────────────────────────


async def api_markets(request: Request):
    """Search endpoint for the anchor picker. Returns active markets
    (snapshot within last 48h), filtered by ``q``."""
    _require_pro_user(request)
    q = (request.query_params.get("q") or "").strip().lower()
    try:
        limit = max(1, min(100, int(request.query_params.get("limit") or "50")))
    except ValueError:
        limit = 50

    conn = _connect()
    try:
        if not _table_exists(conn, "market_snapshots"):
            return JSONResponse({"markets": []})
        since = int(time.time()) - 48 * 3600
        rows = conn.execute(
            """
            SELECT market_slug, market_question, category, yes_price, MAX(snapshotted_at) AS ts
            FROM market_snapshots
            WHERE snapshotted_at >= ?
            GROUP BY market_slug
            ORDER BY ts DESC
            LIMIT 500
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        question = (r["market_question"] or r["market_slug"] or "").strip()
        if q and q not in question.lower() and q not in (r["market_slug"] or "").lower():
            continue
        out.append({
            "slug": r["market_slug"],
            "question": question,
            "category": r["category"],
            "current_price": round(float(r["yes_price"]), 4) if r["yes_price"] is not None else None,
        })
        if len(out) >= limit:
            break
    return JSONResponse({"markets": out})


# ── API: correlations + scenario ───────────────────────────────────────────


async def api_correlations(request: Request, anchor_slug: str):
    _require_pro_user(request)
    try:
        days = max(7, min(365, int(request.query_params.get("days") or "90")))
    except ValueError:
        days = 90
    try:
        min_abs = float(request.query_params.get("min_abs") or "0.25")
    except ValueError:
        min_abs = 0.25
    min_abs = max(0.0, min(1.0, min_abs))
    correlations = await compute_market_correlations(
        anchor_slug, days=days, min_abs=min_abs, limit=50,
    )
    return JSONResponse({
        "anchor_slug": anchor_slug, "days": days, "min_abs": min_abs,
        "correlations": correlations,
        "disclaimer": DISCLAIMER,
    })


async def api_compute_scenario(
    request: Request,
    anchor_slug: str = Form(...),
    hypothetical: str = Form(...),
    anchor_current_price: Optional[float] = Form(None),
):
    _require_pro_user(request)
    result = await compute_scenario(
        anchor_slug, hypothetical,
        anchor_current_price=anchor_current_price,
    )
    return JSONResponse(result)


# ── API: save / list scenarios ──────────────────────────────────────────────


def _ensure_scenario_saves_table(conn: sqlite3.Connection) -> None:
    # Dedicated table — simpler than reusing saved_views, which has a
    # wide schema we don't need. Safe to create on-demand because the
    # table is additive and unreferenced by any other module.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scenario_saves (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            anchor_slug   TEXT NOT NULL,
            hypothetical  TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            created_at    INTEGER NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scenario_saves_user "
        "ON scenario_saves(user_id, created_at DESC)"
    )


async def api_save_scenario(
    request: Request,
    anchor_slug: str = Form(...),
    hypothetical: str = Form(...),
):
    user = _require_pro_user(request)
    result = await compute_scenario(anchor_slug, hypothetical)
    conn = _connect()
    try:
        _ensure_scenario_saves_table(conn)
        cur = conn.execute(
            "INSERT INTO scenario_saves "
            "(user_id, anchor_slug, hypothetical, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (user["user_id"], anchor_slug, hypothetical,
             json.dumps(result), int(time.time())),
        )
        conn.commit()
        save_id = cur.lastrowid
    finally:
        conn.close()
    return JSONResponse({"saved_id": save_id, "scenario": result}, status_code=201)


async def api_list_saved(request: Request):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        _ensure_scenario_saves_table(conn)
        rows = conn.execute(
            "SELECT id, anchor_slug, hypothetical, created_at "
            "FROM scenario_saves WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 50",
            (user["user_id"],),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"saved": [dict(r) for r in rows]})


# ── API: heatmap ────────────────────────────────────────────────────────────


async def api_heatmap(request: Request):
    """Top-30 active markets × themselves, Pearson r on hourly deltas.

    Cached per (top_n, days) key so the heatmap doesn't recompute the
    ~900-cell matrix on every page load.
    """
    _require_pro_user(request)
    try:
        top_n = max(5, min(MATRIX_MARKET_CAP, int(request.query_params.get("top") or "30")))
    except ValueError:
        top_n = 30
    try:
        days = max(7, min(365, int(request.query_params.get("days") or "90")))
    except ValueError:
        days = 90

    cache_key = f"scenario:heatmap:{top_n}:{days}"
    try:
        from cache.ttl import ttl_cache
        cached = ttl_cache.get(cache_key)
        if cached is not None:
            return JSONResponse(cached)
    except Exception:
        ttl_cache = None  # type: ignore

    since_ts = int(time.time()) - days * 86400
    conn = _connect()
    try:
        if not _table_exists(conn, "market_snapshots"):
            payload = {"markets": [], "matrix": [], "disclaimer": DISCLAIMER}
            return JSONResponse(payload)
        rows = conn.execute(
            """
            SELECT market_slug, COUNT(*) AS n,
                   MAX(snapshotted_at) AS last_ts,
                   MAX(market_question) AS question,
                   MAX(category) AS category
            FROM market_snapshots
            WHERE snapshotted_at >= ?
            GROUP BY market_slug
            HAVING n >= 5
            ORDER BY n DESC
            LIMIT ?
            """,
            (since_ts, top_n),
        ).fetchall()
        top = [dict(r) for r in rows]

        histories: dict[str, list[tuple[int, float]]] = {}
        for r in top:
            hist = conn.execute(
                "SELECT snapshotted_at AS ts, yes_price AS y FROM market_snapshots "
                "WHERE market_slug = ? AND snapshotted_at >= ? "
                "ORDER BY snapshotted_at ASC",
                (r["market_slug"], since_ts),
            ).fetchall()
            histories[r["market_slug"]] = [
                (int(h["ts"]), float(h["y"])) for h in hist if h["y"] is not None
            ]
    finally:
        conn.close()

    slugs = [r["market_slug"] for r in top]
    matrix: list[list[Optional[float]]] = []
    for i, a in enumerate(slugs):
        row_r: list[Optional[float]] = []
        a_hist = histories.get(a, [])
        for j, b in enumerate(slugs):
            if i == j:
                row_r.append(1.0)
                continue
            b_hist = histories.get(b, [])
            xs, ys = align_snapshot_series(a_hist, b_hist)
            if len(xs) < 4:
                row_r.append(None)
                continue
            r = pearson(deltas(xs), deltas(ys))
            row_r.append(round(r, 4) if r is not None else None)
        matrix.append(row_r)

    payload = {
        "markets": [
            {"slug": r["market_slug"],
             "question": r["question"] or r["market_slug"],
             "category": r["category"]}
            for r in top
        ],
        "matrix": matrix,
        "days": days,
        "disclaimer": DISCLAIMER,
    }

    try:
        from cache.ttl import ttl_cache
        ttl_cache.set(cache_key, payload, ttl_seconds=86400)
    except Exception:
        pass
    return JSONResponse(payload)


# ── HTML shells ─────────────────────────────────────────────────────────────


def _common_styles() -> str:
    # Monochrome styling that matches the existing admin/Pro shells.
    return """body{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:1080px;margin:0 auto}
h1{font-family:var(--font-display);font-style:italic;font-size:48px;
letter-spacing:-0.02em;margin:0 0 12px}
.meta{color:var(--text-tertiary);font-size:12px;letter-spacing:0.08em;
text-transform:uppercase;margin-bottom:28px}
.panel{background:var(--bg-surface);border:1px solid var(--border-default);
border-radius:10px;padding:20px 22px;margin-bottom:16px}
.btn{padding:10px 16px;background:var(--text-primary);color:var(--interactive-text);
border-radius:8px;border:none;font:500 13px var(--font-ui);cursor:pointer}
.btn.secondary{background:transparent;color:var(--text-primary);
border:1px solid var(--border-default)}
input[type=search],input[type=text],select{
  width:100%;padding:10px 12px;background:var(--bg-surface);
  border:1px solid var(--border-default);color:var(--text-primary);
  border-radius:8px;font-family:inherit;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--text-secondary);font-size:11px;
text-transform:uppercase;letter-spacing:0.08em;padding:10px 12px;
background:var(--bg-surface)}
td{padding:10px 12px;border-top:1px solid var(--border-default)}
.r{text-align:right}
.mono{font-family:var(--font-mono);font-size:12px}
.disclaimer{font-size:11px;color:var(--text-tertiary);
border-top:1px solid var(--border-default);padding-top:14px;margin-top:32px}
.shift-up{color:var(--green)}.shift-down{color:var(--red)}
.small{font-size:11px;color:var(--text-tertiary)}"""


async def scenario_page(request: Request):
    user = _require_pro_user(request)
    body = f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'><title>Scenario calculator — narve.ai</title>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>{_common_styles()}
.radio-row{{display:flex;gap:16px;margin-top:10px}}
.radio-row label{{display:flex;gap:8px;align-items:center;cursor:pointer;
min-height:44px}}
.picker-results{{max-height:260px;overflow-y:auto;margin-top:8px;
border:1px solid var(--border-default);border-radius:8px}}
.picker-row{{padding:12px;cursor:pointer;border-bottom:1px solid var(--border-default);
min-height:44px}}
.picker-row:hover,.picker-row:focus{{background:var(--bg-surface);outline:none}}
.picker-row[aria-selected=true]{{background:var(--bg-raised);
border-left:3px solid var(--text-primary);padding-left:9px}}
.picker-row:last-child{{border-bottom:0}}
.btn:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,
.picker-row:focus-visible{{outline:2px solid var(--text-primary);outline-offset:2px}}
</style></head><body>
<h1>Scenario calculator</h1>
<p class='meta'>If market X resolves a certain way, which of my markets move?</p>

<div class='panel'>
  <label class='small' for='pick'>Pick an anchor market</label>
  <input id='pick' type='search' placeholder='Search active markets…'
         autocomplete='off' aria-autocomplete='list' aria-controls='picker-results'
         aria-expanded='false'>
  <div class='picker-results' id='picker-results' role='listbox' hidden></div>
  <div id='selected' style='margin-top:14px' hidden aria-live='polite'>
    <strong id='sel-q'></strong>
    <span class='small'>current: <span id='sel-p'></span></span>
  </div>
</div>

<div class='panel' id='outcome-panel' hidden>
  <fieldset style='border:0;padding:0;margin:0'>
    <legend class='small' style='padding:0;margin-bottom:6px'>Hypothetical outcome</legend>
    <div class='radio-row' role='radiogroup' aria-label='Hypothetical outcome'>
      <label><input type='radio' name='h' value='yes'> Resolves YES</label>
      <label><input type='radio' name='h' value='no' checked> Resolves NO</label>
    </div>
  </fieldset>
  <div style='margin-top:14px;display:flex;gap:10px;flex-wrap:wrap'>
    <button class='btn' id='run-btn' type='button'>Run scenario</button>
    <button class='btn secondary' id='save-btn' type='button' disabled
            aria-label='Save this scenario to your list'>Save scenario</button>
    <a class='btn secondary' href='/tools/correlations'>Correlation matrix →</a>
  </div>
</div>

<div id='results' class='panel' hidden>
  <h3 style='margin:0 0 12px;font:500 16px var(--font-display)'>Correlated markets</h3>
  <table><thead><tr>
    <th>Market</th><th class='r'>Corr</th><th class='r'>Expected shift</th>
  </tr></thead><tbody id='results-tbody'></tbody></table>
  <p class='disclaimer'>{DISCLAIMER}</p>
</div>

<script>
(function(){{
let selectedSlug=null, selectedPrice=null;
const pick=document.getElementById('pick');
const pickerResults=document.getElementById('picker-results');
const selected=document.getElementById('selected');
const outcomePanel=document.getElementById('outcome-panel');
const results=document.getElementById('results');

async function search(q){{
  const url='/api/scenario/markets?q='+encodeURIComponent(q);
  const r=await fetch(url,{{credentials:'same-origin'}});
  if(!r.ok){{pickerResults.hidden=true;return}}
  const data=await r.json();
  pickerResults.innerHTML=(data.markets||[]).slice(0,40).map(m=>(
    '<div class=\"picker-row\" data-slug=\"'+m.slug+'\" data-price=\"'+(m.current_price||'')+'\">'
    +'<div>'+escape(m.question||m.slug)+'</div>'
    +'<div class=\"small\">'+escape(m.category||'')+' · '+(m.current_price!=null?Math.round(m.current_price*100)+'% YES':'')+'</div>'
    +'</div>'
  )).join('');
  pickerResults.hidden=data.markets.length===0;
}}

function escape(s){{return (s||'').replace(/[&<>\"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}}[c]))}}

pick.addEventListener('input',()=>{{if(pick.value.length>1) search(pick.value); else pickerResults.hidden=true;}});
pickerResults.addEventListener('click',e=>{{
  const row=e.target.closest('.picker-row'); if(!row) return;
  selectedSlug=row.dataset.slug;
  selectedPrice=parseFloat(row.dataset.price)||null;
  document.getElementById('sel-q').textContent=row.querySelector('div').textContent;
  document.getElementById('sel-p').textContent=selectedPrice!=null?Math.round(selectedPrice*100)+'% YES':'—';
  selected.hidden=false;
  outcomePanel.hidden=false;
  pickerResults.hidden=true;
  pick.value='';
}});

document.getElementById('run-btn').addEventListener('click',async()=>{{
  if(!selectedSlug) return;
  const h=document.querySelector('input[name=h]:checked').value;
  const fd=new FormData(); fd.append('anchor_slug',selectedSlug); fd.append('hypothetical',h);
  if(selectedPrice!=null) fd.append('anchor_current_price',selectedPrice);
  const r=await fetch('/api/scenario/compute',{{method:'POST',body:fd,credentials:'same-origin'}});
  const data=await r.json();
  const tbody=document.getElementById('results-tbody');
  if(data.error){{tbody.innerHTML='<tr><td colspan=\"3\">'+escape(data.error)+'</td></tr>';results.hidden=false;return;}}
  const shifts=data.shifts||[];
  if(!shifts.length){{
    tbody.innerHTML='<tr><td colspan=\"3\" class=\"small\">No significant correlations above the 0.25 threshold.</td></tr>';
  }} else {{
    tbody.innerHTML=shifts.slice(0,30).map(s=>{{
      const dir=s.expected_shift>=0?'shift-up':'shift-down';
      const arrow=s.expected_shift>=0?'↑':'↓';
      const sign=s.expected_shift>=0?'+':'';
      return '<tr><td>'+escape(s.question)+'<div class=\"small\">'+escape(s.category||'')+'</div></td>'
           +'<td class=\"r mono\">'+(s.correlation>=0?'+':'')+s.correlation.toFixed(2)+'</td>'
           +'<td class=\"r mono '+dir+'\">'+arrow+' '+sign+Math.round(s.expected_shift*100)+'pp (to '+Math.round(s.projected_price*100)+'%)</td></tr>';
    }}).join('');
  }}
  results.hidden=false;
  document.getElementById('save-btn').disabled=false;
}});

document.getElementById('save-btn').addEventListener('click',async()=>{{
  if(!selectedSlug) return;
  const h=document.querySelector('input[name=h]:checked').value;
  const fd=new FormData(); fd.append('anchor_slug',selectedSlug); fd.append('hypothetical',h);
  const r=await fetch('/api/scenario/save',{{method:'POST',body:fd,credentials:'same-origin'}});
  if(r.ok){{document.getElementById('save-btn').textContent='Saved ✓';document.getElementById('save-btn').disabled=true;}}
}});
}})();
</script>
</body></html>"""
    return HTMLResponse(body)


async def correlation_matrix_page(request: Request):
    _require_pro_user(request)
    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Correlation matrix — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>{_common_styles()}
.heatmap{{overflow:auto;border:1px solid var(--border-default);
border-radius:8px;padding:8px;background:var(--bg-base)}}
.heatmap table{{border-collapse:collapse;font-size:11px}}
.heatmap th,.heatmap td{{border:1px solid var(--bg-base);padding:0;
width:22px;height:22px;text-align:center;vertical-align:middle;
font-family:var(--font-mono);font-size:10px}}
.heatmap th.row-label{{text-align:right;padding:0 6px;font-weight:400;
color:var(--text-secondary);white-space:nowrap;max-width:180px;
overflow:hidden;text-overflow:ellipsis}}
.heatmap th.col-label{{writing-mode:vertical-rl;transform:rotate(180deg);
height:120px;color:var(--text-secondary);font-weight:400}}
.cell{{cursor:pointer;color:transparent}}
.cell:hover{{outline:2px solid var(--text-primary);outline-offset:-2px;
color:var(--text-primary)}}
.legend{{display:flex;align-items:center;gap:12px;margin:12px 0 22px;font-size:12px}}
.legend-scale{{display:flex;gap:0;height:12px;border:1px solid var(--border-default)}}
.legend-scale span{{width:16px;height:100%}}
</style></head><body>
<h1>Correlation matrix</h1>
<p class='meta'>Top 30 active markets · 90-day hourly deltas</p>

<div class='legend'>
  <span class='small'>−1</span>
  <div class='legend-scale' id='legend-scale'></div>
  <span class='small'>+1</span>
  <span class='small' style='margin-left:auto'>Click a cell to open the pair</span>
</div>

<div id='heatmap' class='heatmap'>Loading…</div>
<p class='disclaimer'>{DISCLAIMER}</p>

<script>
(function(){{
// Monochrome scale: negative = dashed lighter, positive = solid darker.
// Using grayscale so the visual stays consistent with the narve.ai palette.
function colorFor(r){{
  if(r==null) return 'transparent';
  const abs=Math.min(1,Math.abs(r));
  // Map to [255 (white), 30 (near black)]
  const gray=Math.round(255-(abs*225));
  return 'rgb('+gray+','+gray+','+gray+')';
}}
const legend=document.getElementById('legend-scale');
const steps=[-1,-0.75,-0.5,-0.25,0,0.25,0.5,0.75,1];
legend.innerHTML=steps.map(s=>'<span style=\"background:'+colorFor(s)+'\"></span>').join('');

fetch('/api/scenario/heatmap?top=30&days=90',{{credentials:'same-origin'}})
  .then(r=>r.json()).then(data=>{{
    const root=document.getElementById('heatmap');
    const markets=data.markets||[]; const matrix=data.matrix||[];
    if(!markets.length){{root.innerHTML='<p class=\"small\" style=\"padding:16px\">Not enough market snapshots to build a matrix yet.</p>';return;}}
    let html='<table><thead><tr><th></th>';
    markets.forEach(m=>html+='<th class=\"col-label\">'+escapeHtml(trunc(m.question,36))+'</th>');
    html+='</tr></thead><tbody>';
    matrix.forEach((row,i)=>{{
      html+='<tr><th class=\"row-label\">'+escapeHtml(trunc(markets[i].question,36))+'</th>';
      row.forEach((r,j)=>{{
        const color=colorFor(r);
        const text=r==null?'—':(Math.round(r*100)/100).toFixed(2);
        html+='<td class=\"cell\" style=\"background:'+color+'\" title=\"'+escapeHtml(markets[i].question)+' × '+escapeHtml(markets[j].question)+' = '+text+'\" data-a=\"'+escapeHtml(markets[i].slug)+'\">'+text+'</td>';
      }});
      html+='</tr>';
    }});
    html+='</tbody></table>';
    root.innerHTML=html;
    root.addEventListener('click',e=>{{
      const c=e.target.closest('.cell'); if(!c) return;
      const slug=c.getAttribute('data-a');
      window.location.href='/tools/scenario#'+encodeURIComponent(slug);
    }});
  }}).catch(()=>{{document.getElementById('heatmap').innerHTML='<p class=\"small\">Matrix load failed.</p>';}});

function trunc(s,n){{s=s||'';return s.length>n?s.slice(0,n-1)+'…':s}}
function escapeHtml(s){{return (s||'').replace(/[&<>\"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}}[c]))}}
}})();
</script>
</body></html>"""
    return HTMLResponse(body)


# ── Registration ────────────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/tools/scenario", scenario_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/tools/correlations", correlation_matrix_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/scenario/markets", api_markets, methods=["GET"])
    app.add_api_route("/api/scenario/correlations/{anchor_slug}",
                      api_correlations, methods=["GET"])
    app.add_api_route("/api/scenario/compute", api_compute_scenario, methods=["POST"])
    app.add_api_route("/api/scenario/save", api_save_scenario, methods=["POST"])
    app.add_api_route("/api/scenario/saved", api_list_saved, methods=["GET"])
    app.add_api_route("/api/scenario/heatmap", api_heatmap, methods=["GET"])
