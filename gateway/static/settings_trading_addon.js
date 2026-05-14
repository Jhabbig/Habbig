/* settings_trading_addon.js — /settings/trading-addon
 *
 * Hydrates the four cards from /api/trading-addon/config and PATCHes
 * back on save. Mirrors settings_integrations.js for CSRF, toasts, and
 * modal wiring so changes here stay consistent with the rest of the
 * settings surface.
 *
 * Endpoints used:
 *   GET    /api/trading-addon/config   → current settings + active flag
 *   PATCH  /api/trading-addon/config   → upsert settings (validates bounds)
 *
 * Auto-execute deserves extra care: switching it ON requires confirming
 * a modal. Switching it OFF is one-click.
 */

(function () {
  'use strict';

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

  // Defaults — must match server-side _TRADING_ADDON_DEFAULTS.
  var DEFAULTS = {
    kelly_fraction: 0.5,
    max_cap_pct: 25,
    auto_execute: false,
    auto_execute_min_ev: 10,
    daily_cap: null,
    daily_cap_currency: 'USD',
    max_position_size: null,
    cooldown_minutes: null,
  };

  var state = Object.assign({}, DEFAULTS);
  var subscribed = false;

  function $(id) { return document.getElementById(id); }

  function renderSubscription(active) {
    var pill = $('ta-sub-pill');
    var subscribe = $('ta-subscribe-btn');
    var manage = $('ta-manage-btn');
    var empty = $('ta-empty-state');
    var configs = document.querySelectorAll('.ta-config');
    var saveBar = $('ta-save-bar');

    if (active) {
      pill.textContent = 'Active';
      pill.setAttribute('data-status', 'active');
      pill.classList.add('si-pill--connected');
      pill.classList.remove('si-pill--expired');
      subscribe.hidden = true;
      manage.hidden = false;
      empty.hidden = true;
      configs.forEach(function (c) { c.hidden = false; });
      saveBar.hidden = false;
    } else {
      pill.textContent = 'Not subscribed';
      pill.setAttribute('data-status', 'disconnected');
      pill.classList.remove('si-pill--connected', 'si-pill--expired');
      subscribe.hidden = false;
      manage.hidden = true;
      empty.hidden = false;
      configs.forEach(function (c) { c.hidden = true; });
      saveBar.hidden = true;
    }
  }

  function hydrate(cfg) {
    state = {
      kelly_fraction: cfg.kelly_fraction != null ? cfg.kelly_fraction : DEFAULTS.kelly_fraction,
      max_cap_pct: cfg.max_cap_pct != null ? cfg.max_cap_pct : DEFAULTS.max_cap_pct,
      auto_execute: !!cfg.auto_execute,
      auto_execute_min_ev: cfg.auto_execute_min_ev != null ? cfg.auto_execute_min_ev : DEFAULTS.auto_execute_min_ev,
      daily_cap: cfg.daily_cap != null ? cfg.daily_cap : null,
      daily_cap_currency: cfg.daily_cap_currency || 'USD',
      max_position_size: cfg.max_position_size != null ? cfg.max_position_size : null,
      cooldown_minutes: cfg.cooldown_minutes != null ? cfg.cooldown_minutes : null,
    };
    renderFromState();
  }

  function renderFromState() {
    var kellyBtns = document.querySelectorAll('#ta-kelly-fraction button');
    kellyBtns.forEach(function (b) {
      var v = parseFloat(b.getAttribute('data-value'));
      b.setAttribute('aria-pressed', String(Math.abs(v - state.kelly_fraction) < 1e-6));
    });

    var slider = $('ta-max-cap');
    var sliderValue = $('ta-max-cap-value');
    slider.value = String(state.max_cap_pct);
    sliderValue.textContent = state.max_cap_pct + '%';

    var autoBox = $('ta-auto-execute');
    var autoEv = $('ta-auto-execute-min-ev');
    autoBox.checked = state.auto_execute;
    autoEv.value = state.auto_execute_min_ev == null ? '' : String(state.auto_execute_min_ev);
    autoEv.disabled = !state.auto_execute;

    setCurrency(state.daily_cap_currency, /* renderOnly */ true);

    $('ta-daily-cap').value = state.daily_cap == null ? '' : String(state.daily_cap);
    $('ta-max-position').value = state.max_position_size == null ? '' : String(state.max_position_size);
    $('ta-cooldown').value = state.cooldown_minutes == null ? '' : String(state.cooldown_minutes);

    updateSaveStatus('');
  }

  function setCurrency(cur, renderOnly) {
    cur = cur === 'GBP' ? 'GBP' : 'USD';
    state.daily_cap_currency = cur;
    var btns = document.querySelectorAll('#ta-currency-toggle button');
    btns.forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-currency') === cur));
    });
    if (!renderOnly) updateSaveStatus('');
  }

  function updateSaveStatus(msg) {
    var statusEl = $('ta-save-status');
    statusEl.textContent = msg || '';
  }

  function collectFormValues() {
    var maxCap = parseInt($('ta-max-cap').value, 10);
    if (!(maxCap >= 1 && maxCap <= 25)) {
      updateSaveStatus('Max cap must be between 1 and 25.');
      return null;
    }

    var autoExecute = !!$('ta-auto-execute').checked;
    var minEvRaw = $('ta-auto-execute-min-ev').value.trim();
    var minEv = minEvRaw === '' ? null : parseFloat(minEvRaw);
    if (autoExecute) {
      if (minEv == null || !(minEv >= 1 && minEv <= 50)) {
        updateSaveStatus('Auto-execute EV threshold must be 1-50%.');
        return null;
      }
    }

    var dailyRaw = $('ta-daily-cap').value.trim();
    var daily = dailyRaw === '' ? null : parseFloat(dailyRaw);
    if (daily != null && !(daily >= 0)) {
      updateSaveStatus('Daily cap must be zero or positive.');
      return null;
    }

    var posRaw = $('ta-max-position').value.trim();
    var pos = posRaw === '' ? null : parseFloat(posRaw);
    if (pos != null && !(pos >= 0)) {
      updateSaveStatus('Max position size must be zero or positive.');
      return null;
    }

    var cdRaw = $('ta-cooldown').value.trim();
    var cooldown = cdRaw === '' ? null : parseInt(cdRaw, 10);
    if (cooldown != null && !(cooldown >= 0 && cooldown <= 1440)) {
      updateSaveStatus('Cooldown must be 0-1440 minutes.');
      return null;
    }

    return {
      kelly_fraction: state.kelly_fraction,
      max_cap_pct: maxCap,
      auto_execute: autoExecute,
      auto_execute_min_ev: minEv,
      daily_cap: daily,
      daily_cap_currency: state.daily_cap_currency,
      max_position_size: pos,
      cooldown_minutes: cooldown,
    };
  }

  async function save() {
    var payload = collectFormValues();
    if (!payload) return;
    var saveBtn = $('ta-save-btn');
    var original = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    updateSaveStatus('');
    try {
      var data = await api('/api/trading-addon/config', { method: 'PATCH', body: payload });
      hydrate(data);
      toastOk('Trading add-on settings saved.');
    } catch (e) {
      updateSaveStatus(e.message || 'Could not save.');
      toastErr(e.message || 'Could not save settings.');
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = original;
    }
  }

  function openAutoModal() {
    var modal = $('ta-auto-modal');
    modal.hidden = false;
    modal.setAttribute('aria-hidden', 'false');
    setTimeout(function () {
      var btn = $('ta-auto-modal-confirm');
      if (btn) btn.focus();
    }, 50);
  }

  function closeAutoModal() {
    var modal = $('ta-auto-modal');
    modal.hidden = true;
    modal.setAttribute('aria-hidden', 'true');
  }

  function cancelAutoModal() {
    var autoBox = $('ta-auto-execute');
    autoBox.checked = state.auto_execute;
    $('ta-auto-execute-min-ev').disabled = !state.auto_execute;
    closeAutoModal();
  }

  function confirmAutoModal() {
    state.auto_execute = true;
    $('ta-auto-execute').checked = true;
    $('ta-auto-execute-min-ev').disabled = false;
    closeAutoModal();
    updateSaveStatus('Auto-execute will be enabled on save.');
  }

  function wire() {
    var kellyGroup = $('ta-kelly-fraction');
    kellyGroup.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-value]');
      if (!b) return;
      var v = parseFloat(b.getAttribute('data-value'));
      state.kelly_fraction = v;
      var btns = kellyGroup.querySelectorAll('button');
      btns.forEach(function (x) {
        x.setAttribute('aria-pressed', String(x === b));
      });
      updateSaveStatus('');
    });

    var slider = $('ta-max-cap');
    var sliderValue = $('ta-max-cap-value');
    slider.addEventListener('input', function () {
      sliderValue.textContent = slider.value + '%';
      state.max_cap_pct = parseInt(slider.value, 10);
      updateSaveStatus('');
    });

    var autoBox = $('ta-auto-execute');
    var autoEv = $('ta-auto-execute-min-ev');
    autoBox.addEventListener('change', function () {
      if (autoBox.checked && !state.auto_execute) {
        autoEv.disabled = true;
        openAutoModal();
      } else if (!autoBox.checked) {
        state.auto_execute = false;
        autoEv.disabled = true;
        updateSaveStatus('');
      }
    });

    var modal = $('ta-auto-modal');
    modal.addEventListener('click', function (e) {
      if (e.target.matches('[data-close]')) cancelAutoModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.hidden) cancelAutoModal();
    });
    $('ta-auto-modal-confirm').addEventListener('click', confirmAutoModal);

    var currencyGroup = $('ta-currency-toggle');
    currencyGroup.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-currency]');
      if (!b) return;
      setCurrency(b.getAttribute('data-currency'));
    });

    ['ta-daily-cap', 'ta-max-position', 'ta-cooldown', 'ta-auto-execute-min-ev'].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener('input', function () { updateSaveStatus(''); });
    });

    $('ta-save-btn').addEventListener('click', save);
    $('ta-reset-btn').addEventListener('click', function () {
      hydrate(DEFAULTS);
      updateSaveStatus('Reset to defaults — save to apply.');
    });
  }

  async function load() {
    try {
      var data = await api('/api/trading-addon/config');
      subscribed = !!data.active;
      renderSubscription(subscribed);
      if (subscribed) {
        hydrate(data.config || {});
      }
    } catch (e) {
      if (e.status !== 401) {
        toastErr(e.message || 'Could not load trading add-on settings.');
      }
      renderSubscription(false);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { wire(); load(); });
  } else {
    wire();
    load();
  }
})();
