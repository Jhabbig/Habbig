/* user-features.js — global command palette (Cmd+K search) + save/follow button helpers.
 * Include on any page that has predictions / sources to turn buttons into live controls.
 *
 *   <script src="/_gateway_static/user-features.js" defer></script>
 *
 * DOM contracts:
 *   Save button:   <button data-action="save"   data-prediction-id="123">...</button>
 *   Follow button: <button data-action="follow" data-source-handle="alice">...</button>
 *
 * The script adds/removes an `is-active` class to reflect saved/following state.
 */

(function () {
  'use strict';

  function getCsrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function jsonFetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf()}, opts.headers || {});
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (data) { return {ok: r.ok, status: r.status, data: data}; });
    });
  }

  // ── Save / unsave predictions ─────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="save"]');
    if (!btn) return;
    e.preventDefault();
    var pid = btn.getAttribute('data-prediction-id');
    if (!pid) return;
    var isActive = btn.classList.contains('is-active');
    // Optimistic flip
    btn.classList.toggle('is-active');
    btn.disabled = true;
    var req = isActive
      ? jsonFetch('/api/saved/' + encodeURIComponent(pid), {method: 'DELETE'})
      : jsonFetch('/api/saved/' + encodeURIComponent(pid), {method: 'POST', body: {}});
    req.then(function (res) {
      btn.disabled = false;
      if (!res.ok) {
        // Revert on failure
        btn.classList.toggle('is-active');
      }
    }).catch(function () {
      btn.disabled = false;
      btn.classList.toggle('is-active');
    });
  });

  // ── Follow / unfollow sources ────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="follow"]');
    if (!btn) return;
    e.preventDefault();
    var handle = btn.getAttribute('data-source-handle');
    if (!handle) return;
    var isActive = btn.classList.contains('is-active');
    btn.classList.toggle('is-active');
    btn.disabled = true;
    var req = isActive
      ? jsonFetch('/api/sources/' + encodeURIComponent(handle) + '/follow', {method: 'DELETE'})
      : jsonFetch('/api/sources/' + encodeURIComponent(handle) + '/follow', {
          method: 'POST',
          body: {notify_on_prediction: false, notify_min_credibility: 0.5},
        });
    req.then(function (res) {
      btn.disabled = false;
      if (!res.ok) btn.classList.toggle('is-active');
    }).catch(function () {
      btn.disabled = false;
      btn.classList.toggle('is-active');
    });
  });

  // ── Command palette (Cmd+K / Ctrl+K) ─────────────────────────────────
  var HISTORY_KEY = 'narve:search:history';
  var MAX_HISTORY = 5;
  var SEARCH_DEBOUNCE_MS = 200;

  function loadHistory() {
    try {
      var raw = localStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }
  function pushHistory(q) {
    if (!q) return;
    var h = loadHistory().filter(function (x) { return x !== q; });
    h.unshift(q);
    h = h.slice(0, MAX_HISTORY);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); } catch (e) {}
  }
  function clearHistory() {
    try { localStorage.removeItem(HISTORY_KEY); } catch (e) {}
  }

  function buildOverlay() {
    if (document.getElementById('narve-cmdk')) return;
    var overlay = document.createElement('div');
    overlay.id = 'narve-cmdk';
    overlay.className = 'command-palette-overlay';
    overlay.innerHTML = [
      '<div class="command-palette" role="dialog" aria-label="Search">',
      '  <input type="text" class="command-palette-input" placeholder="Search predictions, sources, markets…" autocomplete="off" spellcheck="false">',
      '  <div class="command-palette-results" id="narve-cmdk-results"></div>',
      '</div>',
    ].join('');
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) close();
    });

    var input = overlay.querySelector('input');
    var results = overlay.querySelector('#narve-cmdk-results');
    var focused = -1;
    var items = [];
    var debounceT = null;

    function esc(s) {
      if (s === null || s === undefined) return '';
      return String(s).replace(/[&<>"']/g, function (m) {
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m];
      });
    }

    function render(payload, query) {
      items = [];
      var html = '';
      if (!query) {
        var history = loadHistory();
        if (history.length) {
          html += '<div class="command-palette-section">Recent searches</div>';
          history.forEach(function (h) {
            items.push({type: 'recent', query: h});
            html += '<div class="command-palette-item" data-idx="' + (items.length - 1) + '">'
                 + '<span>' + esc(h) + '</span>'
                 + '<span class="command-palette-shortcut">↵</span>'
                 + '</div>';
          });
          html += '<div class="command-palette-section"><a href="#" id="narve-cmdk-clear" style="color:var(--text-tertiary);font-size:0.65rem">Clear history</a></div>';
        } else {
          html += '<div class="command-palette-section">Start typing to search</div>';
        }
        results.innerHTML = html;
        var clr = document.getElementById('narve-cmdk-clear');
        if (clr) clr.addEventListener('click', function (e) { e.preventDefault(); clearHistory(); render(null, ''); });
        return;
      }
      var r = payload && payload.results ? payload.results : {predictions: [], sources: [], markets: []};
      if (r.predictions.length) {
        html += '<div class="command-palette-section">Predictions (' + r.predictions.length + ')</div>';
        r.predictions.forEach(function (p) {
          items.push({type: 'prediction', data: p});
          var hl = p.highlight || esc(p.content);
          html += '<div class="command-palette-item" data-idx="' + (items.length - 1) + '">'
               + '<span style="overflow:hidden;text-overflow:ellipsis">' + hl + '</span>'
               + '<span class="command-palette-shortcut">@' + esc(p.source_handle) + '</span>'
               + '</div>';
        });
      }
      if (r.sources.length) {
        html += '<div class="command-palette-section">Sources (' + r.sources.length + ')</div>';
        r.sources.forEach(function (s) {
          items.push({type: 'source', data: s});
          var cred = s.global_credibility !== null && s.global_credibility !== undefined ? s.global_credibility.toFixed(2) : '—';
          html += '<div class="command-palette-item" data-idx="' + (items.length - 1) + '">'
               + '<span>@' + esc(s.handle) + '</span>'
               + '<span class="command-palette-shortcut">' + cred + '</span>'
               + '</div>';
        });
      }
      if (r.markets.length) {
        html += '<div class="command-palette-section">Markets (' + r.markets.length + ')</div>';
        r.markets.forEach(function (m) {
          items.push({type: 'market', data: m});
          var yes = m.yes_price !== null && m.yes_price !== undefined ? Math.round(m.yes_price * 100) + '%' : '—';
          html += '<div class="command-palette-item" data-idx="' + (items.length - 1) + '">'
               + '<span>' + (m.highlight || esc(m.market_question || m.market_slug)) + '</span>'
               + '<span class="command-palette-shortcut">' + yes + '</span>'
               + '</div>';
        });
      }
      if (!items.length) {
        html = '<div class="empty-state" style="padding:48px 20px">'
             + '<div class="empty-state-title">No results</div>'
             + '<div class="empty-state-body">Try different keywords. Search covers predictions, sources, and markets.</div>'
             + '</div>';
      }
      results.innerHTML = html;
      focused = -1;
      results.querySelectorAll('.command-palette-item').forEach(function (el) {
        el.addEventListener('click', function () { activate(parseInt(el.getAttribute('data-idx'), 10)); });
      });
    }

    function activate(idx) {
      var it = items[idx];
      if (!it) return;
      if (it.type === 'recent') {
        input.value = it.query;
        input.dispatchEvent(new Event('input'));
        return;
      }
      pushHistory(input.value.trim());
      if (it.type === 'prediction') {
        window.location.href = '/saved#pred-' + it.data.id;
      } else if (it.type === 'source') {
        // Dashboard backends each have their own /sources/:handle view — we
        // route through the hub for now so the user lands on their dashboards.
        window.location.href = '/dashboards';
      } else if (it.type === 'market') {
        window.location.href = '/dashboards';
      }
      close();
    }

    function updateFocus() {
      var els = results.querySelectorAll('.command-palette-item');
      els.forEach(function (el, i) {
        if (i === focused) el.classList.add('focused'); else el.classList.remove('focused');
      });
      var el = els[focused];
      if (el && el.scrollIntoView) el.scrollIntoView({block: 'nearest'});
    }

    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (debounceT) clearTimeout(debounceT);
      if (!q) { render(null, ''); return; }
      debounceT = setTimeout(function () {
        fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=8')
          .then(function (r) { return r.json(); })
          .then(function (d) { render(d, q); })
          .catch(function () { render({results: {predictions: [], sources: [], markets: []}}, q); });
      }, SEARCH_DEBOUNCE_MS);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        focused = Math.min(items.length - 1, focused + 1);
        updateFocus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        focused = Math.max(0, focused - 1);
        updateFocus();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (focused >= 0) activate(focused);
        else if (items.length === 1) activate(0);
      } else if (e.key === 'Escape') {
        close();
      }
    });

    function open() {
      overlay.classList.add('open');
      setTimeout(function () { input.focus(); }, 10);
      render(null, '');
    }
    function close() {
      overlay.classList.remove('open');
      input.value = '';
    }

    window.narveCmdk = {open: open, close: close};
  }

  document.addEventListener('keydown', function (e) {
    // Cmd+K (Mac) or Ctrl+K (Win/Linux)
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      if (!document.getElementById('narve-cmdk')) buildOverlay();
      window.narveCmdk.open();
    }
  });

  // Pre-build the overlay at idle so the first Cmd+K is instant.
  if ('requestIdleCallback' in window) {
    requestIdleCallback(buildOverlay);
  } else {
    setTimeout(buildOverlay, 300);
  }
})();
