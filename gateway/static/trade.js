/**
 * Habbig In-App Trading Panel
 * Gateway-injected trade overlay for Polymarket & Kalshi.
 *
 * Usage from any dashboard:
 *   window.hbTrade({ slug: "...", source: "polymarket"|"kalshi", question: "...", price: 0.65 })
 */
(function () {
  'use strict';

  /* ── URL builders ──────────────────────────────────────────── */
  function polyUrl(slug) { return 'https://polymarket.com/event/' + encodeURIComponent(slug); }
  function kalshiUrl(slug) { return 'https://kalshi.com/events/' + encodeURIComponent(slug); }

  /* ── Shadow DOM host ───────────────────────────────────────── */
  var host = document.createElement('div');
  host.id = 'hb-trade-host';
  host.style.cssText = 'all:initial;position:fixed;z-index:2147483646;top:0;right:0;width:0;height:0;pointer-events:none;';
  var root = host.attachShadow({ mode: 'closed' });

  /* ── Styles ────────────────────────────────────────────────── */
  var style = document.createElement('style');
  style.textContent = [
    "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');",
    ':host{all:initial;}',
    '*{box-sizing:border-box;margin:0;padding:0;}',

    /* overlay */
    '.tp-overlay{',
    '  position:fixed;top:0;left:0;right:0;bottom:0;',
    '  background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);',
    '  pointer-events:auto;opacity:0;transition:opacity .2s ease;',
    '}',
    '.tp-overlay.open{opacity:1;}',
    '.tp-overlay.closed{opacity:0;pointer-events:none;}',

    /* slide-in panel */
    '.tp-panel{',
    '  position:fixed;top:0;right:0;bottom:0;width:480px;max-width:100vw;',
    '  background:#0e1016;border-left:1px solid rgba(255,255,255,0.07);',
    '  font-family:"Inter",-apple-system,sans-serif;color:#e8e8e8;',
    '  display:flex;flex-direction:column;pointer-events:auto;',
    '  transform:translateX(100%);transition:transform .25s ease;',
    '  box-shadow:-8px 0 40px rgba(0,0,0,0.5);',
    '}',
    '.tp-panel.open{transform:translateX(0);}',

    /* header */
    '.tp-head{',
    '  padding:20px 24px 16px;border-bottom:1px solid rgba(255,255,255,0.06);',
    '  display:flex;align-items:flex-start;gap:12px;flex-shrink:0;',
    '}',
    '.tp-close{',
    '  width:32px;height:32px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);',
    '  background:transparent;color:rgba(255,255,255,0.5);cursor:pointer;',
    '  display:flex;align-items:center;justify-content:center;font-size:18px;',
    '  transition:background .15s,color .15s;flex-shrink:0;',
    '}',
    '.tp-close:hover{background:rgba(255,255,255,0.06);color:#fff;}',
    '.tp-title{font-size:15px;font-weight:600;line-height:1.4;flex:1;letter-spacing:-0.02em;}',
    '.tp-source{',
    '  display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;',
    '  padding:4px 10px;border-radius:6px;margin-top:6px;letter-spacing:0.3px;',
    '}',
    '.tp-source.poly{background:rgba(139,92,246,0.15);color:#a78bfa;}',
    '.tp-source.kalshi{background:rgba(59,130,246,0.15);color:#60a5fa;}',

    /* price bar */
    '.tp-price-bar{',
    '  padding:16px 24px;border-bottom:1px solid rgba(255,255,255,0.06);flex-shrink:0;',
    '}',
    '.tp-price-row{display:flex;align-items:center;gap:12px;margin-bottom:8px;}',
    '.tp-price-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:rgba(255,255,255,0.35);width:36px;}',
    '.tp-price-track{flex:1;height:28px;background:rgba(255,255,255,0.04);border-radius:6px;overflow:hidden;position:relative;}',
    '.tp-price-fill{height:100%;border-radius:6px;display:flex;align-items:center;padding:0 10px;font-size:12px;font-weight:600;color:#fff;min-width:40px;transition:width .3s ease;}',
    '.tp-price-fill.yes{background:linear-gradient(90deg,#059669,#34d399);}',
    '.tp-price-fill.no{background:linear-gradient(90deg,#dc2626,#f87171);}',

    /* trade buttons */
    '.tp-actions{padding:16px 24px;display:flex;gap:10px;flex-shrink:0;}',
    '.tp-btn{',
    '  flex:1;padding:14px 16px;border-radius:10px;border:none;cursor:pointer;',
    '  font-family:inherit;font-size:14px;font-weight:600;letter-spacing:-0.01em;',
    '  display:flex;align-items:center;justify-content:center;gap:8px;',
    '  transition:transform .12s ease,box-shadow .12s ease;',
    '}',
    '.tp-btn:hover{transform:translateY(-1px);}',
    '.tp-btn:active{transform:translateY(0);}',
    '.tp-btn-yes{background:linear-gradient(135deg,#059669,#34d399);color:#fff;box-shadow:0 4px 16px rgba(5,150,105,0.3);}',
    '.tp-btn-yes:hover{box-shadow:0 6px 24px rgba(5,150,105,0.45);}',
    '.tp-btn-no{background:linear-gradient(135deg,#dc2626,#f87171);color:#fff;box-shadow:0 4px 16px rgba(220,38,38,0.3);}',
    '.tp-btn-no:hover{box-shadow:0 6px 24px rgba(220,38,38,0.45);}',

    /* iframe area */
    '.tp-iframe-wrap{flex:1;overflow:hidden;position:relative;}',
    '.tp-iframe{width:100%;height:100%;border:none;}',
    '.tp-iframe-fallback{',
    '  display:flex;flex-direction:column;align-items:center;justify-content:center;',
    '  height:100%;padding:32px;text-align:center;',
    '}',
    '.tp-fallback-icon{font-size:48px;margin-bottom:16px;opacity:0.3;}',
    '.tp-fallback-text{font-size:13px;color:rgba(255,255,255,0.4);line-height:1.6;margin-bottom:20px;}',
    '.tp-open-btn{',
    '  display:inline-flex;align-items:center;gap:8px;padding:12px 24px;',
    '  border-radius:10px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.06);',
    '  color:#fff;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;',
    '  text-decoration:none;transition:background .15s,border-color .15s;',
    '}',
    '.tp-open-btn:hover{background:rgba(255,255,255,0.1);border-color:rgba(255,255,255,0.2);}',
    '.tp-open-btn.primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);border:none;}',
    '.tp-open-btn.primary:hover{box-shadow:0 4px 16px rgba(99,102,241,0.35);}',

    /* tabs for switching between platforms */
    '.tp-tabs{display:flex;gap:4px;padding:12px 24px 0;flex-shrink:0;}',
    '.tp-tab{',
    '  padding:8px 16px;border-radius:8px;border:none;cursor:pointer;',
    '  font-family:inherit;font-size:12px;font-weight:600;letter-spacing:0.3px;',
    '  background:transparent;color:rgba(255,255,255,0.35);',
    '  transition:background .15s,color .15s;',
    '}',
    '.tp-tab:hover{color:rgba(255,255,255,0.6);}',
    '.tp-tab.active{background:rgba(255,255,255,0.08);color:#fff;}',

    /* footer */
    '.tp-footer{',
    '  padding:12px 24px;border-top:1px solid rgba(255,255,255,0.06);flex-shrink:0;',
    '  font-size:11px;color:rgba(255,255,255,0.25);text-align:center;',
    '}',

    '@media(max-width:520px){',
    '  .tp-panel{width:100vw;}',
    '}',
  ].join('\n');
  root.appendChild(style);

  /* ── Build DOM ─────────────────────────────────────────────── */
  var overlay = document.createElement('div');
  overlay.className = 'tp-overlay closed';

  var panel = document.createElement('div');
  panel.className = 'tp-panel';

  // Header
  var head = document.createElement('div');
  head.className = 'tp-head';

  var closeBtn = document.createElement('button');
  closeBtn.className = 'tp-close';
  closeBtn.innerHTML = '&times;';
  closeBtn.onclick = closeTrade;

  var titleWrap = document.createElement('div');
  titleWrap.style.cssText = 'flex:1;min-width:0;';
  var titleEl = document.createElement('div');
  titleEl.className = 'tp-title';
  var sourceEl = document.createElement('div');
  sourceEl.className = 'tp-source';
  titleWrap.appendChild(titleEl);
  titleWrap.appendChild(sourceEl);
  head.appendChild(titleWrap);
  head.appendChild(closeBtn);
  panel.appendChild(head);

  // Price bar
  var priceBar = document.createElement('div');
  priceBar.className = 'tp-price-bar';
  panel.appendChild(priceBar);

  // Trade buttons
  var actions = document.createElement('div');
  actions.className = 'tp-actions';
  var btnYes = document.createElement('a');
  btnYes.className = 'tp-btn tp-btn-yes';
  btnYes.target = '_blank';
  btnYes.rel = 'noopener';
  btnYes.textContent = 'Buy Yes';
  var btnNo = document.createElement('a');
  btnNo.className = 'tp-btn tp-btn-no';
  btnNo.target = '_blank';
  btnNo.rel = 'noopener';
  btnNo.textContent = 'Buy No';
  actions.appendChild(btnYes);
  actions.appendChild(btnNo);
  panel.appendChild(actions);

  // Tabs (Polymarket / Kalshi)
  var tabs = document.createElement('div');
  tabs.className = 'tp-tabs';
  var tabPoly = document.createElement('button');
  tabPoly.className = 'tp-tab';
  tabPoly.textContent = 'Polymarket';
  var tabKalshi = document.createElement('button');
  tabKalshi.className = 'tp-tab';
  tabKalshi.textContent = 'Kalshi';
  tabs.appendChild(tabPoly);
  tabs.appendChild(tabKalshi);
  panel.appendChild(tabs);

  // Content area (iframe or fallback)
  var iframeWrap = document.createElement('div');
  iframeWrap.className = 'tp-iframe-wrap';
  panel.appendChild(iframeWrap);

  // Footer
  var footer = document.createElement('div');
  footer.className = 'tp-footer';
  footer.textContent = 'Trading is executed on the respective platform. Habbig does not hold funds.';
  panel.appendChild(footer);

  overlay.appendChild(panel);
  root.appendChild(overlay);

  /* ── State ─────────────────────────────────────────────────── */
  var currentMarket = null;
  var activeTab = null;

  function closeTrade() {
    overlay.className = 'tp-overlay closed';
    panel.className = 'tp-panel';
    iframeWrap.innerHTML = '';
    currentMarket = null;
  }

  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) closeTrade();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && currentMarket) closeTrade();
  });

  function switchTab(tab) {
    activeTab = tab;
    tabPoly.className = 'tp-tab' + (tab === 'polymarket' ? ' active' : '');
    tabKalshi.className = 'tp-tab' + (tab === 'kalshi' ? ' active' : '');

    var slug = currentMarket ? currentMarket.slug : '';
    var url = tab === 'kalshi' ? kalshiUrl(currentMarket.kalshi_slug || slug) : polyUrl(slug);

    // Update buy buttons
    btnYes.href = url;
    btnNo.href = url;

    // Kalshi allows iframe embedding; Polymarket does not
    iframeWrap.innerHTML = '';
    if (tab === 'kalshi') {
      var iframe = document.createElement('iframe');
      iframe.className = 'tp-iframe';
      iframe.src = kalshiUrl(currentMarket.kalshi_slug || slug);
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups');
      iframeWrap.appendChild(iframe);
    } else {
      // Polymarket blocks iframes — show fallback with deep link
      var fb = document.createElement('div');
      fb.className = 'tp-iframe-fallback';
      fb.innerHTML = '<div class="tp-fallback-icon">P</div>'
        + '<div class="tp-fallback-text">Polymarket opens in a trading window so you can<br>trade with your existing wallet.</div>'
        + '<a href="' + polyUrl(slug) + '" target="_blank" rel="noopener" class="tp-open-btn primary">Open Polymarket &rarr;</a>';
      iframeWrap.appendChild(fb);
    }
  }

  tabPoly.onclick = function () { switchTab('polymarket'); };
  tabKalshi.onclick = function () { switchTab('kalshi'); };

  /* ── Public API ────────────────────────────────────────────── */
  /**
   * Open the trade panel.
   * @param {Object} opts
   * @param {string} opts.slug        - Market slug (Polymarket event slug)
   * @param {string} [opts.kalshi_slug] - Kalshi event slug (if different)
   * @param {string} [opts.source]    - "polymarket" or "kalshi" (default tab)
   * @param {string} [opts.question]  - Market question text
   * @param {number} [opts.price]     - Current YES price (0-1)
   * @param {number} [opts.volume]    - Volume in USD
   */
  window.hbTrade = function (opts) {
    if (!opts || !opts.slug) return;
    currentMarket = opts;

    // Title
    titleEl.textContent = opts.question || opts.slug;

    // Source badge
    var src = (opts.source || 'polymarket').toLowerCase();
    sourceEl.className = 'tp-source ' + (src === 'kalshi' ? 'kalshi' : 'poly');
    sourceEl.textContent = src === 'kalshi' ? 'Kalshi' : 'Polymarket';

    // Price bars
    var yesP = opts.price != null ? Math.round(opts.price * 100) : null;
    var noP = yesP != null ? 100 - yesP : null;
    if (yesP != null) {
      priceBar.innerHTML = ''
        + '<div class="tp-price-row"><span class="tp-price-label">Yes</span>'
        + '<div class="tp-price-track"><div class="tp-price-fill yes" style="width:' + Math.max(yesP, 8) + '%">' + yesP + '%</div></div></div>'
        + '<div class="tp-price-row"><span class="tp-price-label">No</span>'
        + '<div class="tp-price-track"><div class="tp-price-fill no" style="width:' + Math.max(noP, 8) + '%">' + noP + '%</div></div></div>';
      if (opts.volume) {
        var vol = opts.volume >= 1000000 ? (opts.volume / 1000000).toFixed(1) + 'M' : opts.volume >= 1000 ? (opts.volume / 1000).toFixed(0) + 'k' : opts.volume;
        priceBar.innerHTML += '<div style="font-size:11px;color:rgba(255,255,255,0.3);margin-top:8px">Volume: $' + vol + '</div>';
      }
      priceBar.style.display = '';
    } else {
      priceBar.style.display = 'none';
    }

    // Show both tabs, default to the market's source
    tabs.style.display = 'flex';
    switchTab(src);

    // Open
    overlay.className = 'tp-overlay open';
    setTimeout(function () { panel.className = 'tp-panel open'; }, 10);
  };

  /* ── Mount ─────────────────────────────────────────────────── */
  document.body.appendChild(host);
})();
