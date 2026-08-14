// Whale Watch frontend.
//
// Vanilla JS by design — matches the lightweight pattern in
// polymarket_weather_dashboard and top-traders-dashboard. The gateway proxies
// /api/* to this dashboard's backend, so all fetches use the same origin.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ---- helpers ---------------------------------------------------------------

function fmtUSD(v, opts = {}) {
  if (v == null || isNaN(v)) return '—';
  const n = Number(v);
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + 'T';
  if (abs >= 1e9)  return (n / 1e9).toFixed(2)  + 'B';
  if (abs >= 1e6)  return (n / 1e6).toFixed(2)  + 'M';
  if (abs >= 1e3)  return (n / 1e3).toFixed(1)  + 'K';
  return n.toFixed(opts.decimals ?? 0);
}

function fmtPct(v, decimals = 2) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(decimals) + '%';
}

function fmtInt(v) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toLocaleString();
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

async function api(path) {
  const res = await fetch(path, { credentials: 'include' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---- tab nav ---------------------------------------------------------------

$('#tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-tab]');
  if (!btn) return;
  const name = btn.dataset.tab;
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b === btn));
  $$('main .tab').forEach((s) => s.classList.toggle('active', s.id === `tab-${name}`));
  loaders[name]?.();
});

// ---- feed ------------------------------------------------------------------

async function loadFeed() {
  const status = $('#feed-status');
  const table = $('#feed-table');
  status.textContent = 'loading…';
  table.hidden = true;
  try {
    const rows = await api('/api/feed?limit=80');
    if (!rows.length) {
      status.textContent = 'No filings yet — the ingester is still warming up. Check back in 30–60 min.';
      return;
    }
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = rows.map((r) => `
      <tr>
        <td class="mono">${escapeHtml(r.filed_date ?? '')}</td>
        <td><span class="tag-pill">${escapeHtml(r.source)}</span></td>
        <td class="mono">${escapeHtml(r.kind ?? '')}</td>
        <td>${escapeHtml(r.filer ?? '')}</td>
        <td>${r.target_ticker ? `<a href="#" data-ticker="${escapeHtml(r.target_ticker)}">${escapeHtml(r.target_ticker)}</a>` : ''} <span class="dim">${escapeHtml(r.target_name ?? '')}</span></td>
        <td class="right mono">${r.source === 'edgar_13d' ? fmtPct(r.value_usd) : fmtUSD(r.value_usd)}</td>
      </tr>
    `).join('');
    status.hidden = true;
    table.hidden = false;
  } catch (e) {
    status.textContent = `error: ${e.message}`;
  }
}

// ---- whales list -----------------------------------------------------------

async function loadWhales() {
  const status = $('#whales-status');
  const table = $('#whales-table');
  status.textContent = 'loading…';
  table.hidden = true;
  $('#whale-detail').hidden = true;
  try {
    const rows = await api('/api/whales');
    if (!rows.length) {
      status.textContent = 'No entities yet.';
      return;
    }
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = rows.map((r) => `
      <tr class="clickable" data-slug="${escapeHtml(r.slug)}">
        <td><strong>${escapeHtml(r.parent_name)}</strong>
            <div class="dim" style="font-size:11px">${escapeHtml(r.description ?? '')}</div></td>
        <td><span class="tag-pill ${escapeHtml(r.entity_type ?? '')}">${escapeHtml(r.entity_type ?? '—')}</span></td>
        <td class="right mono">${r.n_ciks}</td>
        <td class="mono">${escapeHtml(r.latest_quarter ?? '—')}</td>
        <td class="right mono">${fmtUSD(r.latest_book_usd)}</td>
      </tr>
    `).join('');
    status.hidden = true;
    table.hidden = false;
    tbody.querySelectorAll('tr.clickable').forEach((tr) => {
      tr.addEventListener('click', () => loadWhaleDetail(tr.dataset.slug));
    });
  } catch (e) {
    status.textContent = `error: ${e.message}`;
  }
}

