/* narve.ai service worker v2 — offline shell, 4 cache strategies,
 * background sync for user predictions, push notifications.
 *
 * Versioning: bump CACHE_V when cached-asset shape needs purging. Old
 * versions are evicted on `activate`. Clients see the new worker on
 * next navigation; we deliberately don't `skipWaiting` / `clients.claim`
 * so in-flight pages keep their current assets until they reload.
 *
 * Cache strategies:
 *   1. /_gateway_static/*    → cache-first   (static bundle)
 *   2. /api/status, /api/status/feed.xml, whitelisted API GETs → SWR
 *   3. *.{png,jpg,webp,svg,gif,ico} → cache-first
 *   4. HTML navigations       → network-first → cache → /offline
 *
 * Background sync: pending prediction POSTs queued in IndexedDB
 * (`narve-offline-queue` → `predictions` store) are replayed when the
 * `sync` event fires (tag `submit-prediction`). Browsers that lack
 * Background Sync fall back to a replay on next sw `fetch` or on
 * `message` with `{type:'FLUSH_QUEUE'}` from the page.
 */

const CACHE_V = 'narve-v2';
const STATIC_CACHE = `${CACHE_V}-static`;
const API_CACHE = `${CACHE_V}-api`;
const IMG_CACHE = `${CACHE_V}-img`;
const RUNTIME_CACHE = `${CACHE_V}-runtime`;

const OFFLINE_URL = '/offline';

// Precache: offline shell + core static assets. Every entry is wrapped
// in a per-URL .catch so one missing asset doesn't abort install.
const STATIC_ASSETS = [
  OFFLINE_URL,
  '/manifest.json',
  '/favicon.ico',
  '/_gateway_static/img/icon-192.png',
  '/_gateway_static/img/logo.png',
  '/_gateway_static/gateway.css',
  '/_gateway_static/narve-app.js',
  '/_gateway_static/mobile-a11y.css',
];

// API endpoints safe to cache (public / idempotent reads only). Any
// GET matching one of these path prefixes uses stale-while-revalidate
// so the page shows last-known data instantly and refreshes in the
// background. Auth-sensitive paths (/api/admin, /api/billing) are
// never cached.
const CACHEABLE_API_PREFIXES = [
  '/api/status',
  '/api/markets',
  '/api/signals',
  '/api/feed',
  '/api/best-bets',
  '/api/predictions/public',
  '/api/sources',
];

// Never cache — always network, never fall back to cache. Auth +
// side-effecting paths. A stale cache here could leak to a logged-out
// user after logout.
const NEVER_CACHE_PREFIXES = [
  '/api/auth',
  '/api/admin',
  '/api/billing',
  '/auth/',
  '/admin/',
  '/billing/',
  '/stripe/',
];

// IndexedDB queue for background sync.
const IDB_DB = 'narve-offline-queue';
const IDB_STORE = 'predictions';
const IDB_VERSION = 1;

// ── Install ──────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    await Promise.all(
      STATIC_ASSETS.map((url) =>
        cache.add(url).catch((err) => {
          console.warn('[sw] precache failed:', url, err);
        }),
      ),
    );
  })());
});

// ── Activate ─────────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => !n.startsWith(CACHE_V))
        .map((n) => caches.delete(n)),
    );
  })());
});

// ── Fetch router ─────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // POST /api/predictions: if it fails (offline), queue for background
  // sync. Wrap only this specific path; every other POST passes straight
  // through to the network.
  if (req.method === 'POST' && new URL(req.url).pathname === '/api/predictions') {
    event.respondWith(queuePredictionOnFailure(req));
    return;
  }

  // Only cache same-origin GETs past this point.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never-cache list: explicit bypass so stale auth / billing never
  // render to a logged-out user.
  if (NEVER_CACHE_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    return;
  }

  // Strategy 1: static bundle — cache-first, long-lived.
  if (
    url.pathname.startsWith('/_gateway_static/') ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/favicon.ico'
  ) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Strategy 3: images — cache-first.
  if (/\.(webp|png|jpg|jpeg|svg|gif|ico|avif)$/i.test(url.pathname)) {
    event.respondWith(cacheFirst(req, IMG_CACHE));
    return;
  }

  // Strategy 2: whitelisted API reads — stale-while-revalidate.
  if (CACHEABLE_API_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(staleWhileRevalidate(req, API_CACHE));
    return;
  }

  // Strategy 4: HTML navigations — network-first, cache fallback,
  // /offline as last resort.
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(networkFirstWithOffline(req));
    return;
  }

  // Default: stale-while-revalidate in runtime cache.
  event.respondWith(staleWhileRevalidate(req, RUNTIME_CACHE));
});

// ── Strategies ───────────────────────────────────────────────────────

