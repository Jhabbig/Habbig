/**
 * ⌘K command palette — unified search across markets/sources/predictions/users.
 *
 * Hooks:
 *   - ⌘K / Ctrl+K anywhere opens the modal
 *   - / as the first char switches to "command mode" (/settings, /admin, etc.)
 *   - ↑ / ↓ move selection, Enter navigates, Escape closes
 *
 * Auto-mounts: any page that ships this script gets the palette for free.
 * Script is idempotent — loading it twice is a no-op.
 */
(() => {
  if (window.__narveCmdPaletteInit) return;
  window.__narveCmdPaletteInit = true;

  const DEBOUNCE_MS = 150;
  const MIN_QUERY_LEN = 2;
  const RECENTS_KEY = 'narve:cmdp:recents';
  const MAX_RECENTS = 6;

  // "/" commands — keep tiny. Add more as nav solidifies.
  const COMMANDS = [
    { label: '/dashboards', subtitle: 'Your dashboards', url: '/dashboards' },
    { label: '/saved',      subtitle: 'Saved predictions', url: '/saved' },
    { label: '/feed',       subtitle: 'Live signal feed', url: '/dashboards' },
    { label: '/settings',   subtitle: 'Account settings', url: '/settings' },
    { label: '/billing',    subtitle: 'Subscription + invoices', url: '/settings/billing' },
    { label: '/notifications', subtitle: 'In-app notifications', url: '/notifications' },
    { label: '/admin',      subtitle: 'Admin home (admins only)', url: '/admin' },
  ];

  function debounce(fn, ms) {
    let t;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function readRecents() {
    try {
      const raw = localStorage.getItem(RECENTS_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch { return []; }
  }

  function saveRecent(query) {
    if (!query || query.length < MIN_QUERY_LEN) return;
    try {
      const list = readRecents().filter(q => q !== query);
      list.unshift(query);
      localStorage.setItem(RECENTS_KEY, JSON.stringify(list.slice(0, MAX_RECENTS)));
    } catch { /* quota / disabled */ }
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Simple type → monochrome mark. Keeping as ASCII-ish so no font loading.
  const TYPE_GLYPH = {
    market: '◆',
    source: '@',
    prediction: '›',
    user: '●',
    command: '/',
    recent: '↻',
  };
  const TYPE_LABEL = {
    market: 'Markets',
    source: 'Sources',
    prediction: 'Predictions',
    user: 'Users',
    command: 'Commands',
    recent: 'Recent',
  };

  // FTS snippet delimiters we tolerate in server responses. Anything else
  // is escaped for safety. Server writes exactly <mark>…</mark>; narrowing
  // the allowlist means an injection in the underlying text still can't
  // produce arbitrary HTML.
  const MARK_OPEN = '<mark>';
  const MARK_CLOSE = '</mark>';

  /** Render a server-provided highlight string. Escapes everything except
   * the two <mark> tags we expect — keeps XSS closed even if a source's
   * summary contains angle brackets.
   */
  function renderHighlight(raw) {
    if (raw == null) return '';
    // Escape then re-introduce the mark tags — cheaper than a proper
    // whitelisting HTML parser and sufficient given the allowlist is
    // exactly two static strings.
    let out = esc(String(raw));
    out = out.split(esc(MARK_OPEN)).join(MARK_OPEN);
    out = out.split(esc(MARK_CLOSE)).join(MARK_CLOSE);
    return out;
  }

  class Palette {
    constructor() {
      this.root = null;
      this.input = null;
      this.list = null;
      this.footer = null;
      this.items = [];       // flat list of navigable result objects
      this.sel = 0;
      this.queryId = null;   // last /api/search response's query_id
      this.lastQ = '';
      // AbortController for the in-flight search. A fresh keystroke fires
      // the new request AND cancels the previous one — cuts network load
      // on slow connections and prevents late stale responses from ever
      // hitting the reconciliation path.
      this.ac = null;
      this.onSearchBound = debounce(this.doSearch.bind(this), DEBOUNCE_MS);
    }

    mount() {
      if (this.root) return;
      const backdrop = document.createElement('div');
      backdrop.className = 'narve-cmdp-backdrop';
      // ARIA combobox pattern — input owns the results listbox via
      // aria-controls, and aria-activedescendant points to whichever
      // row is currently selected. This lets screen readers announce
      // the highlighted result without moving DOM focus off the input.
      backdrop.innerHTML = `
        <div class="narve-cmdp" role="dialog" aria-modal="true" aria-label="Search">
          <input class="narve-cmdp-input" type="text"
                 placeholder="Search markets, sources, predictions… (/ for commands)"
                 autocomplete="off" autocorrect="off" spellcheck="false"
                 role="combobox" aria-expanded="true" aria-autocomplete="list"
                 aria-controls="narve-cmdp-listbox" aria-label="Search">
          <div class="narve-cmdp-results" id="narve-cmdp-listbox" role="listbox"
               aria-label="Search results"></div>
          <div class="narve-cmdp-footer" aria-hidden="true">
            <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
            <span><kbd>↵</kbd> open</span>
            <span><kbd>esc</kbd> close</span>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      this.root = backdrop;
      this.input = backdrop.querySelector('.narve-cmdp-input');
      this.list = backdrop.querySelector('.narve-cmdp-results');
      this.footer = backdrop.querySelector('.narve-cmdp-footer');
      this.input.addEventListener('input', () => this.onSearchBound());
      this.input.addEventListener('keydown', this.onKey.bind(this));
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) this.close();
      });
    }

    open() {
      this.mount();
      this.root.classList.add('open');
      document.documentElement.classList.add('narve-cmdp-lock');
      this.input.value = '';
      this.lastQ = '';
      this.renderEmpty();
      // Focus after the class flip so Safari doesn't scroll the page.
      requestAnimationFrame(() => this.input.focus());
    }

    close() {
      if (!this.root) return;
      this.root.classList.remove('open');
      document.documentElement.classList.remove('narve-cmdp-lock');
    }

    isOpen() {
      return this.root && this.root.classList.contains('open');
    }

    renderEmpty() {
      const recents = readRecents();
      if (recents.length === 0) {
        this.renderGroups([{
          type: 'command',
          items: COMMANDS.map(c => ({
            type: 'command',
            title: c.label,
            subtitle: c.subtitle,
            url: c.url,
          })),
        }]);
        return;
      }
      this.renderGroups([
        {
          type: 'recent',
          items: recents.map(q => ({
            type: 'recent',
            title: q,
            subtitle: 'Recent search',
            query: q,
          })),
        },
        {
          type: 'command',
          items: COMMANDS.map(c => ({
            type: 'command', title: c.label, subtitle: c.subtitle, url: c.url,
          })),
        },
      ]);
    }

    async doSearch() {
      const q = this.input.value.trim();
      this.lastQ = q;

      if (q.length === 0) { this.renderEmpty(); return; }

      // Command mode — filter COMMANDS list client-side; no API hit.
      if (q.startsWith('/')) {
        const needle = q.slice(1).toLowerCase();
        const matches = COMMANDS.filter(c => c.label.toLowerCase().includes(needle));
        this.renderGroups([{
          type: 'command',
          items: matches.map(c => ({
            type: 'command', title: c.label, subtitle: c.subtitle, url: c.url,
          })),
        }]);
        return;
      }

      if (q.length < MIN_QUERY_LEN) {
        this.list.innerHTML = `<div class="narve-cmdp-hint">Keep typing…</div>`;
        this.items = [];
        this.sel = 0;
        return;
      }

      // Cancel any previous in-flight search. AbortError is swallowed in
      // the catch below so it never renders as a visible error.
      if (this.ac) {
        try { this.ac.abort(); } catch { /* ignore */ }
      }
      this.ac = new AbortController();
      const signal = this.ac.signal;

      try {
        const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`, {
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' },
          signal,
        });
        if (!r.ok) {
          // 429 rate-limit → silent throttle message, don't kill palette
          if (r.status === 429) {
            this.list.innerHTML =
              `<div class="narve-cmdp-hint">Typing too fast — slow down.</div>`;
            return;
          }
          throw new Error('search request failed: ' + r.status);
        }
        const data = await r.json();
        this.queryId = data.query_id || null;
        if (q !== this.lastQ) return;  // stale response

        // Group results by type in the spec order.
        const groups = {};
        for (const res of (data.results || [])) {
          (groups[res.type] = groups[res.type] || []).push(res);
        }
        const order = ['market', 'source', 'prediction', 'user'];
        const grouped = order
          .filter(t => groups[t] && groups[t].length)
          .map(t => ({ type: t, items: groups[t] }));
        if (grouped.length === 0) {
          this.list.innerHTML = `<div class="narve-cmdp-hint">No matches. Try a different term.</div>`;
          this.items = [];
          this.sel = 0;
          return;
        }
        this.renderGroups(grouped);
      } catch (err) {
        // AbortError is expected — a newer keystroke cancelled us.
        if (err && err.name === 'AbortError') return;
        this.list.innerHTML =
          `<div class="narve-cmdp-hint narve-cmdp-error">Search failed. Try again.</div>`;
        console.warn('[cmdp] search error', err);
      }
    }

    renderGroups(groups) {
      const flat = [];
      // Each group gets an accessible group header with an id we point
      // the listbox items at via aria-describedby (so screen readers
      // announce "Markets, <title>" rather than just "<title>").
      const html = groups.map((g, gi) => {
        const groupId = `narve-cmdp-grp-${gi}`;
        const rows = g.items.map(it => {
          const idx = flat.length;
          flat.push(it);
          const glyph = TYPE_GLYPH[it.type] || '·';
          const optId = `narve-cmdp-opt-${idx}`;
          // role=option + aria-selected tracks the currently-highlighted
          // result for assistive tech. aria-describedby points at the
          // group label so type context comes through.
          return `<div class="narve-cmdp-row" data-i="${idx}"
                       id="${optId}" role="option" aria-selected="false"
                       aria-describedby="${groupId}">
            <span class="narve-cmdp-glyph" aria-hidden="true">${esc(glyph)}</span>
            <span class="narve-cmdp-title">${esc(it.title)}</span>
            <span class="narve-cmdp-sub">${esc(it.subtitle)}</span>
          </div>`;
        }).join('');
        const label = TYPE_LABEL[g.type] || g.type;
        return `<div class="narve-cmdp-group" role="group" aria-labelledby="${groupId}">
          <div class="narve-cmdp-group-label" id="${groupId}">${label}</div>
          ${rows}
        </div>`;
      }).join('');
      this.list.innerHTML = html;
      this.items = flat;
      this.sel = 0;
      this.highlight();
      this.list.querySelectorAll('.narve-cmdp-row').forEach(el => {
        el.addEventListener('mouseenter', () => {
          this.sel = Number(el.dataset.i);
          this.highlight();
        });
        el.addEventListener('click', () => this.navigate(this.items[Number(el.dataset.i)]));
      });
    }

    highlight() {
      if (!this.list) return;
      let activeId = '';
      this.list.querySelectorAll('.narve-cmdp-row').forEach((el, i) => {
        const selected = i === this.sel;
        el.classList.toggle('selected', selected);
        el.setAttribute('aria-selected', selected ? 'true' : 'false');
        if (selected) {
          activeId = el.id || '';
          el.scrollIntoView({ block: 'nearest' });
        }
      });
      // aria-activedescendant on the input stays in the DOM focus while
      // the highlighted option changes — the screen-reader-accessible
      // way to render a listbox that doesn't move focus off the search
      // field.
      if (this.input) {
        if (activeId) {
          this.input.setAttribute('aria-activedescendant', activeId);
        } else {
          this.input.removeAttribute('aria-activedescendant');
        }
      }
    }

    moveSel(delta) {
      if (!this.items.length) return;
      this.sel = (this.sel + delta + this.items.length) % this.items.length;
      this.highlight();
    }

    onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); this.close(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); this.moveSel(1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); this.moveSel(-1); return; }
      if (e.key === 'Enter') {
        e.preventDefault();
        const item = this.items[this.sel];
        if (item) this.navigate(item);
      }
    }

    async navigate(item) {
      if (!item) return;
      // "Recent" entries are queries, not targets — requeue the search.
      if (item.type === 'recent') {
        this.input.value = item.query;
        this.doSearch();
        return;
      }
      // Log the click BEFORE leaving the page so analytics gets the hit
      // even if navigation aborts (blocked popup, offline, etc.).
      if (this.queryId && item.type !== 'command' && item.type !== 'recent') {
        try {
          await fetch('/api/search/click', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              // Global CSRF middleware accepts token from the CSRF cookie;
              // credentials: same-origin sends it.
            },
            credentials: 'same-origin',
            body: JSON.stringify({
              query_id: this.queryId,
              result_type: item.type,
              result_id: String(item.id || ''),
            }),
            keepalive: true,
          });
        } catch { /* best-effort */ }
      }
      saveRecent(this.lastQ);
      if (item.url) {
        this.close();
        window.location.href = item.url;
      }
    }
  }

  const palette = new Palette();

  // Global hotkey: ⌘K on mac, Ctrl+K elsewhere.
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (palette.isOpen()) palette.close(); else palette.open();
    }
  });

  // Click target for nav search icon (pages that already inject one), plus
  // our own self-mounted floating pill below for pages that don't.
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-narve-search], .narve-search-trigger');
    if (trigger) {
      e.preventDefault();
      palette.open();
    }
  });

  // Self-mounting floating trigger pill. Matches the notification-bell
  // pattern — pages don't need to embed a specific <button>, we just
  // draw one. Skipped on:
  //   * the palette backdrop itself (would render inside)
  //   * prerelease/gate pages (no session → search is useless)
  //   * any page that already renders its own .narve-search-trigger
  //     (caller opted for custom placement and we don't want two)
  function mountPill() {
    if (document.querySelector('.narve-search-trigger')) return;
    const PUBLIC_PATHS = new Set([
      '/', '/gate', '/login', '/register', '/token', '/signup',
      '/forgot-password', '/reset-password',
      '/terms', '/privacy', '/dpa', '/unsubscribe',
      '/about', '/how-it-works', '/methodology', '/faq',
      '/team', '/press', '/changelog', '/narve',
    ]);
    if (PUBLIC_PATHS.has(location.pathname)) return;
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'narve-search-trigger narve-cmdp-pill';
    // Detect platform to show the right modifier glyph. navigator.platform
    // is deprecated but the replacement (userAgentData.platform) isn't
    // widely available yet, so fall through to legacy.
    const isMac = /Mac|iPhone|iPad/i.test(
      (navigator.userAgentData && navigator.userAgentData.platform) ||
      navigator.platform || ''
    );
    const mod = isMac ? '⌘' : 'Ctrl';
    // aria-label uses the verbose "Command K" / "Control K" phrasing so
    // screen readers announce the shortcut rather than a cryptic glyph.
    const modWord = isMac ? 'Command' : 'Control';
    pill.setAttribute('aria-label',
      `Open search — keyboard shortcut ${modWord}+K`);
    pill.setAttribute('aria-keyshortcuts', isMac ? 'Meta+K' : 'Control+K');
    pill.innerHTML = `
      <span class="narve-cmdp-pill-icon" aria-hidden="true">⌕</span>
      <span class="narve-cmdp-pill-label">Search</span>
      <span class="narve-cmdp-pill-kbd" aria-hidden="true">${mod}K</span>`;
    document.body.appendChild(pill);
  }
  // DOMContentLoaded already fires before this IIFE in the defer path,
  // but guard both sides so repeated boot (pjax, test fixtures) is safe.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountPill, { once: true });
  } else {
    mountPill();
  }

  // Expose a tiny API for debugging + programmatic open.
  window.narveCmdPalette = {
    open: () => palette.open(),
    close: () => palette.close(),
  };
})();