async function loadWhaleDetail(slug) {
  const panel = $('#whale-detail');
  panel.hidden = false;
  panel.innerHTML = '<div class="loading">loading…</div>';
  try {
    const [d, dl] = await Promise.all([
      api(`/api/whale/${encodeURIComponent(slug)}`),
      api(`/api/whale/${encodeURIComponent(slug)}/deltas`),
    ]);
    panel.innerHTML = `
      <h3>${escapeHtml(d.entity.parent_name)}</h3>
      <div class="desc">${escapeHtml(d.entity.description ?? '')}</div>
      <div class="dim mono" style="margin-bottom:14px">
        ${d.ciks.length} CIK(s) · latest 13F: ${escapeHtml(d.latest_quarter ?? '—')}
      </div>

      <h4 style="margin:18px 0 6px">Top positions (latest quarter)</h4>
      <table class="rows">
        <thead><tr><th>Ticker</th><th>Issuer</th><th class="right">Shares</th><th class="right">Value</th><th>Put/Call</th></tr></thead>
        <tbody>${(d.top_positions || []).slice(0, 25).map((p) => `
          <tr>
            <td class="mono">${escapeHtml(p.ticker ?? '')}</td>
            <td>${escapeHtml(p.issuer_name ?? '')}</td>
            <td class="right mono">${fmtInt(p.shares)}</td>
            <td class="right mono">${fmtUSD(p.value_usd)}</td>
            <td class="mono dim">${escapeHtml(p.put_call ?? '')}</td>
          </tr>`).join('')}
        </tbody>
      </table>

      <h4 style="margin:24px 0 6px">Q-over-Q moves</h4>
      <table class="rows">
        <thead><tr><th>Ticker</th><th>Action</th><th class="right">Δ shares</th><th class="right">Δ %</th><th class="right">Δ value</th></tr></thead>
        <tbody>${(dl.deltas || []).slice(0, 25).map((m) => `
          <tr>
            <td class="mono">${escapeHtml(m.ticker ?? '')}</td>
            <td class="action-${escapeHtml(m.action)}">${escapeHtml(m.action)}</td>
            <td class="right mono">${fmtInt(m.delta_shares)}</td>
            <td class="right mono">${fmtPct(m.delta_pct, 1)}</td>
            <td class="right mono">${fmtUSD(m.delta_value_usd)}</td>
          </tr>`).join('')}
        </tbody>
      </table>

      ${d.recent_13d?.length ? `
        <h4 style="margin:24px 0 6px">Recent 13D/G filings</h4>
        <table class="rows">
          <thead><tr><th>Filed</th><th>Schedule</th><th>Target</th><th class="right">Stake</th></tr></thead>
          <tbody>${d.recent_13d.map((f) => `
            <tr>
              <td class="mono">${escapeHtml(f.filed_date)}</td>
              <td class="mono">${escapeHtml(f.schedule)}</td>
              <td>${escapeHtml(f.target_ticker ?? '')} <span class="dim">${escapeHtml(f.target_name ?? '')}</span></td>
              <td class="right mono">${fmtPct(f.ownership_pct)}</td>
            </tr>`).join('')}
          </tbody>
        </table>` : ''}
    `;
  } catch (e) {
    panel.innerHTML = `<div class="loading">error: ${escapeHtml(e.message)}</div>`;
  }
}

// ---- ticker ----------------------------------------------------------------

$('#ticker-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('#ticker-input').value.trim().toUpperCase();
  if (t) loadTicker(t);
});

document.addEventListener('click', (e) => {
  const a = e.target.closest('a[data-ticker]');
  if (!a) return;
  e.preventDefault();
  $('#tabs button[data-tab="ticker"]').click();
  $('#ticker-input').value = a.dataset.ticker;
  loadTicker(a.dataset.ticker);
});

