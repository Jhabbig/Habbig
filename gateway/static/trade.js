/**
 * trade.js — Markets Tab (injected into every dashboard via switcher)
 *
 * Provides a unified Polymarket + Kalshi markets browser, betting, and
 * portfolio view. Reads config from window.__hbSwitcher.markets.
 */
(function () {
  'use strict';

  const cfg = window.__hbSwitcher;
  if (!cfg) return;

  const M = cfg.markets || {};
  const DOMAIN = cfg.domain || 'narve.ai';
  const API_BASE = ''; // Same origin — gateway handles /api/markets/* on all subdomains

  // ── State ──────────────────────────────────────────────────────────────────
  let currentSubTab = 'markets'; // markets | portfolio | orders
  let marketsData = [];
  let marketsPage = 1;
  let marketsTotal = 0;
  let marketsLoading = false;
  let searchQuery = '';
  let filterSource = '';
  let filterCategory = '';
  let filterEnvRelevant = false; // Pro-only filter — only show markets with cached env analysis
  let sortBy = 'volume';
  let connections = M.connections || { polymarket: { connected: false }, kalshi: { connected: false } };

  // ── Helpers ────────────────────────────────────────────────────────────────
  function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML.replace(/'/g, '&#39;'); }
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return (ctx || document).querySelectorAll(sel); }

  async function api(path, opts = {}) {
    const url = API_BASE + path;
    const init = { headers: { 'Content-Type': 'application/json' }, ...opts };
    if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
    try {
      const r = await fetch(url, init);
      const data = await r.json();
      if (!r.ok) return { _error: data.error || data.detail || `HTTP ${r.status}`, _status: r.status };
      return data;
    } catch (e) {
      return { _error: 'Network error — please try again' };
    }
  }

  function pct(v) { return (v * 100).toFixed(0) + '%'; }
  function usd(v) {
    if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
    return '$' + v.toFixed(2);
  }

  // ── CSS injection ─────────────────────────────────────────────────────────
  function injectStyles() {
    if ($('#hb-trade-css')) return;
    const style = document.createElement('style');
    style.id = 'hb-trade-css';
    style.textContent = `
      #hb-markets-overlay {
        position: fixed; inset: 0; z-index: 10000;
        background: #0d0d0d; color: #fff;
        font-family: 'Inter', -apple-system, sans-serif;
        overflow-y: auto; display: none;
      }
      #hb-markets-overlay.open { display: block; }
      .hb-m-header {
        position: sticky; top: 0; z-index: 10;
        background: rgba(13,13,13,0.92); backdrop-filter: blur(16px);
        border-bottom: 1px solid #2a2a2a;
        padding: 16px 32px; display: flex; align-items: center; justify-content: space-between;
      }
      .hb-m-title { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
      .hb-m-close { background: none; border: 1px solid #2a2a2a; border-radius: 6px;
        color: #a3a3a3; cursor: pointer; padding: 6px 14px; font-size: 13px; font-family: inherit; }
      .hb-m-close:hover { color: #fff; border-color: #fff; }
      .hb-m-bar {
        display: flex; align-items: center; gap: 12px; padding: 12px 32px;
        border-bottom: 1px solid #1f1f1f; flex-wrap: wrap;
      }
      .hb-m-search {
        flex: 1; min-width: 200px; padding: 10px 14px;
        background: #141414; border: 1px solid #2a2a2a; border-radius: 6px;
        color: #fff; font-family: inherit; font-size: 14px;
      }
      .hb-m-search:focus { outline: none; border-color: #fff; box-shadow: 0 0 0 3px rgba(255,255,255,0.08); }
      .hb-m-select {
        padding: 10px 14px; background: #141414; border: 1px solid #2a2a2a;
        border-radius: 6px; color: #fff; font-family: inherit; font-size: 13px; cursor: pointer;
      }
      .hb-m-tabs { display: flex; gap: 0; }
      .hb-m-tab {
        padding: 10px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
        background: transparent; border: 1px solid #2a2a2a; color: #a3a3a3;
        font-family: inherit; transition: all 0.15s;
      }
      .hb-m-tab:first-child { border-radius: 6px 0 0 6px; }
      .hb-m-tab:last-child { border-radius: 0 6px 6px 0; }
      .hb-m-tab:not(:first-child) { border-left: none; }
      .hb-m-tab.active { background: #fff; color: #0d0d0d; border-color: #fff; }
      .hb-m-body { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }
      .hb-m-connect-banner {
        background: #141414; border: 1px solid #2a2a2a; border-radius: 12px;
        padding: 20px 24px; margin-bottom: 24px;
        display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;
      }
      .hb-m-connect-text { font-size: 14px; color: #a3a3a3; }
      .hb-m-connect-btns { display: flex; gap: 10px; }
      .hb-m-btn {
        padding: 10px 20px; border-radius: 6px; font-size: 13px; font-weight: 600;
        cursor: pointer; font-family: inherit; transition: transform 0.1s, box-shadow 0.2s; border: none;
      }
      .hb-m-btn:hover { transform: translateY(-1px); }
      .hb-m-btn-primary { background: #fff; color: #0d0d0d; }
      .hb-m-btn-outline { background: transparent; color: #fff; border: 1px solid #2a2a2a; }
      .hb-m-btn-outline:hover { border-color: #fff; }
      .hb-m-btn-sm { padding: 6px 14px; font-size: 12px; }
      .hb-m-btn-danger { background: transparent; color: #a3a3a3; border: 1px solid #2a2a2a; }
      .hb-m-btn-danger:hover { color: #ff6b6b; border-color: #ff6b6b; }
      /* Table */
      .hb-m-table { width: 100%; border-collapse: collapse; }
      .hb-m-table th {
        text-align: left; padding: 10px 12px; font-size: 11px; font-weight: 600;
        color: #666; text-transform: uppercase; letter-spacing: 0.06em;
        border-bottom: 1px solid #2a2a2a; cursor: pointer; user-select: none;
      }
      .hb-m-table th:hover { color: #a3a3a3; }
      .hb-m-table td { padding: 14px 12px; font-size: 14px; border-bottom: 1px solid #1f1f1f; }
      .hb-m-table tr { cursor: pointer; transition: background 0.1s; }
      .hb-m-table tbody tr:hover { background: #141414; }
      .hb-m-badge {
        display: inline-block; padding: 2px 8px; border-radius: 10px;
        font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
      }
      .hb-m-badge-poly { background: rgba(255,255,255,0.06); color: #a3a3a3; }
      .hb-m-badge-kalshi { background: rgba(255,255,255,0.06); color: #a3a3a3; }
      .hb-m-ev-high { color: #fff; font-weight: 600; }
      .hb-m-ev-mid { color: #a3a3a3; }
      .hb-m-ev-low { color: #666; }
      /* Loading */
      .hb-m-loading { text-align: center; padding: 48px 0; color: #666; }
      .hb-m-empty { text-align: center; padding: 48px 0; color: #666; }
      /* Pagination */
      .hb-m-pagination { display: flex; justify-content: center; gap: 8px; padding: 24px 0; }
      /* Detail panel */
      .hb-m-detail-overlay {
        position: fixed; inset: 0; z-index: 10001; background: rgba(0,0,0,0.6);
        display: flex; justify-content: flex-end;
      }
      .hb-m-detail-panel {
        width: 560px; max-width: 100%; background: #0d0d0d; border-left: 1px solid #2a2a2a;
        overflow-y: auto; padding: 32px;
      }
      .hb-m-detail-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; letter-spacing: -0.02em; }
      .hb-m-detail-meta { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; font-size: 13px; color: #a3a3a3; }
      .hb-m-prob-bar {
        display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin: 16px 0;
        background: #1f1f1f;
      }
      .hb-m-prob-yes { background: #fff; border-radius: 4px 0 0 4px; }
      .hb-m-prob-labels { display: flex; justify-content: space-between; font-size: 14px; font-weight: 600; margin-bottom: 24px; }
      .hb-m-signal-card {
        background: #141414; border: 1px solid #2a2a2a; border-radius: 8px;
        padding: 16px; margin-bottom: 24px;
      }
      .hb-m-signal-title { font-size: 12px; font-weight: 600; color: #666; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 12px; }
      .hb-m-signal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
      .hb-m-signal-item label { font-size: 11px; color: #666; display: block; margin-bottom: 2px; }
      .hb-m-signal-item span { font-size: 16px; font-weight: 600; }
      /* Bet form */
      .hb-m-bet-section { margin-top: 24px; }
      .hb-m-side-toggle { display: flex; gap: 0; margin-bottom: 16px; }
      .hb-m-side-btn {
        flex: 1; padding: 12px; text-align: center; font-size: 14px; font-weight: 600;
        cursor: pointer; border: 1px solid #2a2a2a; background: transparent; color: #a3a3a3;
        font-family: inherit; transition: all 0.15s;
      }
      .hb-m-side-btn:first-child { border-radius: 6px 0 0 6px; }
      .hb-m-side-btn:last-child { border-radius: 0 6px 6px 0; border-left: none; }
      .hb-m-side-btn.active-yes { background: #fff; color: #0d0d0d; border-color: #fff; }
      .hb-m-side-btn.active-no { background: #333; color: #fff; border-color: #333; }
      .hb-m-input-group { margin-bottom: 12px; }
      .hb-m-input-group label { display: block; font-size: 12px; color: #666; margin-bottom: 6px; }
      .hb-m-input {
        width: 100%; padding: 10px 14px; background: #141414; border: 1px solid #2a2a2a;
        border-radius: 6px; color: #fff; font-family: inherit; font-size: 14px;
      }
      .hb-m-input:focus { outline: none; border-color: #fff; }
      .hb-m-summary {
        background: #141414; border: 1px solid #2a2a2a; border-radius: 8px;
        padding: 14px; margin: 16px 0; font-size: 13px; color: #a3a3a3; line-height: 1.8;
      }
      /* Modal */
      .hb-m-modal-overlay {
        position: fixed; inset: 0; z-index: 10002; background: rgba(0,0,0,0.6);
        display: flex; align-items: center; justify-content: center; padding: 24px;
      }
      .hb-m-modal {
        background: #141414; border: 1px solid #2a2a2a; border-radius: 12px;
        padding: 32px; width: 100%; max-width: 440px;
      }
      .hb-m-modal-title { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
      .hb-m-modal-sub { font-size: 13px; color: #a3a3a3; margin-bottom: 24px; }
      .hb-m-success { color: #fff; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 12px; margin: 12px 0; font-size: 13px; }
      .hb-m-error { color: #ff6b6b; background: rgba(255,107,107,0.08); border: 1px solid rgba(255,107,107,0.15); border-radius: 6px; padding: 12px; margin: 12px 0; font-size: 13px; }
      /* Portfolio */
      .hb-m-port-summary {
        display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 24px;
      }
      .hb-m-port-card {
        background: #141414; border: 1px solid #2a2a2a; border-radius: 8px; padding: 20px;
      }
      .hb-m-port-card-label { font-size: 12px; color: #666; margin-bottom: 4px; }
      .hb-m-port-card-value { font-size: 24px; font-weight: 700; }
      .hb-m-pnl-pos { color: #fff; }
      .hb-m-pnl-neg { color: #666; }
      .hb-m-sig-agree { color: #fff; }
      .hb-m-sig-disagree { color: #666; }
      .hb-m-sig-neutral { color: #a3a3a3; }
      .hb-m-sig-none { color: #444; }
      .hb-m-port-row:hover td { background: rgba(255,255,255,0.02); }
      .hb-m-kelly-card {
        margin-top: 20px; padding: 16px; border: 1px solid #2a2a2a; border-radius: 8px;
        background: rgba(255,255,255,0.02);
      }
      .hb-m-kelly-title {
        font-size: 13px; font-weight: 600; letter-spacing: 0.04em;
        text-transform: uppercase; color: #d4d4d4; margin-bottom: 12px;
      }
      .hb-m-kelly-head {
        font-size: 12px; color: #a3a3a3; margin-bottom: 12px; line-height: 1.5;
      }
      .hb-m-kelly-tier {
        padding: 10px 0; border-top: 1px solid #1f1f1f;
        display: flex; align-items: baseline; justify-content: space-between;
      }
      .hb-m-kelly-tier:first-of-type { border-top: 0; }
      .hb-m-kelly-tier-label { font-size: 12px; color: #a3a3a3; letter-spacing: 0.03em; text-transform: uppercase; }
      .hb-m-kelly-tier-bet { font-size: 16px; font-weight: 700; }
      .hb-m-kelly-tier-meta { font-size: 11px; color: #666; margin-top: 2px; }
      .hb-m-kelly-need {
        padding: 12px; font-size: 13px; color: #a3a3a3;
        border: 1px dashed #2a2a2a; border-radius: 6px;
      }
      /* Locked state */
      .hb-m-locked {
        text-align: center; padding: 80px 32px;
      }
      .hb-m-locked-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.3; }
      .hb-m-locked-title { font-size: 22px; font-weight: 700; margin-bottom: 8px; }
      .hb-m-locked-sub { font-size: 14px; color: #a3a3a3; margin-bottom: 24px; }
      /* Environmental Impact — monochrome (opacity/weight, no colour) */
      .hb-m-pill {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 8px 14px; border-radius: 999px;
        background: transparent; border: 1px solid #2a2a2a;
        color: #a3a3a3; font-family: inherit; font-size: 13px; font-weight: 500;
        cursor: pointer; transition: all 0.15s;
      }
      .hb-m-pill:hover { border-color: #444; color: #d4d4d4; }
      .hb-m-pill.active {
        background: #fff; color: #0d0d0d; border-color: #fff; font-weight: 600;
      }
      .hb-m-leaf { font-size: 14px; opacity: 0.85; }
      .hb-m-env-leaf {
        display: inline-block; margin-right: 6px; opacity: 0.55; font-size: 13px;
        vertical-align: baseline;
      }
      .hb-m-env-card {
        background: #0d0d0d; border: 1px solid #2a2a2a; border-radius: 8px;
        padding: 18px; margin-bottom: 24px;
      }
      .hb-m-env-head { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
      .hb-m-env-title {
        font-size: 12px; font-weight: 600; color: #d4d4d4;
        text-transform: uppercase; letter-spacing: 0.06em;
      }
      .hb-m-env-unit {
        margin-left: auto; font-size: 11px; color: #666; font-weight: 500;
      }
      .hb-m-env-grid {
        display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
        padding-bottom: 14px; border-bottom: 1px solid #1f1f1f;
      }
      .hb-m-env-cell {}
      .hb-m-env-label {
        font-size: 11px; color: #666; text-transform: uppercase;
        letter-spacing: 0.05em; margin-bottom: 6px;
      }
      .hb-m-env-value { margin-bottom: 6px; font-feature-settings: "tnum"; }
      .hb-m-env-impact { font-size: 24px; font-weight: 700; line-height: 1; }
      /* Reduction = bright (full opacity, full weight). The spec calls this
         the "good" outcome and asks for prominence; we keep monochrome by
         using full white at 100% opacity. */
      .hb-m-env-reduction { color: #ffffff; opacity: 1; }
      /* Increase = dim. Same hue, knocked back to ~45% opacity and lighter
         weight so the eye reads it as de-emphasised without breaking the
         monochrome rule. */
      .hb-m-env-increase { color: #ffffff; opacity: 0.45; font-weight: 500; }
      .hb-m-env-na { color: #4a4a4a; font-size: 18px; font-weight: 500; }
      .hb-m-env-timeframe {
        font-size: 11px; color: #666; margin-bottom: 8px; font-style: italic;
      }
      .hb-m-env-desc {
        font-size: 12px; color: #a3a3a3; line-height: 1.55;
      }
      .hb-m-env-foot { padding-top: 14px; }
      .hb-m-env-conf { font-size: 12px; color: #a3a3a3; margin-bottom: 8px; }
      .hb-m-env-conf strong { color: #d4d4d4; font-weight: 600; }
      .hb-m-env-conf-reason { color: #666; }
      .hb-m-env-sources {
        font-size: 11px; color: #666; margin-bottom: 12px; line-height: 1.7;
      }
      .hb-m-env-sources strong { color: #a3a3a3; font-weight: 600; }
      .hb-m-env-source {
        display: inline-block; margin-right: 6px; padding: 2px 8px;
        background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 999px;
        color: #a3a3a3; font-size: 10px;
      }
      .hb-m-env-refresh { font-size: 11px; }
      /* Responsive */
      @media (max-width: 720px) {
        .hb-m-env-grid { grid-template-columns: 1fr; gap: 14px; }
        .hb-m-header, .hb-m-bar, .hb-m-body { padding-left: 16px; padding-right: 16px; }
        .hb-m-detail-panel { width: 100%; }
        .hb-m-port-summary { grid-template-columns: 1fr; }
        .hb-m-table { font-size: 13px; }
        .hb-m-table th, .hb-m-table td { padding: 10px 8px; }
      }
    `;
    document.head.appendChild(style);
  }

  // ── Build the "Markets" button in the switcher bar ─────────────────────────
  function addMarketsButton() {
    // Wait for switcher to render, then insert a Markets button
    const check = () => {
      const switcher = $('.hb-switcher-bar') || $('.hb-sw-bar') || $('[data-hb-switcher]');
      // If switcher isn't there, create a floating button instead
      if (!switcher) {
        createFloatingButton();
        return;
      }
      const btn = document.createElement('button');
      btn.className = 'hb-sw-item';
      btn.textContent = 'Markets';
      btn.style.cssText = 'cursor:pointer;background:none;border:1px solid #2a2a2a;color:#a3a3a3;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600;font-family:inherit;margin-left:8px;transition:all 0.15s';
      btn.addEventListener('mouseenter', () => { btn.style.borderColor = '#fff'; btn.style.color = '#fff'; });
      btn.addEventListener('mouseleave', () => { btn.style.borderColor = '#2a2a2a'; btn.style.color = '#a3a3a3'; });
      btn.addEventListener('click', openMarketsTab);
      switcher.appendChild(btn);
    };
    // Try immediately and with a delay (switcher may load async)
    check();
    setTimeout(check, 500);
    setTimeout(check, 1500);
  }

  function createFloatingButton() {
    if ($('#hb-markets-fab')) return;
    const btn = document.createElement('button');
    btn.id = 'hb-markets-fab';
    btn.textContent = 'Markets';
    btn.style.cssText = `
      position: fixed; bottom: 20px; right: 20px; z-index: 9999;
      background: #fff; color: #0d0d0d; border: none; border-radius: 8px;
      padding: 12px 24px; font-size: 14px; font-weight: 700; font-family: 'Inter', sans-serif;
      cursor: pointer; box-shadow: 0 0 24px rgba(255,255,255,0.15);
      transition: transform 0.1s, box-shadow 0.2s;
    `;
    btn.addEventListener('mouseenter', () => { btn.style.transform = 'translateY(-2px)'; btn.style.boxShadow = '0 0 32px rgba(255,255,255,0.25)'; });
    btn.addEventListener('mouseleave', () => { btn.style.transform = ''; btn.style.boxShadow = '0 0 24px rgba(255,255,255,0.15)'; });
    btn.addEventListener('click', openMarketsTab);
    document.body.appendChild(btn);
  }

  // ── Overlay ────────────────────────────────────────────────────────────────
  function createOverlay() {
    if ($('#hb-markets-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'hb-markets-overlay';
    overlay.innerHTML = `
      <div class="hb-m-header">
        <div class="hb-m-title">Markets</div>
        <div style="display:flex;align-items:center;gap:12px">
          <div class="hb-m-tabs">
            <button class="hb-m-tab active" data-tab="markets">Markets</button>
            <button class="hb-m-tab" data-tab="portfolio">Portfolio</button>
            <button class="hb-m-tab" data-tab="orders">Orders</button>
          </div>
          <button class="hb-m-close" id="hb-m-close">Close</button>
        </div>
      </div>
      <div class="hb-m-bar" id="hb-m-bar">
        <input class="hb-m-search" id="hb-m-search" type="text" placeholder="Search markets...">
        <select class="hb-m-select" id="hb-m-source">
          <option value="">All sources</option>
          <option value="polymarket">Polymarket</option>
          <option value="kalshi">Kalshi</option>
        </select>
        <select class="hb-m-select" id="hb-m-category">
          <option value="">All categories</option>
          <option value="politics">Politics</option>
          <option value="sports">Sports</option>
          <option value="crypto">Crypto</option>
          <option value="finance">Finance</option>
          <option value="weather">Weather</option>
          <option value="entertainment">Entertainment</option>
          <option value="science">Science</option>
          <option value="world">World</option>
          <option value="other">Other</option>
        </select>
        <select class="hb-m-select" id="hb-m-sort">
          <option value="volume">Volume</option>
          <option value="ev">EV Score</option>
          <option value="close_time">Closes Soonest</option>
          <option value="credibility">Credibility</option>
        </select>
        <button class="hb-m-pill" id="hb-m-env-pill" type="button" aria-pressed="false" title="Show only markets with environmental impact analysis (Pro)">
          <span class="hb-m-leaf" aria-hidden="true">&#127807;</span>
          <span>Environmental impact</span>
        </button>
      </div>
      <div class="hb-m-body" id="hb-m-body"></div>
    `;
    document.body.appendChild(overlay);

    // Event listeners
    $('#hb-m-close').addEventListener('click', closeMarketsTab);
    overlay.addEventListener('keydown', e => { if (e.key === 'Escape') closeMarketsTab(); });

    $$('.hb-m-tab', overlay).forEach(tab => {
      tab.addEventListener('click', () => {
        $$('.hb-m-tab', overlay).forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentSubTab = tab.dataset.tab;
        // Show/hide search bar
        $('#hb-m-bar').style.display = currentSubTab === 'markets' ? '' : 'none';
        renderBody();
      });
    });

    let searchTimeout;
    $('#hb-m-search').addEventListener('input', e => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => { searchQuery = e.target.value; marketsPage = 1; loadMarkets(); }, 300);
    });
    $('#hb-m-source').addEventListener('change', e => { filterSource = e.target.value; marketsPage = 1; loadMarkets(); });
    $('#hb-m-category').addEventListener('change', e => { filterCategory = e.target.value; marketsPage = 1; loadMarkets(); });
    $('#hb-m-sort').addEventListener('change', e => { sortBy = e.target.value; marketsPage = 1; loadMarkets(); });
    // Environmental Impact filter — toggles a pill that constrains the table
    // to markets with a cached env-relevant analysis. The backend silently
    // ignores the param for non-Pro users so the pill is harmless on Trader.
    const envPill = $('#hb-m-env-pill');
    if (envPill) {
      envPill.addEventListener('click', () => {
        filterEnvRelevant = !filterEnvRelevant;
        envPill.classList.toggle('active', filterEnvRelevant);
        envPill.setAttribute('aria-pressed', String(filterEnvRelevant));
        marketsPage = 1;
        loadMarkets();
      });
    }
  }

  function openMarketsTab() {
    injectStyles();
    createOverlay();
    const overlay = $('#hb-markets-overlay');
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';

    if (!M.enabled) {
      renderLocked();
      return;
    }
    renderBody();
  }

  function closeMarketsTab() {
    const overlay = $('#hb-markets-overlay');
    if (overlay) overlay.classList.remove('open');
    document.body.style.overflow = '';
    // Close any open detail panels
    const detail = $('.hb-m-detail-overlay');
    if (detail) detail.remove();
  }

  function renderLocked() {
    const body = $('#hb-m-body');
    $('#hb-m-bar').style.display = 'none';
    body.innerHTML = `
      <div class="hb-m-locked">
        <div class="hb-m-locked-icon">&#128274;</div>
        <div class="hb-m-locked-title">Upgrade to access Markets</div>
        <div class="hb-m-locked-sub">
          Browse Polymarket and Kalshi markets, place bets, and track your portfolio.
          Available with Trader plan and above.
        </div>
        <a href="https://${esc(DOMAIN)}/billing" class="hb-m-btn hb-m-btn-primary" style="text-decoration:none;display:inline-block">
          Upgrade to Trader
        </a>
      </div>
    `;
  }

  function renderBody() {
    if (currentSubTab === 'markets') loadMarkets();
    else if (currentSubTab === 'portfolio') loadPortfolio();
    else if (currentSubTab === 'orders') loadOrders();
  }

  // ── Markets list ───────────────────────────────────────────────────────────
  async function loadMarkets() {
    const body = $('#hb-m-body');
    if (marketsLoading) return;
    marketsLoading = true;
    body.innerHTML = '<div class="hb-m-loading">Loading markets...</div>';

    const params = new URLSearchParams({
      page: marketsPage, limit: 20, sort: sortBy,
      ...(searchQuery && { search: searchQuery }),
      ...(filterSource && { source: filterSource }),
      ...(filterCategory && { category: filterCategory }),
      ...(filterEnvRelevant && { env_relevant: '1' }),
    });

    const data = await api(`/api/markets/unified?${params}`);
    marketsLoading = false;

    if (data._error) {
      body.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = data._error || '';
      body.appendChild(d);
      return;
    }

    marketsData = data.markets || [];
    marketsTotal = data.total || 0;

    renderConnectBanner(body);
    renderMarketsTable(body);
  }

  function renderConnectBanner(container) {
    if (connections.polymarket.connected && connections.kalshi.connected) return;
    let html = '<div class="hb-m-connect-banner"><div class="hb-m-connect-text">Connect your accounts to place bets and track your portfolio</div><div class="hb-m-connect-btns">';
    if (!connections.polymarket.connected) html += '<button class="hb-m-btn hb-m-btn-outline hb-m-btn-sm" onclick="window.__hbTrade.connectPolymarket()">Connect Polymarket</button>';
    if (!connections.kalshi.connected) html += '<button class="hb-m-btn hb-m-btn-outline hb-m-btn-sm" onclick="window.__hbTrade.connectKalshi()">Connect Kalshi</button>';
    html += '</div></div>';
    const existing = container.querySelector('.hb-m-connect-banner');
    if (existing) existing.outerHTML = html;
    else container.insertAdjacentHTML('afterbegin', html);
  }

  function renderMarketsTable(container) {
    if (!marketsData.length) {
      const banner = container.querySelector('.hb-m-connect-banner');
      const after = banner ? banner.outerHTML : '';
      container.innerHTML = after + '<div class="hb-m-empty">No markets found</div>';
      return;
    }

    let html = `<table class="hb-m-table"><thead><tr>
      <th>Market</th><th>Source</th><th>Yes</th><th>No</th>
      <th>Volume</th><th>Closes</th><th>EV</th><th>Cred.</th><th></th>
    </tr></thead><tbody>`;

    for (const m of marketsData) {
      const badge = m.source === 'polymarket'
        ? '<span class="hb-m-badge hb-m-badge-poly">POLY</span>'
        : '<span class="hb-m-badge hb-m-badge-kalshi">KALSHI</span>';

      const ev = m.betyc_ev_score;
      const evClass = ev !== null ? (ev > 0.1 ? 'hb-m-ev-high' : ev > 0 ? 'hb-m-ev-mid' : 'hb-m-ev-low') : 'hb-m-ev-low';
      const evStr = ev !== null ? (ev > 0 ? '+' : '') + ev.toFixed(2) : '—';

      const cred = m.betyc_avg_credibility;
      const credStr = cred !== null ? cred.toFixed(2) : '—';

      const closeStr = m.close_time ? new Date(m.close_time).toLocaleDateString() : '—';

      const canBet = (m.source === 'polymarket' && connections.polymarket.connected) ||
                     (m.source === 'kalshi' && connections.kalshi.connected);
      const betBtn = canBet
        ? `<button class="hb-m-btn hb-m-btn-primary hb-m-btn-sm" onclick="event.stopPropagation();window.__hbTrade.openBetModal('${esc(m.id)}','${esc(m.title)}','${m.source}',${m.yes_price})">Bet</button>`
        : `<button class="hb-m-btn hb-m-btn-outline hb-m-btn-sm" title="Connect ${m.source} first" onclick="event.stopPropagation();window.__hbTrade.connect${m.source === 'polymarket' ? 'Polymarket' : 'Kalshi'}()">Connect</button>`;

      // Leaf badge marks markets that have a cached environmental analysis
      // (only present when the user is Pro and has env_show enabled). Inline
      // glyph next to the title — no extra column so the table layout stays
      // identical for Trader-tier users who never see env data.
      const envLeaf = m.is_env_relevant
        ? '<span class="hb-m-env-leaf" title="Has environmental impact analysis" aria-label="environmental impact">&#127807;</span>'
        : '';

      html += `<tr onclick="window.__hbTrade.openDetail('${esc(m.id)}')">
        <td style="max-width:320px">${envLeaf}${esc(m.title)}</td>
        <td>${badge}</td>
        <td style="font-weight:600">${pct(m.yes_price)}</td>
        <td style="color:#a3a3a3">${pct(m.no_price)}</td>
        <td>${usd(m.volume_usd)}</td>
        <td style="font-size:12px;color:#666">${closeStr}</td>
        <td class="${evClass}">${evStr}</td>
        <td style="color:#a3a3a3">${credStr}</td>
        <td>${betBtn}</td>
      </tr>`;
    }
    html += '</tbody></table>';

    // Pagination
    const pages = Math.ceil(marketsTotal / 20);
    if (pages > 1) {
      html += '<div class="hb-m-pagination">';
      for (let i = 1; i <= Math.min(pages, 10); i++) {
        const cls = i === marketsPage ? 'hb-m-btn hb-m-btn-primary hb-m-btn-sm' : 'hb-m-btn hb-m-btn-outline hb-m-btn-sm';
        html += `<button class="${cls}" onclick="window.__hbTrade.goPage(${i})">${i}</button>`;
      }
      html += '</div>';
    }

    // Preserve connect banner if present
    const banner = container.querySelector('.hb-m-connect-banner');
    const bannerHtml = banner ? banner.outerHTML : '';
    container.innerHTML = bannerHtml + html;
  }

  // ── Environmental Impact panel block ───────────────────────────────────────
  //
  // The /api/markets/unified/{id} response includes an `environmental_impact`
  // field for Pro users with env_show enabled, populated from the cache only.
  // We render two columns (YES outcome / NO outcome) plus confidence and
  // sources. Reduction (negative MT) renders bright; increase (positive)
  // renders dim, per the spec's monochrome guidance.
  function renderEnvImpactSection(m) {
    const ei = m && m.environmental_impact;
    if (!ei || ei.is_relevant === false) return '';

    // Pull both the user's converted unit AND the underlying MT for tooltips.
    const yesConv = ei.yes_co2_impact_converted || {};
    const noConv = ei.no_co2_impact_converted || {};
    const unitLabel = (yesConv.unit_label || noConv.unit_label || 'MT CO2e');

    function fmtImpact(conv, mtRaw) {
      const v = conv && conv.value;
      if (v === null || v === undefined) return '<span class="hb-m-env-na">—</span>';
      const sign = v < 0 ? '−' : '+';
      const abs = Math.abs(v);
      // 4 sig figs is plenty for headline display.
      const display = abs >= 1000 ? abs.toLocaleString(undefined, {maximumFractionDigits: 0}) : abs.toFixed(2);
      const cls = mtRaw !== null && mtRaw !== undefined && mtRaw < 0
        ? 'hb-m-env-impact hb-m-env-reduction'
        : 'hb-m-env-impact hb-m-env-increase';
      return `<span class="${cls}">${sign}${display}</span>`;
    }

    function fmtConfidence(c) {
      const valid = ['high', 'medium', 'low', 'speculative'];
      const safe = valid.includes(c) ? c : 'speculative';
      return safe.charAt(0).toUpperCase() + safe.slice(1);
    }

    const sources = Array.isArray(ei.data_sources) ? ei.data_sources : [];
    const sourceList = sources.length
      ? sources.slice(0, 5).map(s => `<span class="hb-m-env-source">${esc(String(s))}</span>`).join('')
      : '<span class="hb-m-env-na">No sources cited</span>';

    return `
      <div class="hb-m-env-card">
        <div class="hb-m-env-head">
          <span class="hb-m-leaf" aria-hidden="true">&#127807;</span>
          <span class="hb-m-env-title">Environmental Impact</span>
          <span class="hb-m-env-unit">${esc(unitLabel)}</span>
        </div>
        <div class="hb-m-env-grid">
          <div class="hb-m-env-cell">
            <div class="hb-m-env-label">If ${esc(ei.yes_outcome_label || 'YES')} (${pct(m.yes_price)})</div>
            <div class="hb-m-env-value">${fmtImpact(yesConv, ei.yes_co2_impact_mt)}</div>
            <div class="hb-m-env-timeframe">${esc(ei.yes_impact_timeframe || '')}</div>
            <div class="hb-m-env-desc">${esc(ei.yes_impact_description || '')}</div>
          </div>
          <div class="hb-m-env-cell">
            <div class="hb-m-env-label">If ${esc(ei.no_outcome_label || 'NO')} (${pct(m.no_price)})</div>
            <div class="hb-m-env-value">${fmtImpact(noConv, ei.no_co2_impact_mt)}</div>
            <div class="hb-m-env-timeframe">${esc(ei.no_impact_timeframe || '')}</div>
            <div class="hb-m-env-desc">${esc(ei.no_impact_description || '')}</div>
          </div>
        </div>
        <div class="hb-m-env-foot">
          <div class="hb-m-env-conf">
            <strong>Confidence:</strong> ${fmtConfidence(ei.confidence)}
            ${ei.confidence_reason ? `<span class="hb-m-env-conf-reason">— ${esc(ei.confidence_reason)}</span>` : ''}
          </div>
          <div class="hb-m-env-sources"><strong>Sources:</strong> ${sourceList}</div>
          <button class="hb-m-btn hb-m-btn-outline hb-m-btn-sm hb-m-env-refresh"
                  onclick="event.stopPropagation();window.__hbTrade.refreshEnv('${esc(m.id)}')"
                  title="Force regenerate (Pro only, 5/day)">
            Refresh analysis &#8635;
          </button>
        </div>
      </div>`;
  }

  // ── Market Detail Panel ────────────────────────────────────────────────────
  async function openDetail(marketId) {
    // Close existing
    const existing = $('.hb-m-detail-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.className = 'hb-m-detail-overlay';
    overlay.innerHTML = '<div class="hb-m-detail-panel"><div class="hb-m-loading">Loading...</div></div>';
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);

    const data = await api(`/api/markets/unified/${encodeURIComponent(marketId)}`);
    if (data._error) {
      const panel = overlay.querySelector('.hb-m-detail-panel');
      panel.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = data._error || '';
      panel.appendChild(d);
      return;
    }

    const m = data;
    const badge = m.source === 'polymarket'
      ? '<span class="hb-m-badge hb-m-badge-poly">Polymarket</span>'
      : '<span class="hb-m-badge hb-m-badge-kalshi">Kalshi</span>';
    const closeStr = m.close_time ? new Date(m.close_time).toLocaleDateString() : '—';

    const canBet = (m.source === 'polymarket' && connections.polymarket.connected) ||
                   (m.source === 'kalshi' && connections.kalshi.connected);

    overlay.querySelector('.hb-m-detail-panel').innerHTML = `
      <div class="hb-m-detail-title">${esc(m.title)}</div>
      <div class="hb-m-detail-meta">
        ${badge}
        <span>Closes: ${closeStr}</span>
        <span>Volume: ${usd(m.volume_usd)}</span>
        <span>Liquidity: ${usd(m.liquidity_usd)}</span>
      </div>

      <div class="hb-m-prob-bar">
        <div class="hb-m-prob-yes" style="width:${(m.yes_price * 100).toFixed(0)}%"></div>
      </div>
      <div class="hb-m-prob-labels">
        <span>YES ${pct(m.yes_price)}</span>
        <span style="color:#a3a3a3">NO ${pct(m.no_price)}</span>
      </div>

      <div class="hb-m-signal-card">
        <div class="hb-m-signal-title">Narve Signal</div>
        <div class="hb-m-signal-grid">
          <div class="hb-m-signal-item"><label>EV Score</label><span>${m.betyc_ev_score !== null ? (m.betyc_ev_score > 0 ? '+' : '') + m.betyc_ev_score.toFixed(2) : '—'}</span></div>
          <div class="hb-m-signal-item"><label>Avg Credibility</label><span>${m.betyc_avg_credibility !== null ? m.betyc_avg_credibility.toFixed(2) : '—'}</span></div>
          <div class="hb-m-signal-item"><label>Predictions</label><span>${m.betyc_prediction_count || '—'}</span></div>
          <div class="hb-m-signal-item"><label>Consensus</label><span>${m.betyc_consensus || '—'}</span></div>
        </div>
      </div>

      <div id="hb-m-kelly-slot"></div>

      ${renderEnvImpactSection(m)}

      ${canBet ? `
      <div class="hb-m-bet-section">
        <div class="hb-m-signal-title">Place Bet</div>
        <div class="hb-m-side-toggle">
          <button class="hb-m-side-btn active-yes" data-side="yes" onclick="window.__hbTrade._toggleSide(this,'yes')">YES</button>
          <button class="hb-m-side-btn" data-side="no" onclick="window.__hbTrade._toggleSide(this,'no')">NO</button>
        </div>
        <div class="hb-m-input-group">
          <label>Amount (USD)</label>
          <input class="hb-m-input" id="hb-detail-amount" type="number" min="1" step="1" value="10" placeholder="10">
        </div>
        <div class="hb-m-summary" id="hb-detail-summary">
          Buying YES at ${pct(m.yes_price)} — potential payout ~$${(10 / m.yes_price).toFixed(2)} if correct
        </div>
        <button class="hb-m-btn hb-m-btn-primary" style="width:100%" onclick="window.__hbTrade._placeBetFromDetail('${esc(m.id)}','${m.source}')">
          Confirm Bet
        </button>
        <div id="hb-detail-bet-result"></div>
      </div>` : `
      <div style="margin-top:24px;text-align:center">
        <button class="hb-m-btn hb-m-btn-outline" onclick="window.__hbTrade.connect${m.source === 'polymarket' ? 'Polymarket' : 'Kalshi'}()">
          Connect ${m.source === 'polymarket' ? 'Polymarket wallet' : 'Kalshi account'} to bet
        </button>
      </div>`}

      <div style="margin-top:24px;display:flex;gap:10px;flex-wrap:wrap">
        <a href="${esc(m.url)}" target="_blank" rel="noopener" class="hb-m-btn hb-m-btn-outline hb-m-btn-sm" style="text-decoration:none;display:inline-block">
          View on ${m.source === 'polymarket' ? 'Polymarket' : 'Kalshi'} &rarr;
        </a>
        <button type="button" class="hb-m-btn hb-m-btn-outline hb-m-btn-sm"
                data-add-to-collection
                data-item-type="market"
                data-item-ref="${esc(m.id)}"
                title="Save this market to a collection">+ Collection</button>
      </div>
    `;

    // Wire up amount change
    const amountInput = $('#hb-detail-amount');
    if (amountInput) {
      let _side = 'yes';
      window.__hbTrade._detailSide = 'yes';
      window.__hbTrade._detailMarket = m;
      amountInput.addEventListener('input', () => {
        const amt = parseFloat(amountInput.value) || 0;
        const side = window.__hbTrade._detailSide;
        const price = side === 'yes' ? m.yes_price : m.no_price;
        const payout = price > 0 ? (amt / price).toFixed(2) : '0';
        $('#hb-detail-summary').textContent = `Buying ${side.toUpperCase()} at ${pct(price)} — potential payout ~$${payout} if correct`;
      });
    }

    // Kelly sizing — async so the panel opens fast; fills in when ready.
    loadKellyInto(m.id, overlay.querySelector('#hb-m-kelly-slot'));
  }

  async function loadKellyInto(marketId, slot) {
    if (!slot) return;
    slot.innerHTML = '<div class="hb-m-kelly-card"><div class="hb-m-kelly-title">Bet Sizing Calculator</div><div class="hb-m-loading" style="padding:8px 0">Computing Kelly…</div></div>';

    const res = await api('/api/kelly/calculate', { method: 'POST', body: { market_id: marketId } });
    if (res._error) {
      if (res._status === 400 && /bankroll/i.test(res._error || '')) {
        slot.innerHTML = `
          <div class="hb-m-kelly-card">
            <div class="hb-m-kelly-title">Bet Sizing Calculator</div>
            <div class="hb-m-kelly-need">
              Set your bankroll in <a href="/settings" style="color:#fff;text-decoration:underline">Settings → Bet sizing</a> to see Kelly recommendations.
            </div>
          </div>`;
        return;
      }
      slot.textContent = '';
      const card = document.createElement('div');
      card.className = 'hb-m-kelly-card';
      const t = document.createElement('div');
      t.className = 'hb-m-kelly-title';
      t.textContent = 'Bet Sizing Calculator';
      const err = document.createElement('div');
      err.className = 'hb-m-error';
      err.textContent = res._error || '';
      card.appendChild(t);
      card.appendChild(err);
      slot.appendChild(card);
      return;
    }

    if (!res.has_signal) {
      slot.innerHTML = `
        <div class="hb-m-kelly-card">
          <div class="hb-m-kelly-title">Bet Sizing Calculator</div>
          <div class="hb-m-kelly-need">${esc(res.message || 'No narve.ai signal yet.')}</div>
        </div>`;
      return;
    }

    const narvePct = (res.narve_yes_probability * 100).toFixed(0) + '% YES';
    const mktPct = (res.market_yes_price * 100).toFixed(0) + '% YES';
    const edgePct = (res.edge >= 0 ? '+' : '') + (res.edge * 100).toFixed(1) + 'pp';

    const tierBlocks = res.recommendations.map(r => {
      if (!r.bet_amount_usd || r.bet_amount_usd <= 0) {
        return `
          <div class="hb-m-kelly-tier">
            <div>
              <div class="hb-m-kelly-tier-label">${esc(r.label.toUpperCase())} KELLY</div>
              <div class="hb-m-kelly-tier-meta">No edge — don't bet</div>
            </div>
            <div class="hb-m-kelly-tier-bet" style="color:#666">—</div>
          </div>`;
      }
      const sideStr = r.side === 'YES' ? 'YES' : 'NO';
      return `
        <div class="hb-m-kelly-tier">
          <div>
            <div class="hb-m-kelly-tier-label">${esc(r.label.toUpperCase())} KELLY · ${sideStr}</div>
            <div class="hb-m-kelly-tier-meta">
              ${r.pct_of_bankroll.toFixed(2)}% of bankroll · Max profit ${usd(r.max_profit_usd)} · Max loss ${usd(r.max_loss_usd)}
            </div>
          </div>
          <div class="hb-m-kelly-tier-bet">${usd(r.bet_amount_usd)}</div>
        </div>`;
    }).join('');

    slot.innerHTML = `
      <div class="hb-m-kelly-card">
        <div class="hb-m-kelly-title">Bet Sizing Calculator</div>
        <div class="hb-m-kelly-head">
          Market: <strong>${esc(mktPct)}</strong> · narve.ai: <strong>${esc(narvePct)}</strong> · Edge: <strong>${esc(edgePct)}</strong><br>
          Bankroll: ${usd(res.bankroll)} — change in <a href="/settings" style="color:#a3a3a3;text-decoration:underline">Settings</a>
        </div>
        ${tierBlocks}
      </div>`;
  }

  // ── Portfolio ──────────────────────────────────────────────────────────────
  function signalGlyph(sig) {
    if (!sig) return { ch: '—', cls: 'hb-m-sig-none', title: 'No narve.ai signal' };
    if (sig.agreement === 'agree') {
      const edge = sig.edge_pp !== null && sig.edge_pp !== undefined
        ? ` (+${(sig.edge_pp * 100).toFixed(0)}pp edge)` : '';
      return { ch: '✓', cls: 'hb-m-sig-agree', title: 'narve.ai agrees' + edge };
    }
    if (sig.agreement === 'disagree') {
      const edge = sig.edge_pp !== null && sig.edge_pp !== undefined
        ? ` (${(sig.edge_pp * 100).toFixed(0)}pp)` : '';
      return { ch: '✗', cls: 'hb-m-sig-disagree', title: 'narve.ai disagrees' + edge };
    }
    if (sig.agreement === 'neutral') {
      return { ch: '↔', cls: 'hb-m-sig-neutral', title: 'narve.ai has no strong side' };
    }
    return { ch: '—', cls: 'hb-m-sig-none', title: 'No narve.ai signal yet' };
  }

  async function loadPortfolio() {
    const body = $('#hb-m-body');
    body.innerHTML = '<div class="hb-m-loading">Loading portfolio…</div>';

    const [data, stats] = await Promise.all([
      api('/api/markets/portfolio'),
      api('/api/markets/stats'),
    ]);
    if (data._error) {
      body.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = data._error || '';
      body.appendChild(d);
      return;
    }

    const pnl = (stats && !stats._error) ? stats.unrealised_pnl_usd : null;
    const active = (stats && !stats._error) ? stats.active_positions : null;
    const winRate = (stats && !stats._error) ? stats.win_rate : null;
    const pnlCls = pnl === null ? '' : (pnl >= 0 ? 'hb-m-pnl-pos' : 'hb-m-pnl-neg');
    const pnlPct = (pnl !== null && data.combined_total_usd)
      ? ((pnl / Math.max(1, data.combined_total_usd - pnl)) * 100).toFixed(1) + '%'
      : null;

    let html = `
      <div class="hb-m-port-summary">
        <div class="hb-m-port-card">
          <div class="hb-m-port-card-label">Total value</div>
          <div class="hb-m-port-card-value">${usd(data.combined_total_usd || 0)}</div>
        </div>
        <div class="hb-m-port-card">
          <div class="hb-m-port-card-label">Unrealised P&L</div>
          <div class="hb-m-port-card-value ${pnlCls}">${pnl === null ? '—' : (pnl >= 0 ? '+' : '') + usd(pnl)}${pnlPct ? ` <span style="color:#a3a3a3;font-size:12px">(${pnlPct})</span>` : ''}</div>
        </div>
        <div class="hb-m-port-card">
          <div class="hb-m-port-card-label">Active positions</div>
          <div class="hb-m-port-card-value">${active === null ? '—' : active}${winRate !== null ? ` <span style="color:#a3a3a3;font-size:12px">(${(winRate * 100).toFixed(0)}% win rate)</span>` : ''}</div>
        </div>
      </div>
    `;

    function statusBadge(src, info) {
      const status = info && info.status;
      const cls = info.connected ? 'hb-m-badge-poly' : (status === 'expired' ? 'hb-m-badge-kalshi' : '');
      const label = info.connected
        ? `${src === 'polymarket' ? 'Polymarket' : 'Kalshi'} ✓ connected`
        : status === 'expired'
          ? `${src === 'polymarket' ? 'Polymarket' : 'Kalshi'} ⚠ reconnect`
          : `${src === 'polymarket' ? 'Polymarket' : 'Kalshi'} not connected`;
      return `<span class="hb-m-badge ${cls}" style="margin-right:8px;font-size:11px">${label}</span>`;
    }
    html += `<div style="padding:0 16px 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${statusBadge('polymarket', data.polymarket || {})}
      ${statusBadge('kalshi', data.kalshi || {})}
      <button class="hb-m-btn hb-m-btn-outline hb-m-btn-sm" id="hb-port-sync" style="margin-left:auto">Refresh all</button>
    </div>`;

    const allPositions = [
      ...(data.kalshi.positions || []),
      ...(data.polymarket.positions || []),
    ];

    if (allPositions.length) {
      html += `<table class="hb-m-table"><thead><tr>
        <th>Platform</th><th>Market</th><th>Side</th><th>Shares</th>
        <th>Entry</th><th>Current</th><th>P&L</th><th>Value</th><th>narve.ai</th>
      </tr></thead><tbody>`;
      for (const p of allPositions) {
        const pnlClass = (p.pnl || 0) >= 0 ? 'hb-m-pnl-pos' : 'hb-m-pnl-neg';
        const g = signalGlyph(p.narve_signal);
        html += `<tr class="hb-m-port-row" data-market="${esc(p.market_id || '')}" data-side="${esc((p.side || '').toLowerCase())}" style="cursor:pointer">
          <td><span class="hb-m-badge hb-m-badge-${p.platform === 'polymarket' ? 'poly' : 'kalshi'}">${p.platform === 'polymarket' ? 'POLY' : 'KALSHI'}</span></td>
          <td>${esc(p.market_title || p.market_id)}</td>
          <td style="font-weight:600">${(p.side || '').toUpperCase()}</td>
          <td>${p.shares}</td>
          <td>${p.avg_price ? pct(p.avg_price) : '—'}</td>
          <td>${p.current_price ? pct(p.current_price) : '—'}</td>
          <td class="${pnlClass}">${p.pnl ? usd(p.pnl) : '—'}</td>
          <td>${p.value ? usd(p.value) : '—'}</td>
          <td class="${g.cls}" title="${esc(g.title)}" style="font-weight:700;text-align:center">${g.ch}</td>
        </tr>`;
      }
      html += '</tbody></table>';
    } else {
      html += '<div class="hb-m-empty">No positions yet. Browse markets and place your first bet.</div>';
    }

    body.innerHTML = html;

    body.querySelectorAll('.hb-m-port-row').forEach(row => {
      row.addEventListener('click', () => {
        const mid = row.dataset.market;
        if (mid) openDetail(mid);
      });
    });
    const syncBtn = $('#hb-port-sync');
    if (syncBtn) {
      syncBtn.addEventListener('click', async () => {
        syncBtn.disabled = true;
        syncBtn.textContent = 'Refreshing…';
        const r = await api('/api/markets/sync', { method: 'POST', body: {} });
        if (r._error) {
          syncBtn.textContent = r._status === 429 ? 'Try again in a minute' : 'Error — retry';
          setTimeout(() => {
            syncBtn.disabled = false;
            syncBtn.textContent = 'Refresh all';
          }, 2000);
          return;
        }
        loadPortfolio();
      });
    }
  }

  // ── Orders ─────────────────────────────────────────────────────────────────
  async function loadOrders() {
    const body = $('#hb-m-body');
    body.innerHTML = '<div class="hb-m-loading">Loading orders...</div>';

    const data = await api('/api/markets/orders');
    if (data._error) {
      body.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = data._error || '';
      body.appendChild(d);
      return;
    }

    const orders = data.orders || [];
    if (!orders.length) {
      body.innerHTML = '<div class="hb-m-empty">No orders yet.</div>';
      return;
    }

    let html = `<table class="hb-m-table"><thead><tr>
      <th>Market</th><th>Platform</th><th>Side</th><th>Amount</th>
      <th>Price</th><th>Status</th><th>Placed</th>
    </tr></thead><tbody>`;
    for (const o of orders) {
      html += `<tr>
        <td>${esc(o.market_title || o.market_id)}</td>
        <td><span class="hb-m-badge hb-m-badge-${o.platform === 'polymarket' ? 'poly' : 'kalshi'}">${o.platform === 'polymarket' ? 'POLY' : 'KALSHI'}</span></td>
        <td style="font-weight:600">${(o.side || '').toUpperCase()}</td>
        <td>${usd(o.amount || 0)}</td>
        <td>${o.price ? pct(o.price) : '—'}</td>
        <td>${esc(o.status)}</td>
        <td style="font-size:12px;color:#666">${o.placed_at ? new Date(o.placed_at).toLocaleDateString() : '—'}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    body.innerHTML = html;
  }

  // ── Connection modals ─────────────────────────────────────────────────────
  function connectKalshi() {
    showModal(`
      <div class="hb-m-modal-title">Connect Kalshi</div>
      <div class="hb-m-modal-sub">Enter your Kalshi email and password. Your password is used once to get a session token and is never stored.</div>
      <div class="hb-m-input-group"><label>Email</label><input class="hb-m-input" id="hb-kalshi-email" type="email" placeholder="you@email.com"></div>
      <div class="hb-m-input-group"><label>Password</label><input class="hb-m-input" id="hb-kalshi-pass" type="password" placeholder="Your Kalshi password"></div>
      <div id="hb-kalshi-result"></div>
      <button class="hb-m-btn hb-m-btn-primary" style="width:100%;margin-top:16px" id="hb-kalshi-submit">Connect Kalshi Account</button>
    `);
    $('#hb-kalshi-submit').addEventListener('click', async () => {
      const email = $('#hb-kalshi-email').value.trim();
      const password = $('#hb-kalshi-pass').value;
      if (!email || !password) {
        const r = $('#hb-kalshi-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = 'Email and password required';
        r.appendChild(d);
        return;
      }
      $('#hb-kalshi-submit').textContent = 'Connecting...';
      $('#hb-kalshi-submit').disabled = true;
      const data = await api('/api/markets/connect/kalshi', { method: 'POST', body: { email, password } });
      if (data._error) {
        const r = $('#hb-kalshi-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = data._error || '';
        r.appendChild(d);
        $('#hb-kalshi-submit').textContent = 'Connect Kalshi Account';
        $('#hb-kalshi-submit').disabled = false;
        return;
      }
      connections.kalshi = { connected: true, member_id: data.member_id, balance: data.balance };
      {
        const r = $('#hb-kalshi-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-success';
        d.textContent = `Connected! Member: ${data.member_id}${data.balance !== null ? ' — Balance: $' + data.balance.toFixed(2) : ''}`;
        r.appendChild(d);
      }
      setTimeout(closeModal, 1500);
    });
  }

  function connectPolymarket() {
    if (typeof window.ethereum === 'undefined') {
      showModal(`
        <div class="hb-m-modal-title">Connect Polymarket</div>
        <div class="hb-m-modal-sub">MetaMask or a compatible wallet is required to connect your Polymarket account.</div>
        <a href="https://metamask.io" target="_blank" rel="noopener" class="hb-m-btn hb-m-btn-primary" style="width:100%;text-decoration:none;display:block;text-align:center">Install MetaMask</a>
      `);
      return;
    }

    showModal(`
      <div class="hb-m-modal-title">Connect Polymarket</div>
      <div class="hb-m-modal-sub">Click below to connect your wallet. Your private key is never sent to our servers.</div>
      <div id="hb-poly-result"></div>
      <button class="hb-m-btn hb-m-btn-primary" style="width:100%" id="hb-poly-submit">Connect Wallet</button>
    `);
    $('#hb-poly-submit').addEventListener('click', async () => {
      try {
        $('#hb-poly-submit').textContent = 'Requesting wallet...';
        const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
        const address = accounts[0];
        if (!address) throw new Error('No account selected');

        const data = await api('/api/markets/connect/polymarket', { method: 'POST', body: { wallet_address: address } });
        if (data._error) {
          const r = $('#hb-poly-result');
          r.textContent = '';
          const d = document.createElement('div');
          d.className = 'hb-m-error';
          d.textContent = data._error || '';
          r.appendChild(d);
          $('#hb-poly-submit').textContent = 'Connect Wallet';
          return;
        }
        connections.polymarket = { connected: true, address: address };
        {
          const r = $('#hb-poly-result');
          r.textContent = '';
          const d = document.createElement('div');
          d.className = 'hb-m-success';
          d.textContent = `Connected: ${address.slice(0, 6)}...${address.slice(-4)}`;
          r.appendChild(d);
        }
        setTimeout(closeModal, 1500);
      } catch (e) {
        if (e.code === 4001) {
          // User rejected
          {
            const r = $('#hb-poly-result');
            r.textContent = '';
            const d = document.createElement('div');
            d.setAttribute('style', 'color:#a3a3a3;font-size:13px;margin-top:12px');
            d.textContent = 'Connection cancelled';
            r.appendChild(d);
          }
          $('#hb-poly-submit').textContent = 'Connect Wallet';
        } else {
          {
            const r = $('#hb-poly-result');
            r.textContent = '';
            const d = document.createElement('div');
            d.className = 'hb-m-error';
            d.textContent = e.message || 'Failed to connect';
            r.appendChild(d);
          }
          $('#hb-poly-submit').textContent = 'Connect Wallet';
        }
      }
    });
  }

  // ── Bet flow ──────────────────────────────────────────────────────────────
  function openBetModal(marketId, title, source, yesPrice) {
    let side = 'yes';
    showModal(`
      <div class="hb-m-modal-title">${esc(title)}</div>
      <div class="hb-m-modal-sub">Platform: ${source === 'polymarket' ? 'Polymarket' : 'Kalshi'}</div>
      <div class="hb-m-side-toggle">
        <button class="hb-m-side-btn active-yes" id="hb-bet-yes" data-side="yes">YES</button>
        <button class="hb-m-side-btn" id="hb-bet-no" data-side="no">NO</button>
      </div>
      <div class="hb-m-input-group"><label>Amount (USD)</label><input class="hb-m-input" id="hb-bet-amount" type="number" min="1" value="10"></div>
      <div class="hb-m-input-group" id="hb-bet-type-group">
        <label>Order Type</label>
        <select class="hb-m-select" id="hb-bet-type" style="width:100%">
          <option value="market">Market</option>
          <option value="limit">Limit</option>
        </select>
      </div>
      <div class="hb-m-input-group" id="hb-bet-price-group" style="display:none">
        <label>Limit Price (0.01-0.99)</label>
        <input class="hb-m-input" id="hb-bet-price" type="number" min="0.01" max="0.99" step="0.01" value="${yesPrice.toFixed(2)}">
      </div>
      <div class="hb-m-summary" id="hb-bet-summary">
        Buying YES at ${pct(yesPrice)} — potential payout ~$${(10 / yesPrice).toFixed(2)}
      </div>
      <div id="hb-bet-result"></div>
      <button class="hb-m-btn hb-m-btn-primary" style="width:100%" id="hb-bet-submit">Confirm Bet</button>
    `);

    const updateSummary = () => {
      const amt = parseFloat($('#hb-bet-amount').value) || 0;
      const price = side === 'yes' ? yesPrice : (1 - yesPrice);
      const payout = price > 0 ? (amt / price).toFixed(2) : '0';
      $('#hb-bet-summary').textContent = `Buying ${side.toUpperCase()} at ${pct(price)} — potential payout ~$${payout} if correct`;
    };

    $('#hb-bet-yes').addEventListener('click', () => { side = 'yes'; _toggleSideBtn('yes'); updateSummary(); });
    $('#hb-bet-no').addEventListener('click', () => { side = 'no'; _toggleSideBtn('no'); updateSummary(); });
    $('#hb-bet-amount').addEventListener('input', updateSummary);
    $('#hb-bet-type').addEventListener('change', e => {
      $('#hb-bet-price-group').style.display = e.target.value === 'limit' ? '' : 'none';
    });

    $('#hb-bet-submit').addEventListener('click', async () => {
      const amount = parseFloat($('#hb-bet-amount').value);
      if (!amount || amount <= 0) {
        const r = $('#hb-bet-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = 'Enter a valid amount';
        r.appendChild(d);
        return;
      }
      const orderType = $('#hb-bet-type').value;
      const limitPrice = orderType === 'limit' ? parseFloat($('#hb-bet-price').value) : null;

      $('#hb-bet-submit').textContent = source === 'polymarket' ? 'Sign in wallet...' : 'Placing bet...';
      $('#hb-bet-submit').disabled = true;

      let result;
      if (source === 'kalshi') {
        const ticker = marketId.replace('kalshi:', '');
        result = await api('/api/markets/bet/kalshi', {
          method: 'POST',
          body: { ticker, side, amount_usd: amount, type: orderType, price: limitPrice },
        });
      } else {
        // Polymarket: real EIP-712 client-side signing via eth_signTypedData_v4
        result = await signAndSubmitPolymarketOrder(marketId, side, amount, $('#hb-bet-result'));
        if (result === null) {
          // User cancelled or signing failed — UI already updated
          $('#hb-bet-submit').textContent = 'Confirm Bet';
          $('#hb-bet-submit').disabled = false;
          return;
        }
      }

      if (result._error) {
        const r = $('#hb-bet-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = result._error || '';
        r.appendChild(d);
        $('#hb-bet-submit').textContent = 'Confirm Bet';
        $('#hb-bet-submit').disabled = false;
        return;
      }

      {
        const r = $('#hb-bet-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-success';
        d.textContent = `Bet placed! Order: ${result.order_id || 'confirmed'}`;
        r.appendChild(d);
      }
      setTimeout(closeModal, 2000);
    });
  }

  function _toggleSideBtn(side) {
    const yes = $('#hb-bet-yes') || $$('[data-side="yes"]')[0];
    const no = $('#hb-bet-no') || $$('[data-side="no"]')[0];
    if (!yes || !no) return;
    yes.className = 'hb-m-side-btn' + (side === 'yes' ? ' active-yes' : '');
    no.className = 'hb-m-side-btn' + (side === 'no' ? ' active-no' : '');
  }

  // ── Polymarket EIP-712 signing ────────────────────────────────────────────
  // Signs an order matching the CTF Exchange Order struct using the user's
  // MetaMask wallet. Never sends the private key to the server.

  // Generate a random uint256 salt as a decimal string
  function _randomSalt() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    // Convert to BigInt via hex
    let hex = '0x';
    for (const b of bytes) hex += b.toString(16).padStart(2, '0');
    return BigInt(hex).toString();
  }

  // USDC and CTF share tokens both use 6 decimals on Polygon
  const POLY_DECIMALS = 6n;
  const POLY_UNIT = 1000000n; // 10^6

  // Convert a USD float to an integer string with 6 decimals (wei-equivalent)
  function _toUnits(usdFloat) {
    // Round to 6 decimal places to avoid float precision issues
    const scaled = Math.round(usdFloat * 1000000);
    return BigInt(scaled).toString();
  }

  // Given price (0-1) and amount_usdc, compute makerAmount (USDC you pay)
  // and takerAmount (shares you receive). For BUY: maker = USDC, taker = shares.
  // shares = amount_usdc / price, rounded down to avoid overpaying.
  function _calcAmounts(amountUsdc, price) {
    const makerUnits = BigInt(Math.round(amountUsdc * 1000000)); // USDC in 6 decimals
    // shares_units = (makerUnits * 10^6) / priceScaled  where priceScaled = price * 10^6
    // Simplified: shares_units = makerUnits / price
    // But we must avoid float in the final calc. Use price as string with 4 decimals.
    const priceScaled = BigInt(Math.round(price * 10000)); // price * 10^4
    if (priceScaled === 0n) throw new Error('Price cannot be zero');
    // shares_units = makerUnits * 10^4 / priceScaled
    const takerUnits = (makerUnits * 10000n) / priceScaled;
    return { maker: makerUnits.toString(), taker: takerUnits.toString() };
  }

  async function signAndSubmitPolymarketOrder(marketId, side, amountUsdc, resultEl) {
    if (typeof window.ethereum === 'undefined') {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = 'MetaMask required for Polymarket bets';
      resultEl.appendChild(d);
      return null;
    }

    // 1. Get order parameters from backend (token IDs, exchange address, connected wallet)
    resultEl.textContent = '';
    {
      const d = document.createElement('div');
      d.setAttribute('style', 'color:#a3a3a3;font-size:13px;padding:8px');
      d.textContent = 'Fetching market data...';
      resultEl.appendChild(d);
    }
    const params = await api(`/api/markets/poly/order-params/${encodeURIComponent(marketId)}`);
    if (params._error) {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = params._error || '';
      resultEl.appendChild(d);
      return null;
    }

    // 2. Ensure the connected wallet matches the one MetaMask currently has active
    let accounts;
    try {
      accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    } catch (e) {
      resultEl.textContent = '';
      if (e.code === 4001) {
        const d = document.createElement('div');
        d.setAttribute('style', 'color:#a3a3a3;font-size:13px;padding:8px');
        d.textContent = 'Wallet connection cancelled';
        resultEl.appendChild(d);
      } else {
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = e.message || 'Wallet error';
        resultEl.appendChild(d);
      }
      return null;
    }
    const activeAddr = (accounts[0] || '').toLowerCase();
    if (activeAddr !== (params.maker_address || '').toLowerCase()) {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = `Your connected wallet (${activeAddr.slice(0, 6)}...) does not match the registered Polymarket wallet. Please switch accounts in MetaMask.`;
      resultEl.appendChild(d);
      return null;
    }

    // 3. Pick the token ID based on side
    const tokenId = side === 'yes' ? params.yes_token_id : params.no_token_id;
    const price = side === 'yes' ? params.yes_price : params.no_price;
    if (!tokenId) {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = 'Market has no CLOB token for this side';
      resultEl.appendChild(d);
      return null;
    }
    if (!price || price <= 0 || price >= 1) {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = 'Invalid market price';
      resultEl.appendChild(d);
      return null;
    }

    // 4. Compute maker/taker amounts for a BUY order
    let makerAmount, takerAmount;
    try {
      const amounts = _calcAmounts(amountUsdc, price);
      makerAmount = amounts.maker;
      takerAmount = amounts.taker;
    } catch (e) {
      resultEl.textContent = '';
      const d = document.createElement('div');
      d.className = 'hb-m-error';
      d.textContent = e.message || '';
      resultEl.appendChild(d);
      return null;
    }

    // 5. Build the EIP-712 typed data matching CTFExchange Order struct
    const salt = _randomSalt();
    const nonce = '0'; // onchain cancellation nonce — 0 unless user has cancelled nonces
    const expiration = '0'; // 0 = no expiry (GTC)
    const feeRateBps = String(params.fee_rate_bps || 0);
    const sideEnum = 0; // 0 = BUY, 1 = SELL. We only support BUY for now.
    const signatureType = 0; // 0 = EOA (externally owned account)

    const typedData = {
      types: {
        EIP712Domain: [
          { name: 'name', type: 'string' },
          { name: 'version', type: 'string' },
          { name: 'chainId', type: 'uint256' },
          { name: 'verifyingContract', type: 'address' },
        ],
        Order: [
          { name: 'salt', type: 'uint256' },
          { name: 'maker', type: 'address' },
          { name: 'signer', type: 'address' },
          { name: 'taker', type: 'address' },
          { name: 'tokenId', type: 'uint256' },
          { name: 'makerAmount', type: 'uint256' },
          { name: 'takerAmount', type: 'uint256' },
          { name: 'expiration', type: 'uint256' },
          { name: 'nonce', type: 'uint256' },
          { name: 'feeRateBps', type: 'uint256' },
          { name: 'side', type: 'uint8' },
          { name: 'signatureType', type: 'uint8' },
        ],
      },
      primaryType: 'Order',
      domain: {
        name: params.domain_name,
        version: params.domain_version,
        chainId: params.chain_id,
        verifyingContract: params.exchange,
      },
      message: {
        salt,
        maker: activeAddr,
        signer: activeAddr,
        taker: '0x0000000000000000000000000000000000000000', // 0x0 = any taker
        tokenId,
        makerAmount,
        takerAmount,
        expiration,
        nonce,
        feeRateBps,
        side: sideEnum,
        signatureType,
      },
    };

    // 6. Request signature from MetaMask
    resultEl.textContent = '';
    {
      const d = document.createElement('div');
      d.setAttribute('style', 'color:#a3a3a3;font-size:13px;padding:8px');
      d.textContent = 'Sign the order in your wallet...';
      resultEl.appendChild(d);
    }
    let signature;
    try {
      signature = await window.ethereum.request({
        method: 'eth_signTypedData_v4',
        params: [activeAddr, JSON.stringify(typedData)],
      });
    } catch (e) {
      resultEl.textContent = '';
      if (e.code === 4001) {
        const d = document.createElement('div');
        d.setAttribute('style', 'color:#a3a3a3;font-size:13px;padding:8px');
        d.textContent = 'Transaction cancelled';
        resultEl.appendChild(d);
      } else {
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = e.message || 'Signing failed';
        resultEl.appendChild(d);
      }
      return null;
    }

    // 7. Build the signed order payload matching the CTFExchange Order struct
    const signedOrder = {
      ...typedData.message,
      signature,
    };

    // 8. Submit to backend for CLOB forwarding
    resultEl.textContent = '';
    {
      const d = document.createElement('div');
      d.setAttribute('style', 'color:#a3a3a3;font-size:13px;padding:8px');
      d.textContent = 'Submitting to Polymarket...';
      resultEl.appendChild(d);
    }
    return await api('/api/markets/bet/polymarket', {
      method: 'POST',
      body: {
        market_id: marketId,
        side,
        amount_usdc: amountUsdc,
        owner: activeAddr,
        signed_order: signedOrder,
      },
    });
  }

  // ── Modal helpers ─────────────────────────────────────────────────────────
  function showModal(html) {
    closeModal();
    const overlay = document.createElement('div');
    overlay.className = 'hb-m-modal-overlay';
    overlay.id = 'hb-m-modal';
    overlay.innerHTML = `<div class="hb-m-modal">${html}</div>`;
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
  }

  function closeModal() {
    const m = $('#hb-m-modal');
    if (m) m.remove();
  }

  // ── Environmental impact: force-refresh ───────────────────────────────────
  // Pro-only. Hits POST /api/markets/{id}/environmental/refresh which is
  // rate-limited to 5/day per user server-side. On success, reopens the
  // detail panel so the freshly generated analysis is visible.
  async function refreshEnv(marketId) {
    const refreshBtn = document.querySelector('.hb-m-env-refresh');
    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = 'Refreshing…'; }
    try {
      const r = await fetch(`/api/markets/${encodeURIComponent(marketId)}/environmental/refresh`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (r.status === 429) {
        const data = await r.json().catch(() => ({}));
        alert(data.error || 'Force-refresh limit reached (5 per day).');
        return;
      }
      if (r.status === 403) {
        alert('Pro tier required to refresh environmental analysis.');
        return;
      }
      if (!r.ok) {
        alert(`Refresh failed (${r.status}).`);
        return;
      }
      // Reopen the detail panel to show the new analysis. The merge happens
      // inside GET /api/markets/unified/{id}.
      openDetail(marketId);
    } catch (e) {
      alert('Network error during refresh.');
    } finally {
      if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = 'Refresh analysis ↻'; }
    }
  }

  // ── Detail panel helpers ──────────────────────────────────────────────────
  window.__hbTrade = {
    openMarketsTab,
    closeMarketsTab,
    openDetail,
    openBetModal,
    connectKalshi,
    connectPolymarket,
    loadPortfolio,
    refreshEnv,
    goPage: (p) => { marketsPage = p; loadMarkets(); },
    _detailSide: 'yes',
    _detailMarket: null,
    _toggleSide: (btn, side) => {
      const toggle = btn.parentElement;
      toggle.querySelectorAll('.hb-m-side-btn').forEach(b => b.className = 'hb-m-side-btn');
      btn.className = 'hb-m-side-btn active-' + side;
      window.__hbTrade._detailSide = side;
      // Update summary
      const m = window.__hbTrade._detailMarket;
      if (m) {
        const amt = parseFloat($('#hb-detail-amount')?.value) || 0;
        const price = side === 'yes' ? m.yes_price : m.no_price;
        const payout = price > 0 ? (amt / price).toFixed(2) : '0';
        const summary = $('#hb-detail-summary');
        if (summary) summary.textContent = `Buying ${side.toUpperCase()} at ${pct(price)} — potential payout ~$${payout} if correct`;
      }
    },
    _placeBetFromDetail: async (marketId, source) => {
      const side = window.__hbTrade._detailSide;
      const amount = parseFloat($('#hb-detail-amount')?.value) || 0;
      const m = window.__hbTrade._detailMarket;
      if (!amount || amount <= 0) {
        const r = $('#hb-detail-bet-result');
        r.textContent = '';
        const d = document.createElement('div');
        d.className = 'hb-m-error';
        d.textContent = 'Enter a valid amount';
        r.appendChild(d);
        return;
      }
      openBetModal(marketId, m?.title || marketId, source, m?.yes_price || 0.5);
    },
  };

  // ── Init ───────────────────────────────────────────────────────────────────
  injectStyles();
  addMarketsButton();

  // Keyboard shortcut: Ctrl/Cmd + M to toggle markets
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'm') {
      e.preventDefault();
      const overlay = $('#hb-markets-overlay');
      if (overlay && overlay.classList.contains('open')) closeMarketsTab();
      else openMarketsTab();
    }
  });
})();
