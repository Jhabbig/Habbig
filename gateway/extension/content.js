/**
 * content.js — Injected into polymarket.com/event/* pages.
 *
 * 1. Detects the market slug from the URL
 * 2. Requests narve.ai data from the background service worker
 * 3. Injects an overlay panel into the Polymarket page
 * 4. Watches for SPA navigation and updates the overlay when the market changes
 */

(function () {
  'use strict';

  let currentSlug = null;
  let overlayVisible = true;

  // ── Slug extraction ──────────────────────────────────────────────
  function getSlugFromUrl() {
    const parts = window.location.pathname.split('/');
    const idx = parts.indexOf('event');
    return idx >= 0 && parts[idx + 1] ? parts[idx + 1] : null;
  }

  // ── API call via background service worker ───────────────────────
  function fetchNarveData(slug) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: 'FETCH_MARKET_DATA', marketSlug: slug }, resolve);
    });
  }

  // ── HTML builder ─────────────────────────────────────────────────
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]);
  }

  function buildOverlay(data) {
    if (!data || data.error === 'not_authenticated') {
      return `
        <div class="narve-panel narve-unauthenticated">
          <div class="narve-header">
            <span class="narve-logo">narve.ai</span>
            <button class="narve-close" id="narve-close">&times;</button>
          </div>
          <p class="narve-msg">Sign in to narve.ai to see intelligence for this market.</p>
          <a href="https://narve.ai/token" target="_blank" class="narve-btn">Sign in</a>
        </div>`;
    }

    if (data.error === 'no_data') {
      return `
        <div class="narve-panel narve-no-data">
          <div class="narve-header">
            <span class="narve-logo">narve.ai</span>
            <button class="narve-close" id="narve-close">&times;</button>
          </div>
          <p class="narve-msg">No predictions tracked for this market yet.</p>
        </div>`;
    }

    if (data.error === 'tier_required') {
      return `
        <div class="narve-panel narve-unauthenticated">
          <div class="narve-header">
            <span class="narve-logo">narve.ai</span>
            <button class="narve-close" id="narve-close">&times;</button>
          </div>
          <p class="narve-msg">Trader tier or above required for extension data.</p>
          <a href="https://narve.ai/pricing" target="_blank" class="narve-btn">Upgrade</a>
        </div>`;
    }

    if (data.error) {
      return `
        <div class="narve-panel narve-no-data">
          <div class="narve-header">
            <span class="narve-logo">narve.ai</span>
            <button class="narve-close" id="narve-close">&times;</button>
          </div>
          <p class="narve-msg">Could not load data. Try refreshing.</p>
        </div>`;
    }

    const yesP = data.betyc_yes_probability;
    const mktP = data.market_yes_price;
    const edge = data.betyc_edge;
    const edgeSign = edge != null && edge > 0 ? '+' : '';
    const edgeClass = edge != null ? (edge > 0.05 ? 'positive' : edge < -0.05 ? 'negative' : 'neutral') : 'neutral';
    const conf = esc(data.betyc_confidence || 'Unknown');
    const confClass = conf.toLowerCase().replace(/\s+/g, '-');
    const srcCount = data.source_count || 0;
    const topSources = (data.top_sources || []).slice(0, 3);
    const insiders = (data.insider_signals || []).slice(0, 2);

    let html = `
      <div class="narve-panel">
        <div class="narve-header">
          <span class="narve-logo">narve.ai</span>
          <span class="narve-confidence narve-conf-${confClass}">${conf}</span>
          <button class="narve-close" id="narve-close">&times;</button>
        </div>

        <div class="narve-consensus">
          <div class="narve-prob-row">
            <div class="narve-prob-item">
              <div class="narve-prob-label">Market</div>
              <div class="narve-prob-value">${mktP != null ? Math.round(mktP * 100) + '%' : '—'}</div>
            </div>
            <div class="narve-arrow">&rarr;</div>
            <div class="narve-prob-item narve-primary">
              <div class="narve-prob-label">narve.ai</div>
              <div class="narve-prob-value">${yesP != null ? Math.round(yesP * 100) + '%' : '—'}</div>
            </div>
            ${edge != null ? `<div class="narve-edge narve-edge-${edgeClass}">${edgeSign}${Math.round(edge * 100)}pp</div>` : ''}
          </div>
          ${data.risk_flag ? '<div class="narve-risk-flag">Large edge detected — see narve.ai for full analysis</div>' : ''}
        </div>`;

    if (srcCount > 0) {
      html += `
        <div class="narve-sources">
          <div class="narve-sources-header">${srcCount} source${srcCount !== 1 ? 's' : ''} tracked</div>
          ${topSources.map(s => `
            <div class="narve-source">
              <span class="narve-source-handle">@${esc(s.handle)}</span>
              <span class="narve-source-cred">${s.credibility != null ? s.credibility.toFixed(2) : '—'}</span>
              <span class="narve-source-pred">${esc(s.predicted_outcome)}${s.predicted_probability != null ? ' (' + Math.round(s.predicted_probability * 100) + '%)' : ''}</span>
            </div>
          `).join('')}
        </div>`;
    }

    if (insiders.length > 0) {
      html += `
        <div class="narve-insider">
          <div class="narve-insider-header">Insider signals (${insiders.length})</div>
          ${insiders.map(s => `<div class="narve-insider-item">${esc(s.source_name || s.source_handle || '?')}: ${esc(s.action || s.signal_type || '?')} ${esc(s.asset_or_entity || s.market_id || '')}</div>`).join('')}
        </div>`;
    }

    html += `
        <div class="narve-footer">
          <a href="https://narve.ai/dashboards" target="_blank">Full analysis on narve.ai &rarr;</a>
        </div>
      </div>`;
    return html;
  }

  // ── Injection ────────────────────────────────────────────────────
  function injectOverlay(data) {
    const existing = document.getElementById('narve-overlay');
    if (existing) existing.remove();

    if (!overlayVisible) return;

    const el = document.createElement('div');
    el.id = 'narve-overlay';
    el.innerHTML = buildOverlay(data);

    // Close button
    const closeBtn = el.querySelector('#narve-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => {
        el.remove();
        overlayVisible = false;
      });
    }

    // Find best injection point — try multiple selectors since Polymarket's
    // DOM may change across deploys. Worst case: append to body.
    const target =
      document.querySelector('[data-testid="market-header"]') ||
      document.querySelector('.c-dhzjXW.c-dhzjXW-ibKdHzw-css') ||  // known Polymarket header class
      document.querySelector('main > div > div:first-child') ||
      document.querySelector('main');

    if (target) {
      target.insertAdjacentElement('afterend', el);
    } else {
      document.body.appendChild(el);
    }
  }

  // ── SPA navigation observer ──────────────────────────────────────
  async function onSlugChange() {
    const slug = getSlugFromUrl();
    if (!slug || slug === currentSlug) return;
    currentSlug = slug;
    overlayVisible = true;
    const data = await fetchNarveData(slug);
    injectOverlay(data);
  }

  // Polymarket is a React SPA — URL changes don't trigger full page loads.
  // Watch for pushState/replaceState and popstate events.
  const _pushState = history.pushState;
  const _replaceState = history.replaceState;
  history.pushState = function () {
    _pushState.apply(this, arguments);
    setTimeout(onSlugChange, 100);
  };
  history.replaceState = function () {
    _replaceState.apply(this, arguments);
    setTimeout(onSlugChange, 100);
  };
  window.addEventListener('popstate', () => setTimeout(onSlugChange, 100));

  // Also observe DOM mutations as a backup (some SPA frameworks change DOM
  // without triggering history events, especially on initial hydration).
  const domObserver = new MutationObserver(() => {
    const slug = getSlugFromUrl();
    if (slug && slug !== currentSlug) {
      onSlugChange();
    }
  });
  domObserver.observe(document.body, { childList: true, subtree: true });

  // Listen for auth tokens sent by the /extension/auth page via postMessage
  window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'NARVE_EXT_AUTH' && event.data.jwt) {
      chrome.runtime.sendMessage({
        type: 'SAVE_AUTH',
        jwt: event.data.jwt,
        display_name: event.data.display_name || '',
        tier: event.data.tier || '',
      });
      // Refresh the overlay now that we're authenticated
      currentSlug = null;
      onSlugChange();
    }
  });

  // ── Initial load ─────────────────────────────────────────────────
  onSlugChange();
})();