async function loadTicker(t) {
  const out = $('#ticker-result');
  out.innerHTML = '<div class="loading">loading…</div>';
  try {
    const [d, ins] = await Promise.all([
      api(`/api/ticker/${encodeURIComponent(t)}`),
      api(`/api/ticker/${encodeURIComponent(t)}/insider?days=180`),
    ]);
    out.innerHTML = `
      <div class="panel">
        <h3>${escapeHtml(t)} <span class="dim">· latest 13F: ${escapeHtml(d.latest_quarter ?? '—')}</span></h3>

        <h4>Top institutional holders</h4>
        <table class="rows">
          <thead><tr><th>Whale</th><th>Type</th><th class="right">Shares</th><th class="right">Value</th></tr></thead>
          <tbody>${(d.holders || []).map((h) => `
            <tr>
              <td>${escapeHtml(h.parent_name)}</td>
              <td><span class="tag-pill ${escapeHtml(h.entity_type ?? '')}">${escapeHtml(h.entity_type ?? '—')}</span></td>
              <td class="right mono">${fmtInt(h.shares)}</td>
              <td class="right mono">${fmtUSD(h.value_usd)}</td>
            </tr>`).join('')}
          </tbody>
        </table>

        <h4 style="margin-top:18px">Q-over-Q moves</h4>
        <table class="rows">
          <thead><tr><th>Whale</th><th>Action</th><th class="right">Δ shares</th><th class="right">Δ value</th></tr></thead>
          <tbody>${(d.moves || []).map((m) => `
            <tr>
              <td>${escapeHtml(m.parent_name)}</td>
              <td class="action-${escapeHtml(m.action)}">${escapeHtml(m.action)}</td>
              <td class="right mono">${fmtInt(m.delta_shares)}</td>
              <td class="right mono">${fmtUSD(m.delta_value_usd)}</td>
            </tr>`).join('')}
          </tbody>
        </table>

        <h4 style="margin-top:18px">Insider transactions (last 180d, P/S/A)</h4>
        ${ins.txns?.length ? `<table class="rows">
          <thead><tr><th>Date</th><th>Code</th><th>Insider</th><th>Role</th><th class="right">Shares</th><th class="right">Price</th><th class="right">Value</th></tr></thead>
          <tbody>${ins.txns.map((x) => `
            <tr>
              <td class="mono">${escapeHtml(x.txn_date)}</td>
              <td class="mono ${x.txn_code === 'P' ? 'action-ADD' : x.txn_code === 'S' ? 'action-TRIM' : ''}">${escapeHtml(x.txn_code)}</td>
              <td>${escapeHtml(x.insider_name)}</td>
              <td class="dim">${escapeHtml(x.insider_role ?? '')}</td>
              <td class="right mono">${fmtInt(x.shares)}</td>
              <td class="right mono">${x.price ? '$'+Number(x.price).toFixed(2) : '—'}</td>
              <td class="right mono">${fmtUSD(x.value_usd)}</td>
            </tr>`).join('')}
          </tbody>
        </table>` : '<div class="dim">No insider transactions on file.</div>'}
      </div>
    `;
  } catch (e) {
    out.innerHTML = `<div class="loading">error: ${escapeHtml(e.message)}</div>`;
  }
}

// ---- activist --------------------------------------------------------------