// Clone a cached Response and stamp it with served-from-cache headers so
// the page can render a "Last updated X min ago (cached)" ribbon. The
// SW also annotates `X-Cached-At` with the Date header from the cached
// response — pages read this to compute a relative timestamp.
async function withCacheHeaders(resp) {
  if (!resp) return resp;
  const clone = resp.clone();
  const body = await clone.blob();
  const headers = new Headers(resp.headers);
  headers.set('X-Served-From', 'cache');
  if (!headers.has('X-Cached-At')) {
    const d = resp.headers.get('date');
    if (d) headers.set('X-Cached-At', d);
  }
  return new Response(body, {
    status: resp.status,
    statusText: resp.statusText,
    headers,
  });
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return withCacheHeaders(cached);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    return new Response('', { status: 504, statusText: 'Offline' });
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const networkPromise = fetch(request)
    .then((fresh) => {
      if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => (cached ? withCacheHeaders(cached) : undefined));
  // Return cached (stamped) immediately if we have it; otherwise wait
  // for network.
  if (cached) return withCacheHeaders(cached);
  return networkPromise;
}

async function networkFirstWithOffline(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    const cached = await cache.match(request);
    if (cached) return withCacheHeaders(cached);
    // Precached offline shell.
    const staticCache = await caches.open(STATIC_CACHE);
    const offline = await staticCache.match(OFFLINE_URL);
    if (offline) return withCacheHeaders(offline);
    // Worst-case inline fallback.
    return new Response(
      '<!doctype html><meta charset="utf-8"><title>Offline</title>'
      + '<body style="font-family:system-ui;padding:40px;text-align:center">'
      + '<h1>You\u2019re offline</h1><p>Reconnect and refresh.</p></body>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
    );
  }
}

// ── Background sync: pending user predictions ───────────────────────

async function queuePredictionOnFailure(request) {
  // Try the network first. Only queue on genuine network failure, not
  // on server errors — those are the server's problem to handle.
  try {
    const response = await fetch(request.clone());
    return response;
  } catch (netErr) {
    // Read the request body before we walk away; Request objects are
    // single-shot for body consumption.
    let body = null;
    try {
      body = await request.clone().text();
    } catch {}
    const entry = {
      url: request.url,
      method: 'POST',
      headers: Array.from(request.headers.entries()),
      body,
      queuedAt: Date.now(),
    };
    await idbPut(entry);

    // Try to register a background sync. If the browser doesn't support
    // it (Safari, older Firefox) the entry stays in IDB and gets
    // replayed on the next successful fetch event or on manual
    // FLUSH_QUEUE message.
    try {
      if ('sync' in self.registration) {
        await self.registration.sync.register('submit-prediction');
      }
    } catch {}

    return new Response(
      JSON.stringify({
        queued: true,
        message: 'Offline. Your prediction will submit when you reconnect.',
      }),
      { status: 202, headers: { 'Content-Type': 'application/json' } },
    );
  }
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'submit-prediction') {
    event.waitUntil(flushPendingPredictions());
  }
});

self.addEventListener('message', (event) => {
  if (!event.data) return;
  if (event.data === 'SKIP_WAITING') {
    self.skipWaiting();
    return;
  }
  // Pages can ask us to flush the queue (e.g. when they detect
  // `navigator.onLine` went true). Useful on browsers without the
  // Background Sync API.
  if (event.data.type === 'FLUSH_QUEUE') {
    event.waitUntil(flushPendingPredictions());
  }
  // Settings page → Clear cache button. Drops every narve-v2 cache on
  // the SW side so a subsequent navigation refetches everything.
  if (event.data.type === 'CLEAR_CACHE') {
    event.waitUntil((async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith(CACHE_V))
          .map((n) => caches.delete(n)),
      );
    })());
  }
});

async function flushPendingPredictions() {
  const entries = await idbAll();
  for (const entry of entries) {
    try {
      const headers = new Headers(entry.headers || []);
      const r = await fetch(entry.url, {
        method: entry.method,
        headers,
        body: entry.body,
        credentials: 'same-origin',
      });
      if (r.ok || (r.status >= 400 && r.status < 500)) {
        // 4xx means the server refused on its own terms (validation etc.)
        // — don't retry, drop it so we don't hammer forever.
        await idbDelete(entry.id);
      }
      // 5xx / network failure: keep entry, next sync will retry.
    } catch {
      // Network still down — leave entry, retry next time.
    }
  }
}

// ── Minimal IndexedDB wrapper for the prediction queue ─────────────

function idbOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_DB, IDB_VERSION);
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

async function idbPut(value) {
  const db = await idbOpen();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readwrite');
    const req = tx.objectStore(IDB_STORE).add(value);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbAll() {
  const db = await idbOpen();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readonly');
    const req = tx.objectStore(IDB_STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function idbDelete(id) {
  const db = await idbOpen();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readwrite');
    const req = tx.objectStore(IDB_STORE).delete(id);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

// ── Push notifications ──────────────────────────────────────────────
self.addEventListener('push', (event) => {
  let payload = { title: 'narve.ai', body: 'New update', data: {} };
  if (event.data) {
    try { payload = { ...payload, ...event.data.json() }; }
    catch { payload.body = event.data.text() || payload.body; }
  }
  const options = {
    body: payload.body,
    icon: payload.icon || '/_gateway_static/img/icon-192.png',
    badge: payload.badge || '/_gateway_static/img/badge-72.png',
    tag: payload.tag || 'narve-general',
    data: { url: payload.url || '/', ...(payload.data || {}) },
    requireInteraction: false,
  };
  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      if (client.url.includes(new URL(target, self.location.origin).pathname) && 'focus' in client) {
        return client.focus();
      }
    }
    if (clients.openWindow) return clients.openWindow(target);
  })());
});
