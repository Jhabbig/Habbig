/* settings_integrations.js — /settings/integrations
 *
 * Wires the three cards (Polymarket, Kalshi, Bankroll) to the existing
 * market_routes endpoints. No new auth flow: Polymarket reuses the
 * MetaMask eth_requestAccounts pattern from trade.js, and Kalshi posts
 * email/password to /api/markets/connect/kalshi which exchanges them
 * for an encrypted session token server-side.
 *
 * Endpoints used:
 *   GET    /api/markets/connections           → current state
 *   POST   /api/markets/connect/polymarket    → save wallet address
 *   POST   /api/markets/connect/kalshi        → email+password → token
 *   DELETE /api/markets/connect/{source}      → disconnect
 *   GET    /api/user/bankroll                 → current bankroll
 *   PATCH  /api/user/bankroll                 → save bankroll
 *
 * All mutating requests carry the _csrf cookie + matching X-CSRF-Token
 * header — same pattern as feedback.js / collections_widget.js.
 */

(function () {
  'use strict';

  // ── CSRF helper ───────────────────────────────────────────────────────
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function toastOk(msg) {
    if (typeof window.narveToast === 'function') {
      window.narveToast(msg, { type: 'success' });
    }
  }

  function toastErr(msg) {
    if (typeof window.narveToastError === 'function') {
      window.narveToastError(msg);
    } else if (typeof window.narveToast === 'function') {
      window.narveToast(msg, { type: 'error' });
    }
  }

  async function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      headers['X-CSRF-Token'] = csrfToken();
    }
    if (opts.body !== undefined && typeof opts.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var res = await fetch(path, {
      method: method,
      credentials: 'same-origin',
      headers: headers,
      body: opts.body,
    });
    var data = null;
    try { data = await res.json(); } catch (_) { /* tolerate empty bodies */ }
    if (!res.ok) {
      var msg = (data && (data.error || data.detail || data.message)) || ('HTTP ' + res.status);
      var err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data || {};
  }

  // ── Connection card rendering ─────────────────────────────────────────
  function truncateAddress(addr) {
    if (!addr) return '';
    if (addr.length <= 12) return addr;
    return addr.slice(0, 6) + '…' + addr.slice(-4);
  }

  function setPill(el, label, status) {
    el.textContent = label;
    el.setAttribute('data-status', status);
    el.classList.remove('si-pill--connected', 'si-pill--expired');
    if (status === 'active') el.classList.add('si-pill--connected');
    else if (status === 'expired') el.classList.add('si-pill--expired');
  }

  function renderPolymarket(state) {
    var pill = document.getElementById('si-poly-pill');
    var ident = document.getElementById('si-poly-identifier');
    var connectBtn = document.getElementById('si-poly-connect');
    var disconnectBtn = document.getElementById('si-poly-disconnect');
    var status = state.status || 'disconnected';
    var addr = state.address || '';

    if (status === 'active' && addr) {
      setPill(pill, 'Connected', 'active');
      ident.hidden = false;
      ident.className = 'si-identifier';
      ident.textContent = truncateAddress(addr);
      ident.title = addr;
      connectBtn.textContent = 'Reconnect wallet';
      disconnectBtn.hidden = false;
    } else if (status === 'expired' && addr) {
      setPill(pill, 'Session expired', 'expired');
      ident.hidden = false;
      ident.className = 'si-identifier';
      ident.textContent = truncateAddress(addr) + ' (disconnected)';
      ident.title = addr;
      connectBtn.textContent = 'Reconnect wallet';
      disconnectBtn.hidden = false;
    } else {
      setPill(pill, 'Not connected', 'disconnected');
      ident.hidden = false;
      ident.className = 'si-identifier-empty';
      ident.textContent = 'No wallet linked yet.';
      connectBtn.textContent = 'Connect Wallet';
      disconnectBtn.hidden = true;
    }
  }

  function renderKalshi(state) {
    var pill = document.getElementById('si-kalshi-pill');
    var ident = document.getElementById('si-kalshi-identifier');
    var connectBtn = document.getElementById('si-kalshi-connect');
    var reconnectBtn = document.getElementById('si-kalshi-reconnect');
    var disconnectBtn = document.getElementById('si-kalshi-disconnect');
    var status = state.status || 'disconnected';
    var member = state.member_id || '';

    if (status === 'active') {
      setPill(pill, 'Connected', 'active');
      ident.hidden = false;
      ident.className = 'si-identifier';
      ident.textContent = member ? ('Member: ' + member) : 'Connected';
      connectBtn.hidden = true;
      reconnectBtn.hidden = false;
      disconnectBtn.hidden = false;
    } else if (status === 'expired') {
      setPill(pill, 'Session expired', 'expired');
      ident.hidden = false;
      ident.className = 'si-identifier';
      ident.textContent = member
        ? ('Member: ' + member + ' — reconnect to resume sync')
        : 'Session expired — reconnect to resume sync';
      connectBtn.hidden = true;
      reconnectBtn.hidden = false;
      disconnectBtn.hidden = false;
    } else {
      setPill(pill, 'Not connected', 'disconnected');
      ident.hidden = false;
      ident.className = 'si-identifier-empty';
      ident.textContent = 'No Kalshi account linked yet.';
      connectBtn.hidden = false;
      reconnectBtn.hidden = true;
      disconnectBtn.hidden = true;
    }
  }

  async function refreshConnections() {
    try {
      var data = await api('/api/markets/connections');
      renderPolymarket(data.polymarket || {});
      renderKalshi(data.kalshi || {});
    } catch (e) {
      // Trading add-on not active — render as disconnected with a hint.
      renderPolymarket({});
      renderKalshi({});
      if (e.status === 403) {
        toastErr('Trading add-on required to manage market integrations.');
      } else if (e.status !== 401) {
        toastErr('Could not load connections: ' + e.message);
      }
    }
  }

  // ── Polymarket connect (MetaMask) ─────────────────────────────────────
  async function connectPolymarket() {
    if (typeof window.ethereum === 'undefined') {
      toastErr('MetaMask or a compatible wallet is required.');
      return;
    }
    var btn = document.getElementById('si-poly-connect');
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Requesting wallet…';
    try {
      var accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
      var address = accounts && accounts[0];
      if (!address) throw new Error('No account selected');
      await api('/api/markets/connect/polymarket', {
        method: 'POST',
        body: { wallet_address: address },
      });
      toastOk('Polymarket wallet connected.');
      await refreshConnections();
    } catch (e) {
      if (e.code === 4001) {
        // User rejected — silent.
      } else {
        toastErr(e.message || 'Could not connect wallet.');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function disconnectPolymarket() {
    if (!window.confirm('Disconnect your Polymarket wallet?')) return;
    try {
      await api('/api/markets/connect/polymarket', { method: 'DELETE' });
      toastOk('Polymarket wallet disconnected.');
      await refreshConnections();
    } catch (e) {
      toastErr(e.message || 'Could not disconnect.');
    }
  }

  // ── Kalshi connect (modal) ────────────────────────────────────────────
  function openKalshiModal() {
    var modal = document.getElementById('si-kalshi-modal');
    modal.hidden = false;
    modal.setAttribute('aria-hidden', 'false');
    document.getElementById('si-kalshi-modal-status').textContent = '';
    var emailEl = document.getElementById('si-kalshi-email');
    emailEl.value = '';
    document.getElementById('si-kalshi-pass').value = '';
    setTimeout(function () { emailEl.focus(); }, 50);
  }

  function closeKalshiModal() {
    var modal = document.getElementById('si-kalshi-modal');
    modal.hidden = true;
    modal.setAttribute('aria-hidden', 'true');
  }

  async function submitKalshiModal() {
    var emailEl = document.getElementById('si-kalshi-email');
    var passEl = document.getElementById('si-kalshi-pass');
    var statusEl = document.getElementById('si-kalshi-modal-status');
    var submitBtn = document.getElementById('si-kalshi-modal-submit');
    var email = emailEl.value.trim();
    var password = passEl.value;
    if (!email || !password) {
      statusEl.textContent = 'Email and password are required.';
      return;
    }
    submitBtn.disabled = true;
    var original = submitBtn.textContent;
    submitBtn.textContent = 'Connecting…';
    statusEl.textContent = '';
    try {
      var data = await api('/api/markets/connect/kalshi', {
        method: 'POST',
        body: { email: email, password: password },
      });
      toastOk('Kalshi connected' + (data.member_id ? ' (' + data.member_id + ').' : '.'));
      passEl.value = '';
      closeKalshiModal();
      await refreshConnections();
    } catch (e) {
      statusEl.textContent = e.message || 'Could not connect.';
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = original;
    }
  }

  async function disconnectKalshi() {
    if (!window.confirm('Disconnect your Kalshi account? You will need to sign in again to resume sync.')) return;
    try {
      await api('/api/markets/connect/kalshi', { method: 'DELETE' });
      toastOk('Kalshi disconnected.');
      await refreshConnections();
    } catch (e) {
      toastErr(e.message || 'Could not disconnect.');
    }
  }

  // ── Bankroll ──────────────────────────────────────────────────────────
  var bankrollState = { currency: 'USD' };

  function formatCurrent(amount, currency) {
    if (amount === null || amount === undefined) return '—';
    var symbol = currency === 'GBP' ? '£' : '$';
    return symbol + Number(amount).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  async function loadBankroll() {
    try {
      var data = await api('/api/user/bankroll');
      var input = document.getElementById('si-bankroll-input');
      var current = document.getElementById('si-bankroll-current');
      if (data.bankroll !== null && data.bankroll !== undefined) {
        input.value = String(data.bankroll);
      }
      // Currency is informational on the client — backend stores USD only.
      var amount = (data.bankroll === null || data.bankroll === undefined) ? null : Number(data.bankroll);
      current.innerHTML = 'Current: <strong>' + (amount === null ? '—' : formatCurrent(amount, bankrollState.currency)) + '</strong>';
    } catch (e) {
      if (e.status !== 401 && e.status !== 403) {
        toastErr(e.message || 'Could not load bankroll.');
      }
    }
  }

  function setCurrency(cur) {
    bankrollState.currency = cur;
    var btns = document.querySelectorAll('.si-currency-toggle button');
    btns.forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-currency') === cur));
    });
    // Re-render current with new symbol (rough display only — server stores USD).
    var input = document.getElementById('si-bankroll-input');
    var current = document.getElementById('si-bankroll-current');
    var v = input.value.trim();
    if (v) {
      current.innerHTML = 'Current: <strong>' + formatCurrent(parseFloat(v), cur) + '</strong>';
    }
  }

  async function saveBankroll() {
    var input = document.getElementById('si-bankroll-input');
    var statusEl = document.getElementById('si-bankroll-status');
    var btn = document.getElementById('si-bankroll-save');
    var raw = input.value.trim();
    if (raw === '') {
      statusEl.textContent = 'Enter an amount first.';
      return;
    }
    var amount = parseFloat(raw);
    if (!Number.isFinite(amount) || amount < 0) {
      statusEl.textContent = 'Bankroll must be a non-negative number.';
      return;
    }
    btn.disabled = true;
    statusEl.textContent = 'Saving…';
    try {
      var data = await api('/api/user/bankroll', {
        method: 'PATCH',
        body: { bankroll: amount, currency: bankrollState.currency },
      });
      var saved = (data.bankroll === null || data.bankroll === undefined) ? null : Number(data.bankroll);
      document.getElementById('si-bankroll-current').innerHTML =
        'Current: <strong>' + (saved === null ? '—' : formatCurrent(saved, bankrollState.currency)) + '</strong>';
      statusEl.textContent = 'Saved.';
      toastOk('Bankroll saved.');
      setTimeout(function () {
        if (statusEl.textContent === 'Saved.') statusEl.textContent = '';
      }, 2500);
    } catch (e) {
      statusEl.textContent = e.message || 'Could not save.';
    } finally {
      btn.disabled = false;
    }
  }

  // ── Wire-up ───────────────────────────────────────────────────────────
  function init() {
    var polyConnect = document.getElementById('si-poly-connect');
    var polyDisconnect = document.getElementById('si-poly-disconnect');
    var kConnect = document.getElementById('si-kalshi-connect');
    var kReconnect = document.getElementById('si-kalshi-reconnect');
    var kDisconnect = document.getElementById('si-kalshi-disconnect');
    var kModalSubmit = document.getElementById('si-kalshi-modal-submit');
    var brSave = document.getElementById('si-bankroll-save');
    if (!polyConnect) return; // page mismatch — bail silently

    polyConnect.addEventListener('click', connectPolymarket);
    polyDisconnect.addEventListener('click', disconnectPolymarket);
    kConnect.addEventListener('click', openKalshiModal);
    kReconnect.addEventListener('click', openKalshiModal);
    kDisconnect.addEventListener('click', disconnectKalshi);
    kModalSubmit.addEventListener('click', submitKalshiModal);
    brSave.addEventListener('click', saveBankroll);

    document.querySelectorAll('#si-kalshi-modal [data-close]').forEach(function (el) {
      el.addEventListener('click', closeKalshiModal);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var m = document.getElementById('si-kalshi-modal');
        if (m && !m.hidden) closeKalshiModal();
      }
    });

    document.querySelectorAll('.si-currency-toggle button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        setCurrency(btn.getAttribute('data-currency'));
      });
    });

    refreshConnections();
    loadBankroll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
