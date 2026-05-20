// MidtermEdge service worker
// - Receives web push events from the backend alert worker
// - Caches the SPA shell so the app is usable offline
// - Click on a notification opens the corresponding race detail page

const CACHE_NAME = 'midtermedge-v1'
const SHELL = ['/', '/manifest.json']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)).catch(() => null)
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  // Never cache the data API — it changes constantly.
  if (url.pathname.startsWith('/data/') || url.pathname.startsWith('/auth/') ||
      url.pathname.startsWith('/admin/') || url.pathname.startsWith('/premium/')) {
    return
  }
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached
      return fetch(request).then((resp) => {
        if (resp.ok && url.origin === self.location.origin) {
          const copy = resp.clone()
          caches.open(CACHE_NAME).then((c) => c.put(request, copy)).catch(() => null)
        }
        return resp
      }).catch(() => cached)
    })
  )
})

self.addEventListener('push', (event) => {
  let payload = {}
  try {
    payload = event.data ? event.data.json() : {}
  } catch (_) {
    payload = { title: 'MidtermEdge', body: event.data ? event.data.text() : '' }
  }
  const title = payload.title || 'MidtermEdge'
  const options = {
    body: payload.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: payload.url || '/' },
    tag: payload.race_key || 'midtermedge',
    renotify: true,
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = (event.notification.data && event.notification.data.url) || '/'
  event.waitUntil(
    self.clients.matchAll({ type: 'window' }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus()
      }
      if (self.clients.openWindow) return self.clients.openWindow(target)
    })
  )
})
