/**
 * narve.ai In-App Trading Panel
 * Gateway-injected trade overlay for Polymarket & Kalshi.
 *
 * Usage from any dashboard:
 *   window.hbTrade({
 *     slug: "...",
 *     source: "polymarket"|"kalshi",
 *     question: "...",
 *     price: 0.65,
 *     token_id: "...",        // Polymarket CLOB token ID (required for PM trades)
 *     kalshi_ticker: "...",   // Kalshi event ticker
 *     volume: 123456,
 *   })
 */
(function () {
  'use strict';

  /* ── CSRF token (provided by the gateway switcher injection) ── */
  function csrfToken() {
    return (window.__hbSwitcher && window.__hbSwitcher.csrf_token) || '';
  }

  /* ── HTML escaping ─────────────────────────────────────────── */
  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }

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
    'input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0;}',
    'input[type=number]{-moz-appearance:textfield;}',

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
    '  position:fixed;top:0;right:0;bottom:0;width:440px;max-width:100vw;',
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

    /* side selector */
    '.tp-side-selector{display:flex;gap:8px;padding:16px 24px;flex-shrink:0;}',
    '.tp-side-btn{',
    '  flex:1;padding:14px 16px;border-radius:10px;border:2px solid rgba(255,255,255,0.08);',
    '  cursor:pointer;font-family:inherit;font-size:14px;font-weight:600;',
    '  display:flex;flex-direction:column;align-items:center;gap:4px;',
    '  transition:all .15s ease;background:transparent;color:rgba(255,255,255,0.5);',
    '}',
    '.tp-side-btn:hover{border-color:rgba(255,255,255,0.15);color:rgba(255,255,255,0.8);}',
    '.tp-side-btn.active-yes{border-color:#059669;background:rgba(5,150,105,0.1);color:#34d399;}',
    '.tp-side-btn.active-no{border-color:#dc2626;background:rgba(220,38,38,0.1);color:#f87171;}',
    '.tp-side-price{font-size:20px;font-weight:700;letter-spacing:-0.03em;}',
    '.tp-side-label{font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.7;}',

    /* order form */
    '.tp-form{padding:0 24px 16px;flex-shrink:0;}',
    '.tp-form-row{margin-bottom:14px;}',
    '.tp-form-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:rgba(255,255,255,0.35);margin-bottom:6px;display:block;}',
    '.tp-form-input{',
    '  width:100%;padding:12px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);',
    '  background:rgba(255,255,255,0.04);color:#fff;font-family:inherit;font-size:15px;',
    '  font-weight:600;transition:border-color .15s;',
    '}',
    '.tp-form-input:focus{outline:none;border-color:rgba(255,255,255,0.25);}',
    '.tp-form-input::placeholder{color:rgba(255,255,255,0.2);}',
    '.tp-form-hint{font-size:11px;color:rgba(255,255,255,0.25);margin-top:4px;}',

    /* summary */
    '.tp-summary{',
    '  margin:0 24px 16px;padding:14px 16px;border-radius:10px;',
    '  background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);',
    '  font-size:13px;flex-shrink:0;',
    '}',
    '.tp-summary-row{display:flex;justify-content:space-between;margin-bottom:6px;}',
    '.tp-summary-row:last-child{margin-bottom:0;padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);font-weight:600;}',
    '.tp-summary-label{color:rgba(255,255,255,0.4);}',
    '.tp-summary-value{color:#fff;font-weight:500;}',

    /* submit */
    '.tp-submit{',
    '  margin:0 24px 16px;padding:16px;border-radius:12px;border:none;cursor:pointer;',
    '  font-family:inherit;font-size:15px;font-weight:700;letter-spacing:-0.01em;',
    '  color:#fff;transition:transform .12s ease,box-shadow .12s ease,opacity .15s;',
    '  display:flex;align-items:center;justify-content:center;gap:8px;width:calc(100% - 48px);',
    '}',
    '.tp-submit:hover{transform:translateY(-1px);}',
    '.tp-submit:active{transform:translateY(0);}',
    '.tp-submit:disabled{opacity:0.5;cursor:not-allowed;transform:none;}',
    '.tp-submit.buy-yes{background:linear-gradient(135deg,#059669,#34d399);box-shadow:0 4px 16px rgba(5,150,105,0.3);}',
    '.tp-submit.buy-no{background:linear-gradient(135deg,#dc2626,#f87171);box-shadow:0 4px 16px rgba(220,38,38,0.3);}',

    /* status messages */
    '.tp-status{',
    '  margin:0 24px 16px;padding:14px 16px;border-radius:10px;font-size:13px;',
    '  display:none;animation:tpFadeIn .2s ease;',
    '}',
    '.tp-status.success{display:block;background:rgba(5,150,105,0.12);border:1px solid rgba(5,150,105,0.3);color:#34d399;}',
    '.tp-status.error{display:block;background:rgba(220,38,38,0.12);border:1px solid rgba(220,38,38,0.3);color:#f87171;}',
    '.tp-status.pending{display:block;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.3);color:#fbbf24;}',
    '@keyframes tpFadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}',

    /* credential warning */
    '.tp-cred-warning{',
    '  margin:0 24px 16px;padding:16px;border-radius:10px;text-align:center;',
    '  background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2);',
    '  font-size:13px;color:rgba(255,255,255,0.6);flex-shrink:0;',
    '}',
    '.tp-cred-link{',
    '  display:inline-block;margin-top:10px;padding:8px 20px;border-radius:8px;',
    '  background:rgba(255,255,255,0.08);color:#fff;text-decoration:none;font-weight:600;font-size:12px;',
    '  transition:background .15s;',
    '}',
    '.tp-cred-link:hover{background:rgba(255,255,255,0.14);}',

    /* tabs */
    '.tp-tabs{display:flex;gap:4px;padding:12px 24px 0;flex-shrink:0;}',
    '.tp-tab{',
    '  padding:8px 16px;border-radius:8px;border:none;cursor:pointer;',
    '  font-family:inherit;font-size:12px;font-weight:600;letter-spacing:0.3px;',
    '  background:transparent;color:rgba(255,255,255,0.35);',
    '  transition:background .15s,color .15s;',
    '}',
    '.tp-tab:hover{color:rgba(255,255,255,0.6);}',
    '.tp-tab.active{background:rgba(255,255,255,0.08);color:#fff;}',

    /* order history */
    '.tp-history{flex:1;overflow-y:auto;padding:16px 24px;}',
    '.tp-history-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:rgba(255,255,255,0.3);margin-bottom:12px;}',
    '.tp-order{',
    '  padding:12px;border-radius:8px;background:rgba(255,255,255,0.03);',
    '  border:1px solid rgba(255,255,255,0.06);margin-bottom:8px;font-size:12px;',
    '}',
    '.tp-order-head{display:flex;justify-content:space-between;margin-bottom:4px;}',
    '.tp-order-side{font-weight:600;}',
    '.tp-order-side.yes{color:#34d399;}',
    '.tp-order-side.no{color:#f87171;}',
    '.tp-order-status{font-size:11px;padding:2px 8px;border-radius:4px;}',
    '.tp-order-status.filled,.tp-order-status.submitted{background:rgba(5,150,105,0.15);color:#34d399;}',
    '.tp-order-status.pending{background:rgba(245,158,11,0.15);color:#fbbf24;}',
    '.tp-order-status.error{background:rgba(220,38,38,0.15);color:#f87171;}',
    '.tp-order-q{color:rgba(255,255,255,0.4);margin-bottom:4px;line-height:1.4;}',
    '.tp-order-meta{color:rgba(255,255,255,0.3);}',
    '.tp-no-orders{color:rgba(255,255,255,0.2);text-align:center;padding:32px 0;font-size:13px;}',

    /* external link */
    '.tp-external{',
    '  display:flex;align-items:center;justify-content:center;gap:6px;',
    '  padding:10px 24px;font-size:12px;color:rgba(255,255,255,0.3);',
    '  border-top:1px solid rgba(255,255,255,0.06);flex-shrink:0;',
    '}',
    '.tp-external a{color:rgba(255,255,255,0.5);text-decoration:none;font-weight:500;}',
    '.tp-external a:hover{color:#fff;}',

    /* footer */
    '.tp-footer{',
    '  padding:12px 24px;border-top:1px solid rgba(255,255,255,0.06);flex-shrink:0;',
    '  font-size:11px;color:rgba(255,255,255,0.2);text-align:center;',
    '}',

    /* spinner */
    '.tp-spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;animation:tpSpin .6s linear infinite;}',
    '@keyframes tpSpin{to{transform:rotate(360deg)}}',

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

  // Tabs
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

  // Price bar
  var priceBar = document.createElement('div');
  priceBar.className = 'tp-price-bar';
  panel.appendChild(priceBar);

  // Side selector (Yes / No)
  var sideSelector = document.createElement('div');
  sideSelector.className = 'tp-side-selector';
  var btnYesSide = document.createElement('button');
  btnYesSide.className = 'tp-side-btn active-yes';
  var btnNoSide = document.createElement('button');
  btnNoSide.className = 'tp-side-btn';
  sideSelector.appendChild(btnYesSide);
  sideSelector.appendChild(btnNoSide);
  panel.appendChild(sideSelector);

  // Credential warning (hidden by default)
  var credWarning = document.createElement('div');
  credWarning.className = 'tp-cred-warning';
  credWarning.style.display = 'none';
  panel.appendChild(credWarning);

  // Order form
  var formWrap = document.createElement('div');
  formWrap.className = 'tp-form';

  var amountRow = document.createElement('div');
  amountRow.className = 'tp-form-row';
  var amountLabel = document.createElement('label');
  amountLabel.className = 'tp-form-label';
  amountLabel.textContent = 'Amount (USD)';
  var amountInput = document.createElement('input');
  amountInput.className = 'tp-form-input';
  amountInput.type = 'number';
  amountInput.placeholder = '10.00';
  amountInput.min = '0.10';
  amountInput.max = '10000';
  amountInput.step = '0.01';
  var amountHint = document.createElement('div');
  amountHint.className = 'tp-form-hint';
  amountRow.appendChild(amountLabel);
  amountRow.appendChild(amountInput);
  amountRow.appendChild(amountHint);
  formWrap.appendChild(amountRow);

  var priceRow = document.createElement('div');
  priceRow.className = 'tp-form-row';
  var priceLabel = document.createElement('label');
  priceLabel.className = 'tp-form-label';
  priceLabel.textContent = 'Limit Price';
  var priceInput = document.createElement('input');
  priceInput.className = 'tp-form-input';
  priceInput.type = 'number';
  priceInput.placeholder = '0.65';
  priceInput.min = '0.01';
  priceInput.max = '0.99';
  priceInput.step = '0.01';
  var priceHint = document.createElement('div');
  priceHint.className = 'tp-form-hint';
  priceHint.textContent = 'Price per share ($0.01 - $0.99)';
  priceRow.appendChild(priceLabel);
  priceRow.appendChild(priceInput);
  priceRow.appendChild(priceHint);
  formWrap.appendChild(priceRow);

  panel.appendChild(formWrap);

  // Summary
  var summary = document.createElement('div');
  summary.className = 'tp-summary';
  panel.appendChild(summary);

  // Status message
  var statusEl = document.createElement('div');
  statusEl.className = 'tp-status';
  panel.appendChild(statusEl);

  // Submit button
  var submitBtn = document.createElement('button');
  submitBtn.className = 'tp-submit buy-yes';
  submitBtn.textContent = 'Buy Yes';
  panel.appendChild(submitBtn);

  // External link
  var externalLink = document.createElement('div');
  externalLink.className = 'tp-external';
  panel.appendChild(externalLink);

  // Order history
  var historyWrap = document.createElement('div');
  historyWrap.className = 'tp-history';
  panel.appendChild(historyWrap);

  // Footer
  var footer = document.createElement('div');
  footer.className = 'tp-footer';
  footer.textContent = 'Orders execute on the respective platform. narve.ai does not custody funds.';
  panel.appendChild(footer);

  overlay.appendChild(panel);
  root.appendChild(overlay);

  /* ── State ─────────────────────────────────────────────────── */
  var currentMarket = null;
  var activeTab = null;
  var selectedSide = 'yes';
  var credStatus = { polymarket: false, kalshi: false };
  var submitting = false;

  function closeTrade() {
    overlay.className = 'tp-overlay closed';
    panel.className = 'tp-panel';
    currentMarket = null;
    statusEl.className = 'tp-status';
    statusEl.textContent = '';
    submitting = false;
  }

  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) closeTrade();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && currentMarket) closeTrade();
  });

  /* ── Credential check ─────────────────────────────────────── */
  function checkCredentials() {
    fetch('/api/trading/credentials', { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); })
      .then(function (d) { credStatus = d; })
      .catch(function () {});
  }
  checkCredentials();

  /* ── Side selector logic ──────────────────────────────────── */
  function selectSide(side) {
    selectedSide = side;
    if (side === 'yes') {
      btnYesSide.className = 'tp-side-btn active-yes';
      btnNoSide.className = 'tp-side-btn';
      submitBtn.className = 'tp-submit buy-yes';
      submitBtn.textContent = 'Buy Yes';
    } else {
      btnYesSide.className = 'tp-side-btn';
      btnNoSide.className = 'tp-side-btn active-no';
      submitBtn.className = 'tp-submit buy-no';
      submitBtn.textContent = 'Buy No';
    }
    updateSummary();
  }

  btnYesSide.onclick = function () { selectSide('yes'); };
  btnNoSide.onclick = function () { selectSide('no'); };

  /* ── Summary update ───────────────────────────────────────── */
  function updateSummary() {
    var amt = parseFloat(amountInput.value) || 0;
    var prc = parseFloat(priceInput.value) || 0;
    if (amt <= 0 || prc <= 0 || prc >= 1) {
      summary.innerHTML = '<div style="color:rgba(255,255,255,0.25);text-align:center;font-size:12px">Enter amount and price to see order summary</div>';
      return;
    }
    var shares = amt / prc;
    var payout = shares; // each share pays $1 if correct
    var profit = payout - amt;
    summary.innerHTML = ''
      + '<div class="tp-summary-row"><span class="tp-summary-label">Side</span><span class="tp-summary-value" style="color:' + (selectedSide === 'yes' ? '#34d399' : '#f87171') + '">' + selectedSide.toUpperCase() + ' @ $' + prc.toFixed(2) + '</span></div>'
      + '<div class="tp-summary-row"><span class="tp-summary-label">Shares</span><span class="tp-summary-value">' + shares.toFixed(2) + '</span></div>'
      + '<div class="tp-summary-row"><span class="tp-summary-label">Cost</span><span class="tp-summary-value">$' + amt.toFixed(2) + '</span></div>'
      + '<div class="tp-summary-row"><span class="tp-summary-label">Payout if correct</span><span class="tp-summary-value" style="color:#34d399">$' + payout.toFixed(2) + ' (+$' + profit.toFixed(2) + ')</span></div>';
  }

  amountInput.addEventListener('input', updateSummary);
  priceInput.addEventListener('input', updateSummary);

  /* ── Tab switching ────────────────────────────────────────── */
  function switchTab(tab) {
    activeTab = tab;
    tabPoly.className = 'tp-tab' + (tab === 'polymarket' ? ' active' : '');
    tabKalshi.className = 'tp-tab' + (tab === 'kalshi' ? ' active' : '');

    // Update source badge
    sourceEl.className = 'tp-source ' + (tab === 'kalshi' ? 'kalshi' : 'poly');
    sourceEl.textContent = tab === 'kalshi' ? 'Kalshi' : 'Polymarket';

    // External link
    var slug = currentMarket ? currentMarket.slug : '';
    var url = tab === 'kalshi' ? kalshiUrl(currentMarket.kalshi_ticker || slug) : polyUrl(slug);
    externalLink.innerHTML = '<span>Also trade on</span> <a href="' + url + '" target="_blank" rel="noopener">' + (tab === 'kalshi' ? 'Kalshi' : 'Polymarket') + ' &nearr;</a>';

    // Check credentials for selected platform
    updateCredentialWarning();
  }

  function updateCredentialWarning() {
    var hasCreds = credStatus[activeTab];
    if (hasCreds) {
      credWarning.style.display = 'none';
      formWrap.style.display = '';
      summary.style.display = '';
      submitBtn.style.display = '';
    } else {
      credWarning.style.display = '';
      credWarning.innerHTML = '<div style="font-size:20px;margin-bottom:8px;opacity:0.3">' + (activeTab === 'kalshi' ? 'K' : 'P') + '</div>'
        + '<div>Connect your <strong>' + (activeTab === 'kalshi' ? 'Kalshi' : 'Polymarket') + '</strong> account to trade directly.</div>'
        + '<a href="/settings#trading" class="tp-cred-link">Add API Keys &rarr;</a>';
      formWrap.style.display = 'none';
      summary.style.display = 'none';
      submitBtn.style.display = 'none';
    }
  }

  tabPoly.onclick = function () { switchTab('polymarket'); };
  tabKalshi.onclick = function () { switchTab('kalshi'); };

  /* ── Submit order ─────────────────────────────────────────── */
  submitBtn.onclick = function () {
    if (submitting) return;
    var amt = parseFloat(amountInput.value) || 0;
    var prc = parseFloat(priceInput.value) || 0;

    if (amt < 0.10) { showStatus('error', 'Minimum amount is $0.10'); return; }
    if (amt > 10000) { showStatus('error', 'Maximum amount is $10,000'); return; }
    if (prc <= 0 || prc >= 1) { showStatus('error', 'Price must be between $0.01 and $0.99'); return; }

    submitting = true;
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="tp-spinner"></span> Placing order...';
    statusEl.className = 'tp-status';

    var payload = {
      platform: activeTab,
      slug: currentMarket.slug || '',
      token_id: selectedSide === 'yes' ? (currentMarket.token_id || '') : (currentMarket.token_id_no || currentMarket.token_id || ''),
      side: selectedSide,
      action: 'buy',
      amount: amt,
      price: prc,
      question: currentMarket.question || '',
      source_dashboard: (window.__hbSwitcher && window.__hbSwitcher.current) || '',
    };

    if (activeTab === 'kalshi') {
      payload.slug = currentMarket.kalshi_ticker || currentMarket.slug || '';
    }

    fetch('/api/trading/place', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify(payload),
    })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
    .then(function (result) {
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.textContent = selectedSide === 'yes' ? 'Buy Yes' : 'Buy No';

      if (result.ok && result.data.ok) {
        var st = result.data.status || 'submitted';
        showStatus(st === 'error' ? 'error' : 'success',
          st === 'filled' ? 'Order filled! ' + (result.data.shares || 0).toFixed(1) + ' shares @ $' + (result.data.fill_price || 0).toFixed(2)
          : st === 'submitted' ? 'Order submitted! ID: ' + (result.data.order_id || '').substring(0, 12) + '...'
          : 'Order ' + st
        );
        loadOrderHistory();
      } else {
        showStatus('error', result.data.error || 'Order failed');
      }
    })
    .catch(function (e) {
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.textContent = selectedSide === 'yes' ? 'Buy Yes' : 'Buy No';
      showStatus('error', 'Network error: ' + e.message);
    });
  };

  function showStatus(type, msg) {
    statusEl.className = 'tp-status ' + type;
    statusEl.textContent = msg;
  }

  /* ── Order history ────────────────────────────────────────── */
  function loadOrderHistory() {
    fetch('/api/trading/orders', { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var orders = d.orders || [];
        if (orders.length === 0) {
          historyWrap.innerHTML = '<div class="tp-history-title">Recent Orders</div><div class="tp-no-orders">No orders yet</div>';
          return;
        }
        var html = '<div class="tp-history-title">Recent Orders</div>';
        orders.slice(0, 10).forEach(function (o) {
          var sideClass = o.side === 'yes' ? 'yes' : 'no';
          var statusClass = (o.status || 'pending').toLowerCase();
          var ts = o.created_at ? new Date(o.created_at * 1000).toLocaleString() : '';
          html += '<div class="tp-order">'
            + '<div class="tp-order-head">'
            + '<span class="tp-order-side ' + sideClass + '">' + escHtml((o.side || '').toUpperCase()) + ' on ' + escHtml(o.platform || '') + '</span>'
            + '<span class="tp-order-status ' + statusClass + '">' + escHtml(o.status || 'pending') + '</span>'
            + '</div>'
            + '<div class="tp-order-q">' + escHtml((o.market_question || o.market_slug || '').substring(0, 60)) + '</div>'
            + '<div class="tp-order-meta">$' + (o.amount || 0).toFixed(2) + ' @ $' + (o.price || 0).toFixed(2) + ' &middot; ' + escHtml(ts) + '</div>'
            + '</div>';
        });
        historyWrap.innerHTML = html;
      })
      .catch(function () {
        historyWrap.innerHTML = '<div class="tp-history-title">Recent Orders</div><div class="tp-no-orders">Could not load orders</div>';
      });
  }

  /* ── Public API ────────────────────────────────────────────── */
  /**
   * Open the trade panel.
   * @param {Object} opts
   * @param {string} opts.slug          - Market slug (Polymarket event slug)
   * @param {string} [opts.kalshi_ticker] - Kalshi event ticker
   * @param {string} [opts.token_id]    - Polymarket CLOB YES token ID
   * @param {string} [opts.token_id_no] - Polymarket CLOB NO token ID
   * @param {string} [opts.source]      - "polymarket" or "kalshi" (default tab)
   * @param {string} [opts.question]    - Market question text
   * @param {number} [opts.price]       - Current YES price (0-1)
   * @param {number} [opts.volume]      - Volume in USD
   */
  window.hbTrade = function (opts) {
    if (!opts || (!opts.slug && !opts.kalshi_ticker)) return;
    currentMarket = opts;

    // Reset form
    amountInput.value = '';
    priceInput.value = '';
    statusEl.className = 'tp-status';
    statusEl.textContent = '';
    submitting = false;
    submitBtn.disabled = false;

    // Title
    titleEl.textContent = opts.question || opts.slug;

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

    // Side buttons with prices
    btnYesSide.innerHTML = '<span class="tp-side-price">' + (yesP != null ? yesP + '&cent;' : '—') + '</span><span class="tp-side-label">Buy Yes</span>';
    btnNoSide.innerHTML = '<span class="tp-side-price">' + (noP != null ? noP + '&cent;' : '—') + '</span><span class="tp-side-label">Buy No</span>';

    // Default price to current market price
    if (opts.price != null) {
      priceInput.value = opts.price.toFixed(2);
    }

    // Select yes by default
    selectSide('yes');

    // Credential check + tabs
    checkCredentials();
    var src = (opts.source || 'polymarket').toLowerCase();
    tabs.style.display = (opts.slug && opts.kalshi_ticker) ? 'flex' : 'none';
    switchTab(src);

    // Load order history
    loadOrderHistory();

    // Update summary
    updateSummary();

    // Open
    overlay.className = 'tp-overlay open';
    setTimeout(function () { panel.className = 'tp-panel open'; }, 10);
  };

  /* ── Mount ─────────────────────────────────────────────────── */
  document.body.appendChild(host);
})();
