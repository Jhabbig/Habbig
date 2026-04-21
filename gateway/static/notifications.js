/* In-app notification bell — self-mounting widget.
 *
 * Include on any authenticated page with:
 *   <script src="/_gateway_static/notifications.js" defer></script>
 *
 * The script checks /api/me (via /api/notifications/unread_count) on load.
 * If the user is authenticated it injects a fixed-position bell in the
 * top-right corner with a badge count, a dropdown listing the last ~10
 * notifications, and an SSE connection for real-time updates.
 *
 * Everything is scoped by the single ID `narve-notif-root` so including the
 * script twice is an idempotent no-op (second init is skipped).
 */
(function () {
  'use strict';

  // Prevent double-init if two pages both include the script (should never
  // happen with defer, but cheap insurance).
  if (document.getElementById('narve-notif-root')) return;

  const API_BASE        = '/api/notifications';
  const UNREAD_POLL_MS  = 30_000;      // badge-only fallback poll
  const AUTOREAD_DELAY  = 1000;        // ms before dropdown open marks-all-seen
  const DROPDOWN_LIMIT  = 10;

  // ── Minimal CSRF helper ───────────────────────────────────────────────────
  // Gateway uses double-submit cookie. POST/PATCH/DELETE to our API must echo
  // the cookie value in the x-csrf-token header.
  function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  async function api(path, opts = {}) {
    const init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: { 'accept': 'application/json', ...(opts.headers || {}) },
    };
    if (opts.body) {
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
      init.headers['content-type'] = 'application/json';
    }
    if (init.method !== 'GET') {
      const t = csrfToken();
      if (t) init.headers['x-csrf-token'] = t;
    }
    const res = await fetch(path, init);
    if (res.status === 401) return null;       // not authed — swallow
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const ct = res.headers.get('content-type') || '';
    return ct.includes('json') ? res.json() : res.text();
  }

  // ── DOM scaffolding ───────────────────────────────────────────────────────
  const TYPE_ICON_SVG = {
    market:   '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13l3-5 3 3 4-7 2 4"/></svg>',
    source:   '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5" r="3"/><path d="M2 14c0-3 2.5-5 6-5s6 2 6 5"/></svg>',
    alert:    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L1 14h14L8 1.5z"/><line x1="8" y1="6" x2="8" y2="10"/><circle cx="8" cy="12" r=".5" fill="currentColor"/></svg>',
    insider:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L2.5 4v4c0 3.5 2.3 5.7 5.5 6.5 3.2-.8 5.5-3 5.5-6.5V4L8 1.5z"/></svg>',
    payment:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1.5" y="4" width="13" height="8" rx="1"/><line x1="1.5" y1="7" x2="14.5" y2="7"/></svg>',
    system:   '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><line x1="8" y1="5" x2="8" y2="9"/><circle cx="8" cy="11" r=".5" fill="currentColor"/></svg>',
  };
  const TYPE_TO_ICON = {
    market_resolved:   'market',
    market_mover:      'market',
    high_ev_alert:     'alert',
    source_prediction: 'source',
    insider_signal:    'insider',
    payment:           'payment',
    system:            'system',
  };

  function iconFor(n) {
    const key = n.icon || TYPE_TO_ICON[n.type] || 'system';
    return TYPE_ICON_SVG[key] || TYPE_ICON_SVG.system;
  }

  function relativeTime(epochSec) {
    if (!epochSec) return '';
    const now = Math.floor(Date.now() / 1000);
    const d = Math.max(0, now - Number(epochSec));
    if (d < 60)        return 'just now';
    if (d < 3600)      return `${Math.floor(d / 60)} min ago`;
    if (d < 86400)     return `${Math.floor(d / 3600)} hr ago`;
    if (d < 7 * 86400) return `${Math.floor(d / 86400)} d ago`;
    const dt = new Date(epochSec * 1000);
    return dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── State ─────────────────────────────────────────────────────────────────
  const state = {
    root:           null,
    bell:           null,
    badge:          null,
    dropdown:       null,
    list:           null,
    items:          [],        // {id, type, title, body, link_url, icon, read_at, created_at}
    unreadCount:    0,
    isOpen:         false,
    sse:            null,
    pollHandle:     null,
    autoreadHandle: null,
    initialised:    false,
  };

  // ── Rendering ─────────────────────────────────────────────────────────────
  function renderBadge() {
    const n = state.unreadCount;
    state.badge.textContent = n > 99 ? '99+' : String(n);
    state.badge.style.display = n > 0 ? 'inline-block' : 'none';
  }

  function renderList() {
    if (!state.items.length) {
      state.list.innerHTML =
        '<div class="narve-notif-empty">No notifications yet.</div>';
      return;
    }
    const rows = state.items.slice(0, DROPDOWN_LIMIT).map((n) => {
      const unread = !n.read_at;
      return (
        `<button type="button" class="narve-notif-item${unread ? ' is-unread' : ''}" data-id="${n.id}" data-link="${escapeHtml(n.link_url || '')}">` +
        `  <span class="narve-notif-dot" aria-hidden="true"></span>` +
        `  <span class="narve-notif-icon" aria-hidden="true">${iconFor(n)}</span>` +
        `  <span class="narve-notif-body">` +
        `    <span class="narve-notif-title">${escapeHtml(n.title)}</span>` +
        (n.body ? `    <span class="narve-notif-sub">${escapeHtml(n.body)}</span>` : '') +
        `    <span class="narve-notif-time">${relativeTime(n.created_at)}</span>` +
        `  </span>` +
        `</button>`
      );
    }).join('');
    state.list.innerHTML = rows +
      `<a class="narve-notif-more" href="/notifications">View all notifications →</a>`;
    // Wire per-item clicks
    state.list.querySelectorAll('.narve-notif-item').forEach((el) => {
      el.addEventListener('click', onItemClick);
    });
  }

  // ── Event handlers ────────────────────────────────────────────────────────
  async function onItemClick(ev) {
    const btn   = ev.currentTarget;
    const id    = Number(btn.dataset.id);
    const link  = btn.dataset.link;
    // Mark read optimistically
    const item = state.items.find((x) => x.id === id);
    if (item && !item.read_at) {
      item.read_at = Math.floor(Date.now() / 1000);
      state.unreadCount = Math.max(0, state.unreadCount - 1);
      renderBadge();
      btn.classList.remove('is-unread');
    }
    // Fire-and-forget server update
    api(`${API_BASE}/${id}/read`, { method: 'POST' }).catch(() => {});
    if (link) {
      window.location.href = link;
    }
  }

  function openDropdown() {
    if (state.isOpen) return;
    state.isOpen = true;
    state.dropdown.classList.add('is-open');
    document.addEventListener('click', onDocClick, true);
    // Auto-mark-all-read after a short delay (prevents accidental-read).
    // User still has a way to mark-all via the button; this is the
    // passive UX path the spec calls for.
    clearTimeout(state.autoreadHandle);
    state.autoreadHandle = setTimeout(markAllReadPassive, AUTOREAD_DELAY);
  }

  function closeDropdown() {
    if (!state.isOpen) return;
    state.isOpen = false;
    state.dropdown.classList.remove('is-open');
    document.removeEventListener('click', onDocClick, true);
    clearTimeout(state.autoreadHandle);
  }

  function onDocClick(ev) {
    if (!state.root.contains(ev.target)) closeDropdown();
  }

  async function markAllReadPassive() {
    // Only hits the server if there's something to mark — avoids traffic.
    if (state.unreadCount === 0) return;
    try {
      await api(`${API_BASE}/read-all`, { method: 'POST' });
    } catch (e) { /* ignore, next poll will self-correct */ }
    state.items.forEach((n) => { if (!n.read_at) n.read_at = Math.floor(Date.now() / 1000); });
    state.unreadCount = 0;
    renderBadge();
    // Re-render so the dot indicators clear.
    state.list.querySelectorAll('.narve-notif-item.is-unread')
      .forEach((el) => el.classList.remove('is-unread'));
  }

  async function markAllReadExplicit() {
    await markAllReadPassive();
  }

  async function refresh() {
    const data = await api(`${API_BASE}?limit=${DROPDOWN_LIMIT}`).catch(() => null);
    if (!data) return;
    state.items = data.notifications || [];
    state.unreadCount = Number(data.unread_count || 0);
    renderBadge();
    renderList();
  }

  async function pollUnread() {
    const data = await api(`${API_BASE}/unread_count`).catch(() => null);
    if (!data) return;
    const next = Number(data.count || 0);
    if (next !== state.unreadCount) {
      state.unreadCount = next;
      renderBadge();
      // If the badge ticked up, refresh the list so the dropdown has
      // the fresh rows when the user opens it.
      if (!state.isOpen) refresh();
    }
  }

  // ── SSE ───────────────────────────────────────────────────────────────────
  function connectSSE() {
    if (state.sse) return;
    let es;
    try {
      es = new EventSource(`${API_BASE}/stream`);
    } catch (e) {
      // Some browsers disable EventSource; the 30s poll covers us.
      return;
    }
    state.sse = es;
    es.addEventListener('notification', (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        // Prepend, cap at 50 in local state; dropdown only renders first 10
        state.items = [payload, ...state.items].slice(0, 50);
        state.unreadCount += 1;
        renderBadge();
        if (state.isOpen) renderList();
      } catch (e) { /* ignore malformed */ }
    });
    es.addEventListener('error', () => {
      // Browser auto-reconnects; nothing to do. If it closes permanently
      // (e.g. auth lost), we fall back to the 30s poll.
      if (es.readyState === EventSource.CLOSED) {
        state.sse = null;
      }
    });
  }

  // ── Mount ─────────────────────────────────────────────────────────────────
  function mount() {
    const root = document.createElement('div');
    root.id = 'narve-notif-root';
    root.innerHTML = `
      <button class="narve-notif-bell" aria-label="Notifications" aria-haspopup="true" aria-expanded="false">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 7a4 4 0 0 1 8 0v3l1 2H3l1-2V7z"/>
          <path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/>
        </svg>
        <span class="narve-notif-badge" aria-live="polite" aria-atomic="true"></span>
      </button>
      <div class="narve-notif-dropdown" role="dialog" aria-label="Notifications">
        <div class="narve-notif-header">
          <span>Notifications</span>
          <button type="button" class="narve-notif-mark-all" aria-label="Mark all read">Mark all read</button>
        </div>
        <div class="narve-notif-list"></div>
      </div>
    `;
    document.body.appendChild(root);
    state.root     = root;
    state.bell     = root.querySelector('.narve-notif-bell');
    state.badge    = root.querySelector('.narve-notif-badge');
    state.dropdown = root.querySelector('.narve-notif-dropdown');
    state.list     = root.querySelector('.narve-notif-list');

    state.bell.addEventListener('click', (e) => {
      e.stopPropagation();
      state.bell.setAttribute('aria-expanded', String(!state.isOpen));
      state.isOpen ? closeDropdown() : openDropdown();
    });
    root.querySelector('.narve-notif-mark-all').addEventListener('click', (e) => {
      e.stopPropagation();
      markAllReadExplicit();
    });
    // Escape closes
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && state.isOpen) closeDropdown();
    });
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  async function init() {
    if (state.initialised) return;
    // Probe: if the unread_count endpoint 401s we're not authenticated;
    // skip mounting entirely — no bell on marketing / pre-auth pages.
    const probe = await api(`${API_BASE}/unread_count`).catch(() => null);
    if (probe === null) return;   // 401 or error — bail quietly

    mount();
    state.unreadCount = Number(probe.count || 0);
    renderBadge();
    await refresh();
    connectSSE();
    state.pollHandle = setInterval(pollUnread, UNREAD_POLL_MS);
    state.initialised = true;
  }

  // Defer until DOM is ready (script tag is already [defer], but be
  // paranoid about timing if the script is included inline).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
