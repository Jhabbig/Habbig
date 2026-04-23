/* narve-app.js — shared runtime for every page.
 *
 * Responsibilities:
 *   1. Register the service worker (PWA offline shell).
 *   2. Capture + expose the beforeinstallprompt event for a subtle
 *      install banner (shown once, dismissal persisted in localStorage).
 *   3. Focus management for modals: focus trap + return-focus on close.
 *   4. Global ARIA live-region for toast-like status announcements.
 *   5. Ensure every <main> element has id="main" so the skip link lands.
 *
 * Exposes `window.narve` as the shared namespace. Safe to load on every
 * page: the install prompt only appears if the browser issues the event.
 */
(function () {
  'use strict';

  // Shared namespace. Other modules (shortcuts.js, page-specific scripts)
  // attach to this so the window namespace stays tidy.
  const narve = (window.narve = window.narve || {});

  // ── 1. Service worker registration ──────────────────────────────────
  // Only register in production-like origins (https or localhost). Avoids
  // surprising behaviour on LAN IPs during dev (Chrome warns).
  function canRegisterSW() {
    if (!('serviceWorker' in navigator)) return false;
    const { protocol, hostname } = window.location;
    if (protocol === 'https:') return true;
    return hostname === 'localhost' || hostname === '127.0.0.1';
  }

  if (canRegisterSW()) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((err) => {
        // Non-fatal: the app still works without the SW.
        if (window.console && console.warn) {
          console.warn('[narve] sw register failed:', err);
        }
      });
    });
  }

  // ── 2. Install prompt ───────────────────────────────────────────────
  const INSTALL_DISMISS_KEY = 'narve:install-banner-dismissed';
  let deferredPrompt = null;

  // Only prompt a *logged-in* visitor to install. The session cookie
  // (pm_gateway_session) is HttpOnly so we can't read it from JS;
  // instead we probe for DOM/JS signals that authed pages render.
  // Kept multi-signal so the gate survives template renames — any one
  // hit counts as "this is an authed page".
  function isLoggedIn() {
    // 1. Explicit opt-in: server sets <html data-narve-authed="1">.
    if (document.documentElement.hasAttribute('data-narve-authed')) return true;
    // 2. Dashboard switcher config (only injected on authed pages).
    if (window.__hbSwitcher) return true;
    // 3. Sidebar user avatar — every authed shell renders one.
    if (document.querySelector('.sidebar-user-avatar')) return true;
    // 4. Any logout link anchored in the DOM.
    if (document.querySelector(
      'a[href="/logout"], a[href*="/logout"], form[action="/logout"]'
    )) return true;
    return false;
  }

  function installBannerSuppressed() {
    try {
      return (
        // Public / pre-login pages never get the install nag.
        !isLoggedIn() ||
        localStorage.getItem(INSTALL_DISMISS_KEY) === '1' ||
        // Already running as an installed PWA
        window.matchMedia('(display-mode: standalone)').matches ||
        window.navigator.standalone === true
      );
    } catch { return false; }
  }

  function showInstallBanner() {
    if (installBannerSuppressed() || document.getElementById('narve-install-banner')) return;
    const el = document.createElement('div');
    el.id = 'narve-install-banner';
    el.className = 'narve-install-banner';
    el.setAttribute('role', 'region');
    el.setAttribute('aria-label', 'Install narve.ai as an app');
    el.innerHTML = [
      '<span class="narve-install-banner__text">narve.ai can be installed as an app.</span>',
      '<button type="button" class="narve-install-banner__btn narve-install-banner__btn--primary" data-narve-install>Install</button>',
      '<button type="button" class="narve-install-banner__btn" data-narve-dismiss aria-label="Dismiss install banner">Dismiss</button>',
    ].join('');
    document.body.appendChild(el);
    el.querySelector('[data-narve-install]').addEventListener('click', async () => {
      if (!deferredPrompt) { el.remove(); return; }
      deferredPrompt.prompt();
      try { await deferredPrompt.userChoice; } catch {}
      deferredPrompt = null;
      el.remove();
    });
    el.querySelector('[data-narve-dismiss]').addEventListener('click', () => {
      try { localStorage.setItem(INSTALL_DISMISS_KEY, '1'); } catch {}
      el.remove();
    });
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', showInstallBanner, { once: true });
    } else {
      showInstallBanner();
    }
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    try { localStorage.setItem(INSTALL_DISMISS_KEY, '1'); } catch {}
    const el = document.getElementById('narve-install-banner');
    if (el) el.remove();
  });

  // ── 3. Focus management & skip link target ─────────────────────────
  // Ensure there's a `#main` landing point for the skip-to-content link.
  // Preference: an explicit <main> element. Fallback: the first
  // `.main-content` div that sidebar pages use.
  function ensureMainLandmark() {
    let main = document.getElementById('main');
    if (main) return main;
    main = document.querySelector('main');
    if (main) {
      if (!main.id) main.id = 'main';
      return main;
    }
    const fallback = document.querySelector('.main-content, .pr-wrap, .landing-main, section.landing-hero, .pr-hero, .auth-shell, .intel-main, main');
    if (fallback) {
      fallback.id = 'main';
      if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1');
      if (!fallback.hasAttribute('role')) fallback.setAttribute('role', 'main');
    }
    return fallback;
  }

  // Focus trap for modal-like elements. Call narve.trapFocus(modalEl)
  // on open; it returns a dispose() fn that releases the trap and
  // returns focus to the previously-focused element.
  const FOCUSABLE = [
    'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])', 'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]',
  ].join(',');

  function focusableChildren(root) {
    return Array.from(root.querySelectorAll(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null || el === document.activeElement
    );
  }

  narve.trapFocus = function trapFocus(container) {
    const previouslyFocused = document.activeElement;
    const onKey = (e) => {
      if (e.key !== 'Tab') return;
      const items = focusableChildren(container);
      if (items.length === 0) { e.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    };
    container.addEventListener('keydown', onKey);
    // Move initial focus into the container (first focusable or the
    // container itself if it's focusable).
    const first = focusableChildren(container)[0];
    (first || container).focus({ preventScroll: true });
    return function dispose() {
      container.removeEventListener('keydown', onKey);
      if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
        previouslyFocused.focus({ preventScroll: true });
      }
    };
  };

  // ── 4. Live region ─────────────────────────────────────────────────
  // Singleton ARIA live region for one-shot announcements (e.g. "Saved",
  // "Copied to clipboard"). Polite so it never interrupts the user.
  let liveRegion = null;
  function ensureLiveRegion() {
    if (liveRegion) return liveRegion;
    liveRegion = document.createElement('div');
    liveRegion.setAttribute('role', 'status');
    liveRegion.setAttribute('aria-live', 'polite');
    liveRegion.setAttribute('aria-atomic', 'true');
    liveRegion.className = 'narve-sr-only';
    document.body.appendChild(liveRegion);
    return liveRegion;
  }
  narve.announce = function announce(message) {
    const region = ensureLiveRegion();
    // Clear first so identical consecutive announcements still fire.
    region.textContent = '';
    // Next tick — screen readers need the mutation to register.
    setTimeout(() => { region.textContent = String(message || ''); }, 30);
  };

  // ── 5. Push notifications (opt-in) ─────────────────────────────────
  // Small wrapper around PushManager. Pages expose a toggle somewhere
  // (settings, profile, a bell) and call narve.push.enable() on user
  // click — the browser only shows the permission prompt in response
  // to a user gesture, so never call it from boot().
  function urlBase64ToUint8Array(b64) {
    const padding = '='.repeat((4 - (b64.length % 4)) % 4);
    const s = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(s);
    const arr = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  function getCSRFToken() {
    // Cookie is set by CSRFMiddleware; httponly=false so JS can read it.
    const m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  async function getRegistration() {
    if (!('serviceWorker' in navigator)) throw new Error('Service workers unsupported');
    const reg = await navigator.serviceWorker.ready;
    if (!reg) throw new Error('Service worker not registered');
    return reg;
  }

  narve.push = {
    supported: (
      typeof window !== 'undefined' &&
      'serviceWorker' in navigator &&
      'PushManager' in window &&
      'Notification' in window
    ),

    async status() {
      if (!this.supported) return 'unsupported';
      if (Notification.permission === 'denied') return 'denied';
      try {
        const reg = await getRegistration();
        const sub = await reg.pushManager.getSubscription();
        return sub ? 'subscribed' : (Notification.permission === 'granted' ? 'granted' : 'default');
      } catch { return 'unsupported'; }
    },

    async enable() {
      if (!this.supported) throw new Error('Push is not supported in this browser');
      if (Notification.permission === 'denied') {
        throw new Error('Notifications are blocked — enable them in site settings');
      }

      // Fetch the server's VAPID public key. Cacheable; 503 means the
      // feature isn't configured server-side (pywebpush missing).
      const keyResp = await fetch('/api/push/vapid-key', { credentials: 'same-origin' });
      if (!keyResp.ok) {
        throw new Error('Push is not configured on the server');
      }
      const { publicKey } = await keyResp.json();

      const reg = await getRegistration();
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        });
      }

      const body = sub.toJSON();
      const postResp = await fetch('/api/push/subscribe', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': getCSRFToken(),
        },
        body: JSON.stringify(body),
      });
      if (!postResp.ok) {
        // Roll back the browser-side subscription so a later retry isn't
        // wedged thinking the server already has the keys.
        try { await sub.unsubscribe(); } catch {}
        throw new Error(`Subscribe failed (${postResp.status})`);
      }
      return true;
    },

    async disable() {
      if (!this.supported) return false;
      const reg = await getRegistration();
      const sub = await reg.pushManager.getSubscription();
      if (!sub) return false;
      const endpoint = sub.endpoint;
      try { await sub.unsubscribe(); } catch {}
      try {
        await fetch('/api/push/unsubscribe', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': getCSRFToken(),
          },
          body: JSON.stringify({ endpoint }),
        });
      } catch { /* server-side cleanup is best-effort */ }
      return true;
    },

    async sendTest() {
      const resp = await fetch('/api/push/test', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': getCSRFToken() },
      });
      if (!resp.ok) throw new Error(`Test send failed (${resp.status})`);
      return resp.json();
    },
  };

  // ── 6. Offline banner + IDB client queue ────────────────────────────
  // Small fixed banner appears when navigator.onLine goes false (and on
  // initial load if already offline). Removes itself on online. Also
  // posts FLUSH_QUEUE to the service worker on reconnect so Safari
  // (which lacks Background Sync) still replays queued predictions.

  const OFFLINE_BANNER_ID = 'narve-offline-banner';

  function showOfflineBanner() {
    if (document.getElementById(OFFLINE_BANNER_ID)) return;
    const el = document.createElement('div');
    el.id = OFFLINE_BANNER_ID;
    el.className = 'narve-offline-banner';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.textContent = 'Offline \u2014 showing cached data. Updates will sync when you reconnect.';
    document.body.appendChild(el);
  }

  function hideOfflineBanner() {
    const el = document.getElementById(OFFLINE_BANNER_ID);
    if (el) el.remove();
  }

  function flushSWQueue() {
    try {
      if (navigator.serviceWorker && navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage({ type: 'FLUSH_QUEUE' });
      }
    } catch {}
  }

  function initOfflineBanner() {
    if (!('onLine' in navigator)) return;
    if (!navigator.onLine) showOfflineBanner();
    window.addEventListener('offline', showOfflineBanner);
    window.addEventListener('online', () => {
      hideOfflineBanner();
      flushSWQueue();
    });
  }

  // Minimal IDB wrapper — shape MUST match gateway/static/sw.js
  // (same DB name, store, keyPath) so the service worker can pick up
  // anything the page queues directly.
  const IDB_DB = 'narve-offline-queue';
  const IDB_STORE = 'predictions';

  function idbOpen() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) {
        reject(new Error('IndexedDB unavailable'));
        return;
      }
      const req = indexedDB.open(IDB_DB, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(IDB_STORE)) {
          db.createObjectStore(IDB_STORE, { keyPath: 'id', autoIncrement: true });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPut(entry) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      const req = tx.objectStore(IDB_STORE).add(entry);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbCount() {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readonly');
      const req = tx.objectStore(IDB_STORE).count();
      req.onsuccess = () => resolve(req.result || 0);
      req.onerror = () => reject(req.error);
    });
  }

  narve.offline = {
    /**
     * Queue a prediction body for background sync. Writes to the same
     * IndexedDB store the SW reads, then asks for a background-sync
     * registration so the SW replays the request when the browser thinks
     * the network is back. Falls back to a direct FLUSH_QUEUE postMessage
     * if Background Sync is unavailable (Safari).
     */
    async queuePrediction(body) {
      const entry = {
        url: new URL('/api/predictions', window.location.origin).toString(),
        method: 'POST',
        headers: [
          ['Content-Type', 'application/json'],
          ['X-CSRF-Token', getCSRFToken()],
        ],
        body: typeof body === 'string' ? body : JSON.stringify(body || {}),
        queuedAt: Date.now(),
      };
      await idbPut(entry);
      try {
        const reg = await navigator.serviceWorker.ready;
        if (reg && 'sync' in reg) {
          await reg.sync.register('submit-prediction');
          return true;
        }
      } catch {}
      // No sync support — rely on FLUSH_QUEUE on next reconnect.
      return true;
    },
    async pendingCount() {
      try { return await idbCount(); } catch { return 0; }
    },
    flushNow: flushSWQueue,
  };

  // Thin submit wrapper used by the feed / prediction composer. Prefers
  // the network; on failure (or the SW's 202 response) queues for sync
  // and surfaces a status toast via the shared live region.
  /**
   * narve.cached — fetch wrapper that surfaces SW cache provenance.
   *
   *   const { json, cachedAt, fromCache } = await narve.cached.fetchJSON(url);
   *
   * When the response was served from the service-worker cache (via the
   * `X-Served-From: cache` header the SW stamps), `fromCache` is true
   * and `cachedAt` is a Date. Callers render a ribbon like
   * "Last updated 3 min ago (cached)" via narve.cached.renderRibbon(el, meta).
   */
  narve.cached = {
    async fetchJSON(url, init) {
      const resp = await fetch(url, Object.assign({ credentials: 'same-origin' }, init || {}));
      const fromCache = resp.headers.get('X-Served-From') === 'cache';
      const cachedAtHdr = resp.headers.get('X-Cached-At') || resp.headers.get('date') || '';
      const cachedAt = cachedAtHdr ? new Date(cachedAtHdr) : null;
      const json = await resp.json().catch(() => null);
      return { json, cachedAt, fromCache, ok: resp.ok };
    },
    renderRibbon(container, meta) {
      if (!container) return;
      let el = container.querySelector('.narve-cached-ribbon');
      if (!el) {
        el = document.createElement('p');
        el.className = 'narve-cached-ribbon';
        container.prepend(el);
      }
      if (!meta || !meta.fromCache) {
        el.remove();
        return;
      }
      const ts = meta.cachedAt instanceof Date && !isNaN(meta.cachedAt)
        ? meta.cachedAt
        : new Date();
      const mins = Math.max(0, Math.round((Date.now() - ts.getTime()) / 60000));
      const label = mins < 1 ? 'moments ago'
        : mins < 60 ? mins + ' min ago'
        : Math.round(mins / 60) + ' h ago';
      el.textContent = 'Last updated ' + label + ' (cached)';
    },
  };

  narve.predictions = {
    async submit(body) {
      const payload = typeof body === 'string' ? body : JSON.stringify(body || {});
      try {
        const resp = await fetch('/api/predictions', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': getCSRFToken(),
          },
          body: payload,
        });
        if (resp.status === 202) {
          // SW intercepted and queued the request itself.
          narve.announce('Offline \u2014 prediction will send when you reconnect.');
          return { queued: true };
        }
        if (!resp.ok) {
          let errBody = {};
          try { errBody = await resp.json(); } catch {}
          throw new Error(errBody.error || `Submit failed (${resp.status})`);
        }
        return await resp.json().catch(() => ({ ok: true }));
      } catch (err) {
        // Genuine network error — IDB-queue it ourselves so a browser
        // without a controlling SW still retries on reconnect.
        try {
          await narve.offline.queuePrediction(payload);
          narve.announce('Offline \u2014 prediction queued.');
          return { queued: true };
        } catch {
          throw err;
        }
      }
    },
  };

  // Warm the SW runtime cache with /dashboards once per authed visit.
  // First-visit-offline users get a real landing instead of the generic
  // offline shell. Fire-and-forget; any failure is non-fatal.
  async function precacheDashboards() {
    try {
      if (!('caches' in window) || !isLoggedIn()) return;
      if (window.location.pathname === '/dashboards') return;  // already the current page
      const key = 'narve:dashboards-precached';
      // Re-warm once per day so a long-lived session still gets a
      // reasonably fresh snapshot.
      const last = Number(sessionStorage.getItem(key) || '0');
      if (Date.now() - last < 24 * 60 * 60 * 1000) return;
      const resp = await fetch('/dashboards', { credentials: 'same-origin' });
      if (!resp.ok) return;
      const cache = await caches.open('narve-v2-runtime');
      await cache.put('/dashboards', resp.clone());
      try { sessionStorage.setItem(key, String(Date.now())); } catch {}
    } catch { /* best-effort */ }
  }

  // ── 7. Boot ─────────────────────────────────────────────────────────
  function boot() {
    ensureMainLandmark();
    ensureLiveRegion();
    initOfflineBanner();
    // Defer precache until after first paint so it doesn't compete
    // with critical-path fetches.
    if ('requestIdleCallback' in window) {
      requestIdleCallback(precacheDashboards, { timeout: 4000 });
    } else {
      setTimeout(precacheDashboards, 2500);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
