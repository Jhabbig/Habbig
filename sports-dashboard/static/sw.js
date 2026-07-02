// Service worker — handles PWA install, basic offline shell, and push notifications.
// Cache name is versioned; bump CACHE_VERSION to force clients to refresh
// after a deploy that changes the static shell.

const CACHE_VERSION = 'sharpe-v1';
const STATIC_ASSETS = [
  '/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      // addAll is all-or-nothing — wrap each asset so one missing icon
      // doesn't prevent install entirely
      Promise.all(STATIC_ASSETS.map((url) =>
        cache.add(url).catch(() => {})
      ))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // API calls: always network, never cache (stale signals are worse than no signals).
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;
  // Static assets: cache-first
  if (STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});

// ── Web Push ───────────────────────────────────────────────────────────────
// Server POSTs an alert payload signed with VAPID. We render it as a
// notification and, on click, focus or open the dashboard.

self.addEventListener('push', (event) => {
  if (!event.data) return;
  let payload = {};
  try {
    payload = event.data.json();
  } catch {
    payload = { title: 'Sharpe', body: event.data.text() };
  }
  const title = payload.title || 'Sharpe — new signal';
  const options = {
    body: payload.body || 'A signal fired on a market you watch.',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: payload.tag || 'sharpe-signal',
    data: payload.data || {},
    requireInteraction: false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // Focus an existing tab if one is already open
      for (const w of wins) {
        if (w.url.includes(self.location.origin)) {
          w.focus();
          if ('navigate' in w) w.navigate(url);
          return;
        }
      }
      return clients.openWindow(url);
    })
  );
});
