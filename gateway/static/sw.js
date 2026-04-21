/* narve.ai service worker — offline shell + push notifications.
 *
 * Versioning: bump SW_VERSION when any cached asset needs purging.
 * Clients will see the new worker on next navigation; `skipWaiting`
 * + `clients.claim` is deliberately *not* used so in-flight pages
 * keep their current assets until they reload.
 */

const SW_VERSION = 'narve-v1';
const RUNTIME_CACHE = `${SW_VERSION}-runtime`;
const STATIC_CACHE = `${SW_VERSION}-static`;

// Precache the offline shell. These paths must stay aligned with the
// public routes in server.py (/manifest.json, /favicon.ico) and the static
// mount prefix (/_gateway_static/).
const PRECACHE_URLS = [
  '/manifest.json',
  '/favicon.ico',
  '/_gateway_static/img/icon-192.png',
  '/_gateway_static/img/logo.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      // Allow individual precache failures — a missing asset shouldn't
      // block the SW from installing entirely.
      Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(url).catch(() => null)
        )
      )
    )
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((n) => !n.startsWith(SW_VERSION))
          .map((n) => caches.delete(n))
      )
    )
  );
});

// Router: decide strategy per request.
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Only handle same-origin GETs. Cross-origin (Stripe, fonts CDN) and
  // non-GET requests (POST form submits) pass straight through.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never cache dynamic/private endpoints. These must always hit the
  // network to respect auth, CSRF, and freshness.
  if (
    url.pathname.startsWith('/api/v1/') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/admin/') ||
    url.pathname.startsWith('/billing/') ||
    url.pathname.startsWith('/stripe/') ||
    url.pathname === '/logout'
  ) {
    return;
  }

  // Hashed static assets (/_gateway_static/*?v=…): cache-first, long-lived.
  if (url.pathname.startsWith('/_gateway_static/')) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Root-level static files we serve via explicit routes.
  if (url.pathname === '/manifest.json' || url.pathname === '/favicon.ico') {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // HTML navigations: network-first, fall back to cached shell if offline.
  if (req.mode === 'navigate' || req.headers.get('accept')?.includes('text/html')) {
    event.respondWith(networkFirst(req, RUNTIME_CACHE));
    return;
  }

  // Everything else: stale-while-revalidate.
  event.respondWith(staleWhileRevalidate(req, RUNTIME_CACHE));
});

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    return new Response('', { status: 504, statusText: 'Offline' });
  }
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    // Offline shell fallback — try the dashboards page first, then a minimal 503.
    const shell = await cache.match('/dashboards');
    if (shell) return shell;
    return new Response(
      '<!doctype html><html><head><meta charset="utf-8"><title>Offline — narve.ai</title><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font-family:-apple-system,Inter,sans-serif;background:#0d0d0d;color:#fff;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0;padding:24px;text-align:center}h1{font-size:24px;margin:0 0 8px;font-weight:600}p{color:#888;margin:0;max-width:320px}</style></head><body><div><h1>You\u2019re offline</h1><p>narve.ai needs a connection to fetch fresh market data. Try again when you\u2019re back online.</p></div></body></html>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );
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
    .catch(() => cached);
  return cached || networkPromise;
}

// ── Push notifications ──────────────────────────────────────────────
// Server sends a JSON payload via Web Push. Shape:
//   { title, body, url?, tag?, icon?, badge?, data? }
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
  event.waitUntil(
    (async () => {
      const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of all) {
        if (client.url.includes(new URL(target, self.location.origin).pathname) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(target);
    })()
  );
});

// Allow the app to trigger cache eviction without a hard reload.
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