async function loadActivist() {
  const status = $('#activist-status');
  const table = $('#activist-table');
  status.textContent = 'loading…';
  table.hidden = true;
  try {
    const rows = await api('/api/activist?limit=150');
    if (!rows.length) { status.textContent = 'No 13D/G filings yet.'; return; }
    table.querySelector('tbody').innerHTML = rows.map((r) => `
      <tr>
        <td class="mono">${escapeHtml(r.filed_date)}</td>
        <td class="mono"><span class="tag-pill ${r.schedule.startsWith('13D') ? 'activist' : ''}">${escapeHtml(r.schedule)}</span></td>
        <td>${escapeHtml(r.filer_name ?? '(unmapped)')}
            ${r.filer_type ? `<div><span class="tag-pill ${escapeHtml(r.filer_type)}">${escapeHtml(r.filer_type)}</span></div>` : ''}</td>
        <td>${r.target_ticker ? `<a href="#" data-ticker="${escapeHtml(r.target_ticker)}">${escapeHtml(r.target_ticker)}</a>` : ''}
            <div class="dim">${escapeHtml(r.target_name ?? '')}</div></td>
        <td class="right mono">${fmtPct(r.ownership_pct)}</td>
        <td class="intent-cell">${escapeHtml(r.intent_excerpt ?? '')}</td>
      </tr>
    `).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}

// ---- cluster buys ----------------------------------------------------------

async function loadCluster() {
  const status = $('#cluster-status');
  const table = $('#cluster-table');
  status.textContent = 'loading…';
  table.hidden = true;
  try {
    const rows = await api('/api/cluster-buys?days=14&min_insiders=3');
    if (!rows.length) { status.textContent = 'No cluster buys in the last 14 days at threshold ≥3 insiders.'; return; }
    table.querySelector('tbody').innerHTML = rows.map((r) => `
      <tr>
        <td class="mono"><a href="#" data-ticker="${escapeHtml(r.issuer_ticker)}">${escapeHtml(r.issuer_ticker)}</a></td>
        <td>${escapeHtml(r.issuer_name ?? '')}</td>
        <td class="right mono action-ADD">${r.n_insiders}</td>
        <td class="right mono">${fmtInt(r.total_shares)}</td>
        <td class="right mono">${fmtUSD(r.total_value)}</td>
        <td class="mono dim">${escapeHtml(r.first_txn)} → ${escapeHtml(r.last_txn)}</td>
      </tr>`).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}

// ---- correlations ----------------------------------------------------------

async function loadCorrelations() {
  const status = $('#corr-status');
  const table = $('#corr-table');
  status.textContent = 'loading…';
  table.hidden = true;
  try {
    const rows = await api('/api/correlations?limit=120');
    if (!rows.length) {
      status.textContent = 'No Polymarket cross-links recorded yet — needs at least one filing AND a Polymarket market matching its ticker.';
      return;
    }
    table.querySelector('tbody').innerHTML = rows.map((r) => {
      const sign = (r.edge_bps ?? 0) >= 0 ? 'bps-up' : 'bps-down';
      const slug = r.polymarket_slug
        ? `<a href="https://polymarket.com/event/${escapeHtml(r.polymarket_slug)}" target="_blank" rel="noopener">${escapeHtml(r.polymarket_question ?? r.polymarket_slug)}</a>`
        : escapeHtml(r.polymarket_question ?? r.polymarket_market_id);
      const fmt = (p) => p == null ? '—' : Number(p).toFixed(3);
      return `
        <tr>
          <td class="mono">${escapeHtml((r.recorded_at ?? '').slice(0,16).replace('T',' '))}</td>
          <td class="dim">${escapeHtml(r.source_label ?? r.source_table)}</td>
          <td>${slug}</td>
          <td class="right mono">${fmt(r.price_at_filing)}</td>
          <td class="right mono">${fmt(r.price_24h_after)}</td>
          <td class="right mono">${fmt(r.price_7d_after)}</td>
          <td class="right mono">${fmt(r.price_30d_after)}</td>
          <td class="right mono ${sign}">${r.edge_bps == null ? '—' : Number(r.edge_bps).toFixed(0)}</td>
        </tr>`;
    }).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}

// ---- consensus -------------------------------------------------------------

async function loadConsensus() {
  const status = $('#consensus-status');
  const table  = $('#consensus-table');
  const dir    = $('#consensus-direction').value;
  status.textContent = 'loading…'; table.hidden = true;
  try {
    const data = await api(`/api/consensus?direction=${dir}&limit=80&min_whales=5`);
    const rows = data.rows || [];
    if (!rows.length) { status.textContent = 'No consensus data — needs at least one 13F ingest cycle.'; return; }
    table.querySelector('tbody').innerHTML = rows.map((r) => {
      const sc = r.consensus_score ?? 0;
      const cls = sc > 0 ? 'consensus-pos' : sc < 0 ? 'consensus-neg' : 'dim';
      return `<tr>
        <td class="mono"><a href="#" data-ticker="${escapeHtml(r.ticker)}">${escapeHtml(r.ticker)}</a></td>
        <td>${escapeHtml(r.issuer_name ?? '')}</td>
        <td class="right mono">${r.n_whales_long}</td>
        <td class="right mono action-ADD">${r.n_whales_added}</td>
        <td class="right mono action-TRIM">${r.n_whales_trimmed}</td>
        <td class="right mono ${cls}">${sc.toFixed(2)}</td>
        <td class="right mono dim">${r.crowdedness_pctile == null ? '—' : Number(r.crowdedness_pctile).toFixed(0)}</td>
      </tr>`;
    }).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}
$('#consensus-direction').addEventListener('change', loadConsensus);

// ---- crowdedness -----------------------------------------------------------

async function loadCrowd() {
  const status = $('#crowd-status'); const table = $('#crowd-table');
  status.textContent = 'loading…'; table.hidden = true;
  try {
    const data = await api('/api/crowdedness?limit=80');
    const rows = data.rows || [];
    if (!rows.length) { status.textContent = 'No crowdedness data.'; return; }
    table.querySelector('tbody').innerHTML = rows.map((r) => `
      <tr>
        <td class="mono"><a href="#" data-ticker="${escapeHtml(r.ticker)}">${escapeHtml(r.ticker)}</a></td>
        <td>${escapeHtml(r.issuer_name ?? '')}</td>
        <td class="right mono">${r.n_whales_long}</td>
        <td class="right mono"><span class="crowd-bar" style="width:${Math.min(80, r.crowdedness_pctile)}px"></span>${Number(r.crowdedness_pctile ?? 0).toFixed(0)}</td>
        <td class="right mono">${fmtUSD(r.aggregate_value_usd)}</td>
        <td class="right mono ${r.consensus_score>0?'consensus-pos':r.consensus_score<0?'consensus-neg':'dim'}">${(r.consensus_score ?? 0).toFixed(2)}</td>
      </tr>`).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}

// ---- COT -------------------------------------------------------------------

async function loadCOT() {
  const status = $('#cot-status'); const table = $('#cot-table');
  status.textContent = 'loading…'; table.hidden = true;
  try {
    const rows = await api('/api/cot');
    if (!rows.length) { status.textContent = 'No COT data — first ingest runs daily.'; return; }
    table.querySelector('tbody').innerHTML = rows.map((r) => {
      const specNet = (r.noncommercial_long ?? 0) - (r.noncommercial_short ?? 0);
      const commNet = (r.commercial_long ?? 0) - (r.commercial_short ?? 0);
      const specCls = specNet > 0 ? 'consensus-pos' : specNet < 0 ? 'consensus-neg' : 'dim';
      const commCls = commNet > 0 ? 'consensus-pos' : commNet < 0 ? 'consensus-neg' : 'dim';
      return `<tr>
        <td class="mono">${escapeHtml(r.market_code)}</td>
        <td>${escapeHtml(r.market_name)}</td>
        <td class="mono">${escapeHtml(r.report_date)}</td>
        <td class="right mono ${specCls}">${fmtInt(specNet)}</td>
        <td class="right mono ${commCls}">${fmtInt(commNet)}</td>
        <td class="right mono dim">${fmtInt(r.open_interest)}</td>
      </tr>`;
    }).join('');
    status.hidden = true; table.hidden = false;
  } catch (e) { status.textContent = `error: ${e.message}`; }
}

// ---- watchlist + alerts ----------------------------------------------------

async function loadWatchlist() {
  try {
    const items = await api('/api/watchlist');
    const tbody = $('#wl-table tbody');
    tbody.innerHTML = items.length ? items.map((i) => `
      <tr>
        <td class="mono">${escapeHtml(i.kind)}</td>
        <td class="mono">${i.kind === 'ticker'
          ? `<a href="#" data-ticker="${escapeHtml(i.target)}">${escapeHtml(i.target)}</a>`
          : escapeHtml(i.target)}</td>
        <td class="dim mono">${escapeHtml(i.created_at?.slice(0, 10) ?? '')}</td>
        <td class="right"><button class="btn-link" data-wl-del="${i.id}">remove</button></td>
      </tr>`).join('') : '<tr><td colspan="4" class="dim">No items.</td></tr>';
  } catch (e) {
    $('#wl-table tbody').innerHTML = `<tr><td colspan="4" class="dim">error: ${escapeHtml(e.message)}</td></tr>`;
  }

  try {
    const data = await api('/api/alerts');
    $('#alerts-table tbody').innerHTML = (data.rules || []).length
      ? data.rules.map((r) => `
        <tr>
          <td class="mono">${escapeHtml(r.rule_type)}</td>
          <td class="mono">${escapeHtml(r.target ?? '*any*')}</td>
          <td class="right mono">${r.threshold ?? '—'}</td>
          <td class="dim">${escapeHtml(r.webhook_url ?? r.email ?? '—')}</td>
          <td class="dim mono">${escapeHtml(r.last_fired?.slice(0, 16).replace('T',' ') ?? 'never')}</td>
          <td class="right"><button class="btn-link" data-alert-del="${r.id}">delete</button></td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="dim">No alert rules.</td></tr>';

    $('#deliveries-table tbody').innerHTML = (data.recent_deliveries || []).length
      ? data.recent_deliveries.map((d) => `
        <tr>
          <td class="mono">${escapeHtml(d.fired_at?.slice(0, 16).replace('T',' ') ?? '')}</td>
          <td class="dim mono">${escapeHtml(d.source_table)}#${d.source_id}</td>
          <td class="mono ${d.delivery_status === 'sent' ? 'consensus-pos' : d.delivery_status === 'failed' ? 'consensus-neg' : 'dim'}">${escapeHtml(d.delivery_status)}</td>
          <td class="mono">${d.response_code ?? '—'}</td>
          <td class="dim">${escapeHtml(d.error ?? '')}</td>
        </tr>`).join('')
      : '<tr><td colspan="5" class="dim">No deliveries yet.</td></tr>';
  } catch (e) {
    $('#alerts-table tbody').innerHTML = `<tr><td colspan="6" class="dim">error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

$('#wl-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const kind = $('#wl-kind').value;
  const target = $('#wl-target').value.trim();
  if (!target) return;
  try {
    const res = await fetch('/api/watchlist', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, target }),
    });
    if (!res.ok) throw new Error(`${res.status} — already on list?`);
    $('#wl-target').value = '';
    loadWatchlist();
  } catch (e) { alert(e.message); }
});

$('#alert-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    rule_type: $('#alert-type').value,
    target: $('#alert-target').value.trim() || null,
    threshold: parseFloat($('#alert-threshold').value) || null,
    webhook_url: $('#alert-webhook').value.trim() || null,
  };
  try {
    const res = await fetch('/api/alerts', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail ?? res.statusText);
    }
    e.target.reset();
    loadWatchlist();
  } catch (e) { alert(e.message); }
});

document.addEventListener('click', async (e) => {
  if (e.target.matches('[data-wl-del]')) {
    const id = e.target.dataset.wlDel;
    await fetch(`/api/watchlist/${id}`, { method: 'DELETE', credentials: 'include' });
    loadWatchlist();
  } else if (e.target.matches('[data-alert-del]')) {
    const id = e.target.dataset.alertDel;
    await fetch(`/api/alerts/${id}`, { method: 'DELETE', credentials: 'include' });
    loadWatchlist();
  }
});

// ---- WebSocket live feed ---------------------------------------------------

function connectWS() {
  const ind = $('#ws-indicator');
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws;
  try { ws = new WebSocket(`${proto}//${location.host}/ws/feed`); }
  catch { setTimeout(connectWS, 5000); return; }

  ws.addEventListener('open', () => { ind.textContent = 'live'; ind.classList.add('connected'); });
  ws.addEventListener('close', () => { ind.textContent = 'offline'; ind.classList.remove('connected'); setTimeout(connectWS, 5000); });
  ws.addEventListener('error', () => ws.close());
  ws.addEventListener('message', (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'filing') {
      // Refresh the feed if the user is on it. Cheap.
      if ($('#tab-feed').classList.contains('active')) loadFeed();
    }
  });
}

// ---- bootstrap -------------------------------------------------------------

const loaders = {
  feed: loadFeed,
  whales: loadWhales,
  ticker: () => {},
  activist: loadActivist,
  cluster: loadCluster,
  correlations: loadCorrelations,
  consensus: loadConsensus,
  crowded: loadCrowd,
  cot: loadCOT,
  watchlist: loadWatchlist,
};

loadFeed();
connectWS();
